#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP discovery model：外部 Provider 元数据的最小 bounded 快照。

本模块全部字段属于 External Provider Metadata（provider-declared、
untrusted），只作为 WP2 registration/provenance 决策输入；不得据此推导
risk、approval requirement、idempotency 或 permission。Runtime safety
fact 的唯一 Owner 是本地 ``ToolPolicyCatalog`` 与 ``ToolAdapter.spec_for``。

WP1 不产出 ``ToolRegistration``；本模块只是 ``MCP tools/list -> Local
Discovery Model`` 的输出形态。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json

from mcp.errors import McpDiscoveryError, McpProtocolError

# WP1 冻结单一 protocol version（WP0 Decision §13.6：实现必须使用单一
# 协议版本；Phase9 未引入 MCP SDK，采用官方 lifecycle 2025-06-18）。
MCP_PROTOCOL_VERSION = "2025-06-18"

REMOTE_TOOL_NAME_MAX_CHARS = 128
TOOL_DESCRIPTION_MAX_CHARS = 2048
TOOL_INPUT_SCHEMA_MAX_JSON_CHARS = 32768
TOOL_PROVIDER_METADATA_MAX_JSON_CHARS = 4096
MAX_JSON_STRUCTURE_DEPTH = 32
SERVER_INFO_TEXT_MAX_CHARS = 256
MAX_TOOLS_PER_SERVER = 128
MAX_TOOLS_LIST_PAGES = 16


def _canonical_json(value: object, *, max_chars: int, detail: str) -> str:
    _validate_json_structure_depth(value, detail=detail)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        raise McpDiscoveryError(detail) from None
    if len(text) > max_chars:
        raise McpDiscoveryError(detail)
    return text


def _validate_json_structure_depth(value: object, *, detail: str) -> None:
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > MAX_JSON_STRUCTURE_DEPTH:
            raise McpDiscoveryError(detail)
        if isinstance(current, dict):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)


def _bounded_text(value: object, *, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    return value[:max_chars]


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


@dataclass(frozen=True)
class McpToolDescriptor:
    """单个 remote MCP tool 的 discovery 快照（untrusted provider metadata）。"""

    server_id: str
    remote_name: str
    description: str
    input_schema_json: str
    provider_metadata_json: str

    @classmethod
    def from_protocol_payload(
        cls, server_id: str, payload: object
    ) -> "McpToolDescriptor":
        """校验并压缩一个 tools/list entry；任何越界/非法输入 fail closed。"""
        if not isinstance(payload, dict):
            raise McpDiscoveryError("tool_entry_not_object")
        name = payload.get("name")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > REMOTE_TOOL_NAME_MAX_CHARS
            or _has_control_chars(name)
        ):
            raise McpDiscoveryError("tool_name_invalid")
        description = payload.get("description", "")
        if (
            not isinstance(description, str)
            or len(description) > TOOL_DESCRIPTION_MAX_CHARS
        ):
            raise McpDiscoveryError("tool_description_invalid")
        schema = payload.get("inputSchema", {})
        if not isinstance(schema, dict):
            raise McpDiscoveryError("tool_input_schema_invalid")
        schema_json = _canonical_json(
            schema,
            max_chars=TOOL_INPUT_SCHEMA_MAX_JSON_CHARS,
            detail="tool_input_schema_oversize",
        )
        metadata: dict[str, object] = {}
        if "title" in payload:
            if not isinstance(payload["title"], str):
                raise McpDiscoveryError("tool_provider_metadata_invalid")
            metadata["title"] = payload["title"]
        if "annotations" in payload:
            if not isinstance(payload["annotations"], dict):
                raise McpDiscoveryError("tool_provider_metadata_invalid")
            metadata["annotations"] = payload["annotations"]
        metadata_json = _canonical_json(
            metadata,
            max_chars=TOOL_PROVIDER_METADATA_MAX_JSON_CHARS,
            detail="tool_provider_metadata_invalid",
        )
        return cls(
            server_id=server_id,
            remote_name=name,
            description=description,
            input_schema_json=schema_json,
            provider_metadata_json=metadata_json,
        )


@dataclass(frozen=True)
class McpInitializeInfo:
    """initialize handshake 的 bounded safe 结果（provider metadata）。"""

    protocol_version: str
    server_name: str
    server_version: str


# WP2 冻结结果策略（MCP_RESULT_STRATEGY）：只支持 text content block +
# ``isError``；structuredContent 仅在同时存在 TextContent 时被接受（此时
# 忽略，只用文本表示）；image/audio/resource_link/embedded 等一律记为
# unsupported，由 adapter safe failure，不隐式 fetch/decode。
TEXT_CONTENT_BLOCK_TYPE = "text"


@dataclass(frozen=True)
class McpToolCallResult:
    """tools/call result 的 bounded 归一化中间形态（untrusted provider 数据）。

    text_parts 是各 TextContent block 的原文（不可信，untrusted external
    observation）；总量受 client 单行 JSON-RPC line limit 约束。
    """

    is_error: bool
    text_parts: tuple[str, ...] = ()
    has_unsupported_content: bool = False

    @classmethod
    def from_protocol_payload(cls, payload: object) -> "McpToolCallResult":
        """校验并归一化 tools/call result；非法形态 fail closed。"""
        if not isinstance(payload, dict):
            raise McpProtocolError("tool_result_not_object")
        is_error = payload.get("isError", False)
        if not isinstance(is_error, bool):
            raise McpProtocolError("tool_result_is_error_invalid")
        content = payload.get("content")
        if not isinstance(content, list):
            raise McpProtocolError("tool_result_content_invalid")
        text_parts: list[str] = []
        has_unsupported_content = False
        for block in content:
            if not isinstance(block, dict):
                raise McpProtocolError("tool_result_content_invalid")
            if block.get("type") == TEXT_CONTENT_BLOCK_TYPE:
                text = block.get("text")
                if not isinstance(text, str):
                    raise McpProtocolError("tool_result_text_invalid")
                text_parts.append(text)
            else:
                has_unsupported_content = True
        if (
            not text_parts
            and "structuredContent" in payload
            and payload["structuredContent"] is not None
        ):
            # structured-only result 属 Phase9 不支持形态：safe failure。
            has_unsupported_content = True
        return cls(
            is_error=is_error,
            text_parts=tuple(text_parts),
            has_unsupported_content=has_unsupported_content,
        )


class McpServerDiscoveryStatus(str, Enum):
    """per-server startup discovery 状态（WP1 显式降级策略的输出）。"""

    AVAILABLE = "AVAILABLE"
    DISCOVERY_FAILED = "DISCOVERY_FAILED"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class McpServerDiscoveryResult:
    """单个 configured server 的 discovery 结果（content-free 事实）。"""

    server_id: str
    status: McpServerDiscoveryStatus
    protocol_version: str | None = None
    server_info_name: str = ""
    server_info_version: str = ""
    tools: tuple[McpToolDescriptor, ...] = ()
    safe_error_code: str | None = None


@dataclass(frozen=True)
class McpDiscoverySnapshot:
    """startup discovery 的 immutable 快照（STARTUP_SNAPSHOT_ONLY）。

    不支持 refresh / ``listChanged`` / runtime registration；不提供任何
    policy/risk/approval 推导入口。
    """

    results: tuple[McpServerDiscoveryResult, ...]

    def result_for(self, server_id: str) -> McpServerDiscoveryResult | None:
        for result in self.results:
            if result.server_id == server_id:
                return result
        return None

    def available_tools(self) -> tuple[McpToolDescriptor, ...]:
        return tuple(
            tool
            for result in self.results
            if result.status is McpServerDiscoveryStatus.AVAILABLE
            for tool in result.tools
        )


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "MAX_TOOLS_LIST_PAGES",
    "MAX_TOOLS_PER_SERVER",
    "McpDiscoverySnapshot",
    "McpInitializeInfo",
    "McpServerDiscoveryResult",
    "McpServerDiscoveryStatus",
    "McpToolCallResult",
    "McpToolDescriptor",
]
