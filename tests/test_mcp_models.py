"""Phase9-WP1：MCP discovery model 校验边界单测。

``McpToolDescriptor`` 全部字段是 untrusted External Provider Metadata；
本测试锁定 bounded 校验 fail-closed 行为，以及"不推导任何 Runtime
safety fact"的模型形态。
"""

from __future__ import annotations

import json

import pytest

from mcp.errors import McpDiscoveryError, McpProtocolError
from mcp.models import (
    MAX_TOOLS_PER_SERVER,
    McpDiscoverySnapshot,
    McpServerDiscoveryResult,
    McpServerDiscoveryStatus,
    McpToolDescriptor,
    McpToolCallResult,
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


def test_tool_result_text_only_regression() -> None:
    result = McpToolCallResult.from_protocol_payload(
        {"content": [{"type": "text", "text": ""}]}
    )
    assert result.is_error is False
    assert result.text_parts == ("",)
    assert result.has_unsupported_content is False


@pytest.mark.parametrize("mime", [None, "application/json"])
def test_embedded_text_resource_is_accepted_without_propagating_metadata(mime) -> None:
    resource = {"uri": "repo://owner/path%20with%20space", "text": "body"}
    if mime is not None:
        resource["mimeType"] = mime
    result = McpToolCallResult.from_protocol_payload(
        {
            "content": [
                {
                    "type": "resource",
                    "resource": resource,
                    "annotations": {"sentinel": "ANNOTATION"},
                    "_meta": {"sentinel": "META"},
                }
            ]
        }
    )
    assert result.text_parts == ("body",)
    assert result.has_unsupported_content is False
    assert "repo://" not in "\n".join(result.text_parts)


def test_mixed_and_multiple_embedded_text_preserve_order_and_empty_parts() -> None:
    result = McpToolCallResult.from_protocol_payload(
        {
            "content": [
                {"type": "text", "text": "status"},
                {"type": "resource", "resource": {"uri": "x:a", "text": "body A"}},
                {"type": "text", "text": ""},
                {"type": "resource", "resource": {"uri": "x:b", "text": "body B"}},
            ]
        }
    )
    assert "\n".join(result.text_parts) == "status\nbody A\n\nbody B"


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "resource", "resource": "not-an-object"},
        {"type": "resource", "resource": {"uri": "x:a", "text": 1}},
        {"type": "resource", "resource": {"text": "secret"}},
        {"type": "resource", "resource": {"uri": "1bad:value", "text": "secret"}},
        {"type": "resource", "resource": {"uri": "x:bad value", "text": "secret"}},
        {"type": "resource", "resource": {"uri": "x:bad%2", "text": "secret"}},
        {"type": "resource", "resource": {"uri": "x:a", "text": "secret", "mimeType": 1}},
        {"type": "resource", "resource": {"uri": "x:a", "text": "\ud800"}},
        {"type": "text", "text": "\ud800"},
    ],
)
def test_malformed_tool_result_text_or_resource_fails_closed(payload) -> None:
    with pytest.raises(McpProtocolError) as excinfo:
        McpToolCallResult.from_protocol_payload({"content": [payload]})
    assert excinfo.value.safe_error_code == "MCP_PROTOCOL_ERROR"
    assert "secret" not in str(excinfo.value)


@pytest.mark.parametrize(
    "block",
    [
        {"type": "resource", "resource": {"uri": "x:a", "blob": "AA=="}},
        {
            "type": "resource",
            "resource": {"uri": "x:a", "text": "ignored", "blob": "AA=="},
        },
        {"type": "resource_link", "uri": "x:a", "name": "a"},
        {"type": "image", "data": "AA==", "mimeType": "image/png"},
        {"type": "audio", "data": "AA==", "mimeType": "audio/wav"},
        {"type": "future", "value": "unknown"},
    ],
)
def test_non_text_content_subsets_remain_unsupported(block) -> None:
    result = McpToolCallResult.from_protocol_payload({"content": [block]})
    assert result.has_unsupported_content is True


def test_embedded_text_does_not_enable_structured_content_compatibility() -> None:
    result = McpToolCallResult.from_protocol_payload(
        {
            "content": [
                {"type": "resource", "resource": {"uri": "x:a", "text": "body"}}
            ],
            "structuredContent": {"answer": 42},
        }
    )
    assert result.text_parts == ("body",)
    assert result.has_unsupported_content is True
