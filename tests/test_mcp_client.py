"""Phase9-WP1：MCP stdio client 与 discovery boundary 测试。

全部通过本地确定性 fake MCP server 子进程（DETERMINISTIC_TEST）驱动真实
subprocess/stdin/stdout JSON-RPC 路径；不存在真实外部 MCP server，
本文件不构成 ``REAL_MCP_E2E``。

不覆盖 ToolExecution / HITL / Approval / Governance（属后续 WP）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mcp.client import StdioMcpClient
from mcp.config import McpServerConfig
from mcp.discovery import discover_server
from mcp.errors import (
    McpCapabilityError,
    McpDiscoveryError,
    McpProtocolError,
    McpProtocolVersionError,
    McpServerUnavailableError,
    McpTransportClosedError,
    McpTransportTimeoutError,
)
from mcp.models import (
    MCP_PROTOCOL_VERSION,
    McpServerDiscoveryStatus,
)

FAKE_SERVER_SOURCE = '''
import json
import os
import sys
import time

MODE = os.environ.get("FAKE_MCP_MODE", "success")


def _send(payload):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()


def _read():
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def _tool(name):
    return {
        "name": name,
        "description": "fake tool " + name,
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
        "annotations": {"readOnlyHint": True},
    }


def _init_result():
    if MODE == "bad_version":
        protocol_version = "1999-01-01"
    else:
        protocol_version = "PROTOCOL_VERSION"
    capabilities = {} if MODE == "no_tools_capability" else {"tools": {}}
    return {
        "protocolVersion": protocol_version,
        "capabilities": capabilities,
        "serverInfo": {"name": "fake-mcp", "version": "1.0.0"},
    }


while True:
    message = _read()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        if MODE == "init_error":
            _send({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32600, "message": "refused"},
            })
        else:
            _send({"jsonrpc": "2.0", "id": request_id, "result": _init_result()})
        if MODE == "exit_after_init":
            # 给 client 留出发送 notifications/initialized 的时间窗口，
            # 再模拟进程提前退出；tools/list 将因 stdout EOF 失败。
            time.sleep(1.0)
            sys.exit(0)
        continue
    if method == "tools/list":
        if MODE == "hang":
            time.sleep(3600)
            continue
        if MODE == "garbage_tools":
            sys.stdout.write("not-json\\n")
            sys.stdout.flush()
            continue
        cursor = (message.get("params") or {}).get("cursor")
        if MODE == "missing_tool_name":
            _send({"jsonrpc": "2.0", "id": request_id,
                   "result": {"tools": [{"description": "no name"}]}})
        elif MODE == "duplicate_tool":
            _send({"jsonrpc": "2.0", "id": request_id,
                   "result": {"tools": [_tool("dup"), _tool("dup")]}})
        elif MODE == "oversize_schema":
            tool = _tool("big")
            tool["inputSchema"] = {"blob": "y" * 40000}
            _send({"jsonrpc": "2.0", "id": request_id,
                   "result": {"tools": [tool]}})
        elif MODE == "tools_not_list":
            _send({"jsonrpc": "2.0", "id": request_id,
                   "result": {"tools": "oops"}})
        elif cursor is None:
            _send({"jsonrpc": "2.0", "id": request_id,
                   "result": {"tools": [_tool("echo")],
                              "nextCursor": "page-2"}})
        else:
            _send({"jsonrpc": "2.0", "id": request_id,
                   "result": {"tools": [_tool("reverse")]}})
        continue
    if request_id is not None:
        _send({"jsonrpc": "2.0", "id": request_id,
               "error": {"code": -32601, "message": "method not found"}})
'''.replace("PROTOCOL_VERSION", MCP_PROTOCOL_VERSION)


def _write_fake_server(tmp_path: Path) -> Path:
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(FAKE_SERVER_SOURCE, encoding="utf-8")
    return script


def _make_client(tmp_path: Path, mode: str, **kwargs: object) -> StdioMcpClient:
    script = _write_fake_server(tmp_path)
    config = McpServerConfig(
        server_id=kwargs.pop("server_id", "demo"),
        command=sys.executable,
        arguments=(str(script),),
        environment=(("FAKE_MCP_MODE", mode),),
    )
    return StdioMcpClient(
        server_config=config,
        connect_timeout_seconds=kwargs.pop("connect_timeout_seconds", 10.0),
        request_timeout_seconds=kwargs.pop("request_timeout_seconds", 10.0),
    )


@pytest.mark.asyncio
async def test_initialize_and_list_tools_success_with_pagination(
    tmp_path: Path,
) -> None:
    client = _make_client(tmp_path, "success")
    try:
        info = await client.initialize()
        assert info.protocol_version == MCP_PROTOCOL_VERSION
        assert info.server_name == "fake-mcp"
        assert client.broken is False
        tools = await client.list_tools()
        assert [tool.remote_name for tool in tools] == ["echo", "reverse"]
        assert all(tool.server_id == "demo" for tool in tools)
        assert tools[0].description == "fake tool echo"
        assert json.loads(tools[0].input_schema_json)["type"] == "object"
        # provider annotations 只作为 untrusted metadata 保留
        metadata = json.loads(tools[0].provider_metadata_json)
        assert metadata["annotations"] == {"readOnlyHint": True}
    finally:
        await client.close()
    assert client.closed is True
    assert client.exit_code is not None


@pytest.mark.asyncio
async def test_close_is_idempotent(tmp_path: Path) -> None:
    client = _make_client(tmp_path, "success")
    await client.initialize()
    await client.close()
    await client.close()
    assert client.closed is True


@pytest.mark.asyncio
async def test_initialize_error_response_fails_closed(tmp_path: Path) -> None:
    client = _make_client(tmp_path, "init_error")
    with pytest.raises(McpProtocolError) as excinfo:
        await client.initialize()
    assert excinfo.value.safe_error_code == "MCP_PROTOCOL_ERROR"
    await client.close()


@pytest.mark.asyncio
async def test_unsupported_protocol_version_fails_closed(
    tmp_path: Path,
) -> None:
    client = _make_client(tmp_path, "bad_version")
    with pytest.raises(McpProtocolVersionError):
        await client.initialize()
    await client.close()


@pytest.mark.asyncio
async def test_missing_tools_capability_fails_closed(tmp_path: Path) -> None:
    client = _make_client(tmp_path, "no_tools_capability")
    with pytest.raises(McpCapabilityError):
        await client.initialize()
    await client.close()


@pytest.mark.asyncio
async def test_malformed_json_line_fails_closed(tmp_path: Path) -> None:
    client = _make_client(tmp_path, "garbage_tools")
    await client.initialize()
    with pytest.raises(McpProtocolError):
        await client.list_tools()
    assert client.broken is True
    await client.close()


@pytest.mark.asyncio
async def test_missing_tool_name_fails_closed(tmp_path: Path) -> None:
    client = _make_client(tmp_path, "missing_tool_name")
    await client.initialize()
    with pytest.raises(McpDiscoveryError) as excinfo:
        await client.list_tools()
    assert excinfo.value.safe_error_code == "MCP_DISCOVERY_INVALID"
    await client.close()


@pytest.mark.asyncio
async def test_duplicate_tool_name_fails_closed(tmp_path: Path) -> None:
    client = _make_client(tmp_path, "duplicate_tool")
    await client.initialize()
    with pytest.raises(McpDiscoveryError):
        await client.list_tools()
    await client.close()


@pytest.mark.asyncio
async def test_oversize_input_schema_fails_closed(tmp_path: Path) -> None:
    client = _make_client(tmp_path, "oversize_schema")
    await client.initialize()
    with pytest.raises(McpDiscoveryError):
        await client.list_tools()
    await client.close()


@pytest.mark.asyncio
async def test_tools_field_not_list_fails_closed(tmp_path: Path) -> None:
    client = _make_client(tmp_path, "tools_not_list")
    await client.initialize()
    with pytest.raises(McpDiscoveryError):
        await client.list_tools()
    await client.close()


@pytest.mark.asyncio
async def test_server_unavailable_spawn_failure(tmp_path: Path) -> None:
    missing = tmp_path / "definitely-missing-executable.exe"
    config = McpServerConfig(
        server_id="demo", command=str(missing), arguments=()
    )
    client = StdioMcpClient(
        server_config=config,
        connect_timeout_seconds=2.0,
        request_timeout_seconds=2.0,
    )
    with pytest.raises(McpServerUnavailableError):
        await client.initialize()
    await client.close()
    assert client.closed is True


@pytest.mark.asyncio
async def test_request_timeout_marks_transport_failed(tmp_path: Path) -> None:
    client = _make_client(
        tmp_path, "hang", request_timeout_seconds=0.3
    )
    await client.initialize()
    with pytest.raises(McpTransportTimeoutError):
        await client.list_tools()
    assert client.broken is True
    await client.close(timeout=2.0)
    assert client.exit_code is not None


@pytest.mark.asyncio
async def test_premature_server_exit_fails_transport(tmp_path: Path) -> None:
    client = _make_client(tmp_path, "exit_after_init")
    await client.initialize()
    with pytest.raises(McpTransportClosedError):
        await client.list_tools()
    await client.close()


@pytest.mark.asyncio
async def test_discover_server_success_keeps_session_open(
    tmp_path: Path,
) -> None:
    script = _write_fake_server(tmp_path)
    config = McpServerConfig(
        server_id="demo",
        command=sys.executable,
        arguments=(str(script),),
        environment=(("FAKE_MCP_MODE", "success"),),
    )
    outcome = await discover_server(
        config, connect_timeout_seconds=10.0, request_timeout_seconds=10.0
    )
    try:
        assert outcome.result.status is McpServerDiscoveryStatus.AVAILABLE
        assert outcome.result.safe_error_code is None
        assert outcome.result.server_info_name == "fake-mcp"
        assert len(outcome.result.tools) == 2
        assert outcome.client is not None
        assert outcome.client.closed is False
    finally:
        if outcome.client is not None:
            await outcome.client.close()


@pytest.mark.asyncio
async def test_discover_server_failure_returns_safe_code_and_closes(
    tmp_path: Path,
) -> None:
    config = McpServerConfig(
        server_id="demo",
        command=str(tmp_path / "missing-executable.exe"),
        arguments=(),
    )
    outcome = await discover_server(
        config, connect_timeout_seconds=2.0, request_timeout_seconds=2.0
    )
    assert outcome.result.status is McpServerDiscoveryStatus.DISCOVERY_FAILED
    assert outcome.result.safe_error_code == "MCP_SERVER_UNAVAILABLE"
    assert outcome.result.tools == ()
    assert outcome.client is None
