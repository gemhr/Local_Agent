"""BusinessToolDefinition 到既有 Tool Runtime 的只读绑定。"""

from __future__ import annotations

import json
import inspect

from core.agent_platform.application import _schema_matches
from core.agent_platform.contracts import BusinessToolDefinition, ToolInvocationContext
from core.runtime.retry import OperationIdempotency
from core.runtime.tool_adapters import (
    ToolAdapter,
    ToolAdapterInvocationError,
    ToolAdapterResponse,
)
from core.runtime.tool_contract import (
    ToolErrorCategory,
    ToolExecutionPhase,
    ToolExecutionSpec,
    ToolExecutionStatus,
    ToolInvocation,
    ToolSideEffectKind,
    ToolSideEffectState,
    thaw_json,
)
from core.runtime.cancellation import RunCancelledError
from core.runtime.context import RunDeadlineExceededError
from core.runtime.tool_registry import ToolDescriptor, ToolRegistration


class BusinessToolAdapter(ToolAdapter):
    """执行同步、只读业务 handler，只向 handler 暴露窄 context facade。"""

    def __init__(self, definition: BusinessToolDefinition) -> None:
        self.definition = definition
        self.handler_binding_id = definition.handler_binding_id or (
            f"{definition.handler.__module__}.{definition.handler.__qualname__}"
        )
        self.spec = ToolExecutionSpec(
            tool_name=definition.name,
            side_effect_kind=ToolSideEffectKind.NONE,
            idempotency=OperationIdempotency.READ_ONLY,
            requires_resource_key=False,
            supports_cooperative_cancellation=True,
            supports_side_effect_checkpoint=False,
            default_timeout_seconds=10.0,
            max_output_bytes=16_384,
            max_concurrency=8,
        )

    def llm_input_schema(self) -> dict[str, object]:
        return thaw_json(self.definition.input_schema)

    def build_invocation(self, argument_text: str) -> ToolInvocation:
        try:
            arguments = json.loads(argument_text)
        except (TypeError, ValueError):
            arguments = None
        if not isinstance(arguments, dict) or not _schema_matches(self.definition.input_schema, arguments):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_VALIDATION_ERROR",
                safe_message="Business Tool 参数无效。",
                phase=ToolExecutionPhase.VALIDATION,
            ) from None
        return ToolInvocation.create(tool_name=self.spec.tool_name, arguments=arguments)

    def invoke_once(self, invocation: ToolInvocation, context) -> ToolAdapterResponse:
        context.raise_if_cancelled()
        narrow_context = ToolInvocationContext(
            operation_id=invocation.invocation_id,
            remaining_seconds=context.remaining_seconds(),
            cancellation_check=context.raise_if_cancelled,
        )
        try:
            result = self.definition.handler(thaw_json(invocation.arguments), narrow_context)
            if isinstance(result, str):
                content = result
            else:
                content = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (RunCancelledError, RunDeadlineExceededError):
            raise
        except Exception:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.INTERNAL,
                safe_error_code="BUSINESS_TOOL_FAILED",
                safe_message="Business Tool 执行失败。",
                phase=ToolExecutionPhase.INVOCATION,
                side_effect_state=ToolSideEffectState.NOT_STARTED,
                side_effect_state_authoritative=True,
            ) from None
        context.raise_if_cancelled()
        return ToolAdapterResponse(
            content=content,
            content_type="text/plain",
            safe_summary="Business Tool completed.",
            status=ToolExecutionStatus.SUCCEEDED,
            side_effect_state=ToolSideEffectState.NOT_STARTED,
            side_effect_state_authoritative=True,
        )


def compile_business_tool_registration(definition: BusinessToolDefinition) -> ToolRegistration:
    if definition.side_effect_kind != "READ_ONLY":
        raise ValueError("当前仅支持 READ_ONLY Business Tool")
    if definition.resource_selector is not None:
        raise ValueError("当前不支持 Business Tool resource selector")
    if inspect.iscoroutinefunction(definition.handler):
        raise ValueError("当前不支持 async Business Tool handler")
    if definition.handler_binding_id is None and (
        not inspect.isfunction(definition.handler)
        or "<" in definition.handler.__qualname__
    ):
        raise ValueError("Business Tool 仅支持 module-level function，其它 callable 必须声明 handler_binding_id")
    return ToolRegistration(
        ToolDescriptor(name=definition.name, description=definition.description),
        BusinessToolAdapter(definition),
    )


__all__ = ["BusinessToolAdapter", "compile_business_tool_registration"]
