"""Phase9-WP1：MCP discovery model 校验边界单测。

``McpToolDescriptor`` 全部字段是 untrusted External Provider Metadata；
本测试锁定 bounded 校验 fail-closed 行为，以及"不推导任何 Runtime
safety fact"的模型形态。
"""

from __future__ import annotations

import json

import pytest

from mcp.errors import McpDiscoveryError
from mcp.models import (
    MAX_TOOLS_PER_SERVER,
    McpDiscoverySnapshot,
    McpServerDiscoveryResult,
    McpServerDiscoveryStatus,
    McpToolDescriptor,
)


def _payload(**overrides: object) -> dict:
    tool = {
        "name": "remote_tool",
        "description": "a remote tool",
        "inputSchema": {"type": "object", "properties": {}},
    }
    tool.update(overrides)
    return tool


def test_descriptor_keeps_provider_metadata_only() -> None:
    descriptor = McpToolDescriptor.from_protocol_payload(
        "demo", _payload(title="My Tool", annotations={"readOnlyHint": True})
    )
    assert descriptor.server_id == "demo"
    assert descriptor.remote_name == "remote_tool"
    metadata = json.loads(descriptor.provider_metadata_json)
    assert metadata == {
        "title": "My Tool",
        "annotations": {"readOnlyHint": True},
    }
    # 模型不携带任何 risk/approval/idempotency/permission 字段。
    assert not any(
        field in descriptor.__dataclass_fields__
        for field in (
            "risk",
            "approval_required",
            "idempotency",
            "permission",
            "side_effect",
        )
    )


def test_missing_input_schema_defaults_to_empty_object() -> None:
    payload = _payload()
    del payload["inputSchema"]
    descriptor = McpToolDescriptor.from_protocol_payload("demo", payload)
    assert json.loads(descriptor.input_schema_json) == {}


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-dict",
        _payload(name=""),
        _payload(name="x" * 129),
        _payload(name="bad\x01name"),
        _payload(description="y" * 2049),
        _payload(description=123),
        _payload(inputSchema="not-a-dict"),
        _payload(inputSchema={"blob": "z" * 40000}),
        _payload(title=None),
        _payload(annotations="not-a-dict"),
        _payload(annotations={"blob": "z" * 5000}),
    ],
)
def test_invalid_tool_payloads_fail_closed(payload: object) -> None:
    with pytest.raises(McpDiscoveryError) as excinfo:
        McpToolDescriptor.from_protocol_payload("demo", payload)
    assert excinfo.value.safe_error_code == "MCP_DISCOVERY_INVALID"


def _available_result(server_id: str, tool_name: str):
    return McpServerDiscoveryResult(
        server_id=server_id,
        status=McpServerDiscoveryStatus.AVAILABLE,
        tools=(
            McpToolDescriptor.from_protocol_payload(
                server_id, _payload(name=tool_name)
            ),
        ),
    )


def test_snapshot_is_immutable_projection() -> None:
    snapshot = McpDiscoverySnapshot(
        results=(
            _available_result("a", "tool_a"),
            McpServerDiscoveryResult(
                server_id="b",
                status=McpServerDiscoveryStatus.DISCOVERY_FAILED,
                safe_error_code="MCP_SERVER_UNAVAILABLE",
            ),
        )
    )
    assert snapshot.result_for("a") is not None
    assert snapshot.result_for("b").status is (
        McpServerDiscoveryStatus.DISCOVERY_FAILED
    )
    tools = snapshot.available_tools()
    assert [tool.remote_name for tool in tools] == ["tool_a"]
    with pytest.raises(AttributeError):
        snapshot.results = ()  # type: ignore[misc]


def test_tool_limit_constant_is_frozen() -> None:
    # bounded discovery 上限：超过即 fail closed（client.list_tools 强制）。
    assert MAX_TOOLS_PER_SERVER == 128


def test_deep_or_non_json_schema_fails_closed() -> None:
    schema: object = {}
    for _ in range(33):
        schema = {"nested": schema}
    with pytest.raises(McpDiscoveryError):
        McpToolDescriptor.from_protocol_payload(
            "demo", _payload(inputSchema=schema)
        )
    with pytest.raises(McpDiscoveryError):
        McpToolDescriptor.from_protocol_payload(
            "demo", _payload(inputSchema={"value": float("nan")})
        )
