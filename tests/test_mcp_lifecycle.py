"""Phase9-WP1：MCP 集成组件 lifecycle 测试（DETERMINISTIC_TEST）。

覆盖 application-scope 组件的 startup discovery 快照、per-server 显式降级
策略、disabled server、session 持有与 graceful shutdown。使用本地确定性
fake MCP server 子进程，不构成 ``REAL_MCP_E2E``。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mcp.client import StdioMcpClient
from mcp.config import McpServerConfig
from mcp.lifecycle import McpIntegrationComponent
from mcp.models import McpServerDiscoveryStatus

from tests.test_mcp_client import FAKE_SERVER_SOURCE


def _fake_config(
    tmp_path: Path,
    server_id: str,
    *,
    mode: str = "success",
    enabled: bool = True,
    command: str | None = None,
) -> McpServerConfig:
    script = tmp_path / "fake_mcp_server.py"
    if not script.exists():
        script.write_text(FAKE_SERVER_SOURCE, encoding="utf-8")
    return McpServerConfig(
        server_id=server_id,
        command=command or sys.executable,
        arguments=() if command else (str(script),),
        environment=() if command else (("FAKE_MCP_MODE", mode),),
        enabled=enabled,
    )


def _make_component(
    configs: tuple[McpServerConfig, ...],
    *,
    connect_timeout_seconds: float = 10.0,
    request_timeout_seconds: float = 10.0,
) -> McpIntegrationComponent:
    return McpIntegrationComponent(
        configs,
        connect_timeout_seconds=connect_timeout_seconds,
        request_timeout_seconds=request_timeout_seconds,
    )


@pytest.mark.asyncio
async def test_component_start_builds_frozen_snapshot(tmp_path: Path) -> None:
    component = _make_component(
        (
            _fake_config(tmp_path, "demo"),
            _fake_config(tmp_path, "other", enabled=False),
        )
    )
    try:
        await component.start()
        snapshot = component.discovery_snapshot
        assert snapshot is not None
        assert component.started is True
        result_by_id = {
            result.server_id: result for result in snapshot.results
        }
        assert (
            result_by_id["demo"].status is McpServerDiscoveryStatus.AVAILABLE
        )
        assert (
            result_by_id["other"].status is McpServerDiscoveryStatus.DISABLED
        )
        tools = snapshot.available_tools()
        assert [tool.remote_name for tool in tools] == ["echo", "reverse"]
        assert all(tool.server_id == "demo" for tool in tools)
        assert snapshot.result_for("missing") is None
    finally:
        assert await component.close() is True


@pytest.mark.asyncio
async def test_component_degrades_failing_server_without_blocking_startup(
    tmp_path: Path,
) -> None:
    component = _make_component(
        (
            _fake_config(tmp_path, "broken", command="Z-missing-executable-Z"),
            _fake_config(tmp_path, "healthy"),
        )
    )
    try:
        await component.start()
        snapshot = component.discovery_snapshot
        result_by_id = {
            result.server_id: result for result in snapshot.results
        }
        assert (
            result_by_id["broken"].status
            is McpServerDiscoveryStatus.DISCOVERY_FAILED
        )
        assert (
            result_by_id["broken"].safe_error_code
            == "MCP_SERVER_UNAVAILABLE"
        )
        assert (
            result_by_id["healthy"].status
            is McpServerDiscoveryStatus.AVAILABLE
        )
        # 失败 server 不保留 session；AVAILABLE server 保留。
        assert component.session_for("broken") is None
        assert component.session_for("healthy") is not None
    finally:
        assert await component.close() is True


@pytest.mark.asyncio
async def test_component_close_terminates_retained_sessions(
    tmp_path: Path,
) -> None:
    component = _make_component((_fake_config(tmp_path, "demo"),))
    await component.start()
    client = component.session_for("demo")
    assert isinstance(client, StdioMcpClient)
    assert client.closed is False
    assert await component.close() is True
    assert component.closed is True
    assert client.closed is True
    assert client.exit_code is not None
    # at most once：重复 close 直接返回成功。
    assert await component.close() is True


@pytest.mark.asyncio
async def test_component_close_before_start_is_noop() -> None:
    component = _make_component(())
    assert await component.close() is True
    assert component.discovery_snapshot is None


@pytest.mark.asyncio
async def test_component_double_start_rejected(tmp_path: Path) -> None:
    component = _make_component((_fake_config(tmp_path, "demo"),))
    try:
        await component.start()
        with pytest.raises(RuntimeError):
            await component.start()
    finally:
        await component.close()


@pytest.mark.asyncio
async def test_component_start_after_close_rejected() -> None:
    component = _make_component(())
    await component.close()
    with pytest.raises(RuntimeError):
        await component.start()
