#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP-backed ToolAdapter（WP2：MCP Tool 接入既有 Tool Runtime 的唯一 seam）。

Owner 边界（WP0 Frozen Decision）：

- 本 Adapter 实现既有 ``ToolAdapter`` Contract（``build_invocation`` /
  ``spec_for`` / ``invoke_once``），由 startup registration 注册进既有
  ``ToolRegistry``；所有真实 MCP execution 只能发生在
  ``ToolExecutionService -> adapter.invoke_once -> session_for(server_id)
  -> client.call_tool`` 路径上。
- 不拥有 retry、Run/Step 状态、approval、Runtime deadline；timeout 只把
  Runtime effective deadline 作为上界传给底层 client（lower-level mechanism）。
- Runtime cancellation：outbound 前检查 ``context.raise_if_cancelled``，
  await 期间通过 async task 取消传播；``notifications/cancelled`` 由 client
  best-effort 发送；late response 不会成为第二次完成。
- ``MCP_GOVERNANCE_MODEL``：spec（side-effect/idempotency fact）来自本地
  operator 配置映射，MCP metadata（annotations/inputSchema/结果内容）全部
  是 untrusted provider 数据，只用于 argument 校验输入与文本观测。
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable, Mapping

from core.runtime.tool_adapters import (
    ToolAdapter,
    ToolAdapterContext,
    ToolAdapterInvocationError,
    ToolAdapterResponse,
)
from core.runtime.tool_contract import (
    ToolErrorCategory,
    ToolExecutionPhase,
    ToolExecutionSpec,
    ToolInvocation,
    ToolSideEffectKind,
    ToolSideEffectState,
    thaw_json,
)
from mcp.client import StdioMcpClient
from mcp.errors import McpBoundaryError, McpTransportTimeoutError
from mcp.models import MAX_JSON_STRUCTURE_DEPTH, McpToolCallResult

_ARGUMENTS_MAX_JSON_CHARS = 1_048_576


class McpBackedToolAdapter(ToolAdapter):
    """单个 remote MCP tool 的 LocalAgent ToolAdapter 绑定。

    持有 server_id / remote_name provenance 与 application-scope session
    resolver（``McpIntegrationComponent.session_for`` seam）；每次调用复用
    既有 session，不重新 spawn / initialize / tools/list。
    """

    is_async = True

    def __init__(
        self,
        *,
        spec: ToolExecutionSpec,
        server_id: str,
        remote_name: str,
        input_schema: Mapping[str, object],
        session_resolver: Callable[[], StdioMcpClient | None],
        request_timeout_seconds: float,
    ) -> None:
        if not isinstance(spec, ToolExecutionSpec):
            raise ValueError("spec 必须是 ToolExecutionSpec")
        if not server_id or not isinstance(server_id, str):
            raise ValueError("server_id 必须是非空字符串")
        if not remote_name or not isinstance(remote_name, str):
            raise ValueError("remote_name 必须是非空字符串")
        if not isinstance(input_schema, Mapping):
            raise ValueError("input_schema 必须是 JSON object")
        if not callable(session_resolver):
            raise ValueError("session_resolver 必须可调用")
        self.spec = spec
        self._server_id = server_id
        self._remote_name = remote_name
        self._input_schema: dict[str, object] = dict(input_schema)
        self._session_resolver = session_resolver
        self._request_timeout_seconds = float(request_timeout_seconds)

    # ---- read-only provenance facts（不进入 Runtime 公共合同）----

    @property
    def server_id(self) -> str:
        return self._server_id

    @property
    def remote_name(self) -> str:
        return self._remote_name

    def llm_input_schema(self) -> dict[str, object]:
        """model-facing 参数 schema：untrusted provider metadata 的 bounded 快照。"""
        return dict(self._input_schema)

    # ---- ToolAdapter Contract ----

    def build_invocation(self, argument_text: str) -> ToolInvocation:
        """Model arguments -> 既有 validation boundary -> immutable Invocation。

        inputSchema 校验是有界的静态结构检查（type/required/顶层属性类型），
        不执行任何来自 Server 的代码，也不做 codegen/eval。
        """
        if not isinstance(argument_text, str):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_VALIDATION_ERROR",
                safe_message="MCP Tool 参数必须是 JSON 字符串。",
                phase=ToolExecutionPhase.VALIDATION,
            )
        if len(argument_text) > _ARGUMENTS_MAX_JSON_CHARS:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_VALIDATION_ERROR",
                safe_message="MCP Tool 参数超出允许大小。",
                phase=ToolExecutionPhase.VALIDATION,
            )
        try:
            payload = json.loads(argument_text)
        except (TypeError, ValueError, RecursionError):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_VALIDATION_ERROR",
                safe_message="MCP Tool 参数不是合法 JSON。",
                phase=ToolExecutionPhase.VALIDATION,
            ) from None
        if not isinstance(payload, dict):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_VALIDATION_ERROR",
                safe_message="MCP Tool 参数必须是 JSON object。",
                phase=ToolExecutionPhase.VALIDATION,
            )
        try:
            self._validate_arguments(payload)
        except _ArgumentValidationError:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_VALIDATION_ERROR",
                safe_message="MCP Tool 参数不符合声明的输入约束。",
                phase=ToolExecutionPhase.VALIDATION,
            ) from None
        return ToolInvocation.create(
            tool_name=self.spec.tool_name,
            arguments=payload,
        )

    async def invoke_once(
        self, invocation: ToolInvocation, context: ToolAdapterContext
    ) -> ToolAdapterResponse:
        """执行一次已授权的 MCP tools/call；不重试、不延长 Runtime deadline。"""
        context.raise_if_cancelled()
        if invocation.tool_name != self.spec.tool_name:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_NAME_MISMATCH",
                safe_message="Tool Invocation 与 Adapter 名称不匹配。",
                phase=ToolExecutionPhase.VALIDATION,
            )
        client = self._session_resolver()
        if client is None or client.closed or client.broken:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.INTERNAL,
                safe_error_code="MCP_SESSION_UNAVAILABLE",
                safe_message="MCP Tool session 不可用。",
                phase=ToolExecutionPhase.INVOCATION,
            )
        is_side_effecting = self.spec.side_effect_kind is not ToolSideEffectKind.NONE
        if is_side_effecting:
            # side-effect checkpoint：提交副作用前重新检查取消/Deadline。
            context.before_side_effect()
        arguments = thaw_json(invocation.arguments)
        if not isinstance(arguments, dict):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_VALIDATION_ERROR",
                safe_message="MCP Tool 参数必须是 JSON object。",
                phase=ToolExecutionPhase.VALIDATION,
            )
        remaining = context.remaining_seconds()
        try:
            result = await client.call_tool(
                self._remote_name,
                arguments,
                timeout=min(remaining, self._request_timeout_seconds),
            )
        except asyncio.CancelledError:
            # Runtime cancellation：client 已 best-effort 发送
            # notifications/cancelled；late response 不会产生第二次完成。
            raise
        except McpBoundaryError as exc:
            raise self._map_boundary_error(exc) from None
        context.raise_if_cancelled()
        return self._normalize_result(result, is_side_effecting)

    # ---- internals ----

    def _normalize_result(
        self, result: object, is_side_effecting: bool
    ) -> ToolAdapterResponse:
        if not isinstance(result, McpToolCallResult):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.INTERNAL,
                safe_error_code="MCP_PROTOCOL_ERROR",
                safe_message="MCP Tool 返回了无效结果。",
                phase=ToolExecutionPhase.OUTPUT,
            )
        if result.is_error:
            # isError=true 必须映射 Existing typed failure，不得当作成功观测。
            if is_side_effecting:
                # 副作用结果未知：side_effect_state_authoritative=False，
                # tracker 收口为 UNKNOWN，retry 由既有规则判 OUTCOME_UNKNOWN。
                raise ToolAdapterInvocationError(
                    category=ToolErrorCategory.SIDE_EFFECT_UNKNOWN,
                    safe_error_code="MCP_TOOL_REPORTED_ERROR",
                    safe_message="MCP Tool 报告执行失败，副作用结果未知。",
                    phase=ToolExecutionPhase.OUTPUT,
                )
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.OUTPUT_INVALID,
                safe_error_code="MCP_TOOL_REPORTED_ERROR",
                safe_message="MCP Tool 报告执行失败。",
                phase=ToolExecutionPhase.OUTPUT,
                side_effect_state=ToolSideEffectState.NOT_STARTED,
                side_effect_state_authoritative=True,
            )
        if result.has_unsupported_content:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.OUTPUT_INVALID,
                safe_error_code="MCP_TOOL_RESULT_UNSUPPORTED",
                safe_message="MCP Tool 返回了不支持的结果内容。",
                phase=ToolExecutionPhase.OUTPUT,
                side_effect_state=(
                    ToolSideEffectState.COMMITTED
                    if is_side_effecting
                    else ToolSideEffectState.NOT_STARTED
                ),
                side_effect_state_authoritative=True,
            )
        content = "\n".join(result.text_parts)
        return ToolAdapterResponse(
            content=content,
            content_type="text/plain",
            safe_summary="MCP Tool 调用已完成。",
            side_effect_state=(
                ToolSideEffectState.COMMITTED
                if is_side_effecting
                else ToolSideEffectState.NOT_STARTED
            ),
            side_effect_state_authoritative=True,
        )

    @staticmethod
    def _map_boundary_error(exc: McpBoundaryError) -> ToolAdapterInvocationError:
        """MCP boundary 失败 -> Existing typed failure；只携带 safe code。"""
        if isinstance(exc, McpTransportTimeoutError):
            return ToolAdapterInvocationError(
                category=ToolErrorCategory.TIMEOUT,
                safe_error_code=exc.safe_error_code,
                safe_message="MCP Tool 调用超时。",
                phase=ToolExecutionPhase.INVOCATION,
            )
        return ToolAdapterInvocationError(
            category=ToolErrorCategory.INTERNAL,
            safe_error_code=exc.safe_error_code,
            safe_message="MCP Tool 调用失败。",
            phase=ToolExecutionPhase.INVOCATION,
        )

    def _validate_arguments(self, payload: dict) -> None:
        """bounded 静态 schema 检查：type=object / required / 顶层属性类型。"""
        _validate_json_depth(payload)
        schema_type = self._input_schema.get("type")
        if schema_type is not None and schema_type != "object":
            raise _ArgumentValidationError()
        required = self._input_schema.get("required")
        if isinstance(required, list):
            for name in required:
                if isinstance(name, str) and name not in payload:
                    raise _ArgumentValidationError()
        properties = self._input_schema.get("properties")
        if not isinstance(properties, dict):
            return
        for key, property_schema in properties.items():
            if key not in payload or not isinstance(property_schema, dict):
                continue
            expected = property_schema.get("type")
            if not _json_type_matches(payload[key], expected):
                raise _ArgumentValidationError()


class _ArgumentValidationError(ValueError):
    """内部校验失败信号；不离开 adapter 边界。"""


def _validate_json_depth(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > MAX_JSON_STRUCTURE_DEPTH:
            raise _ArgumentValidationError()
        if isinstance(current, dict):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)


def _json_type_matches(value: object, expected: object) -> bool:
    """顶层属性 primitive type 检查；未知/缺失 type 声明不失败（permissive）。"""
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return True


__all__ = [
    "McpBackedToolAdapter",
]
