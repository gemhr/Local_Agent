#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""已迁移 Tool 的单次调用 Adapter；不拥有 Retry、Budget 或 Runtime 状态。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
import json
from typing import Any, Callable, Mapping, Protocol

from core.runtime.retry import OperationIdempotency
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
from tools.complex_workflow_simulator import (
    ComplexWorkflowRequest,
    ComplexWorkflowResult,
    ComplexWorkflowSimulationTool,
    InMemoryWorkflowStateStore,
    WorkflowExecutionMode,
    WorkflowResultStatus,
    WorkflowStateStore,
)


class ToolAdapterContext(Protocol):
    def raise_if_cancelled(self) -> None: ...
    def before_side_effect(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ToolAdapterResponse:
    content: str
    content_type: str
    safe_summary: str
    status: ToolExecutionStatus = ToolExecutionStatus.SUCCEEDED
    side_effect_state: ToolSideEffectState = ToolSideEffectState.NOT_STARTED
    idempotency_replayed: bool = False
    side_effect_state_authoritative: bool = True


class ToolAdapterInvocationError(RuntimeError):
    """Adapter 只抛安全分类，不保留原始异常、参数或输出正文。"""

    def __init__(
        self,
        *,
        category: ToolErrorCategory,
        safe_error_code: str,
        safe_message: str,
        phase: ToolExecutionPhase = ToolExecutionPhase.INVOCATION,
        side_effect_state: ToolSideEffectState = ToolSideEffectState.NOT_STARTED,
        side_effect_state_authoritative: bool = False,
        compensation_attempted: bool = False,
        compensation_succeeded: bool = False,
        output_started: bool = False,
        partial_result: Mapping[str, Any] | None = None,
    ) -> None:
        self.category = category
        self.safe_error_code = safe_error_code
        self.safe_message = safe_message
        self.phase = phase
        self.side_effect_state = side_effect_state
        self.side_effect_state_authoritative = side_effect_state_authoritative
        self.compensation_attempted = compensation_attempted
        self.compensation_succeeded = compensation_succeeded
        self.output_started = output_started
        self.partial_result = partial_result
        super().__init__(safe_message)


class ToolAdapter(ABC):
    """Adapter 的唯一动作是执行一次已解析 Invocation。"""

    is_async = False
    spec: ToolExecutionSpec

    def spec_for(self, invocation: ToolInvocation) -> ToolExecutionSpec:
        if invocation.tool_name != self.spec.tool_name:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_NAME_MISMATCH",
                safe_message="Tool Invocation 与 Adapter 名称不匹配。",
                phase=ToolExecutionPhase.VALIDATION,
            )
        return self.spec

    @abstractmethod
    def build_invocation(self, argument_text: str) -> ToolInvocation:
        """由 Transport/AgentRouter 边界把字符串解析为强类型 Invocation。"""

    @abstractmethod
    def invoke_once(
        self, invocation: ToolInvocation, context: ToolAdapterContext
    ) -> ToolAdapterResponse:
        """执行一次，不重试、不记预算、不修改 Run/Step 状态。"""


class _RuntimeOwnedWorkflowLockManager:
    """Adapter 模式下资源互斥已由 Runtime Lease 持有，此对象不再创建第二把锁。"""

    def acquire(self, resource_key: str) -> bool:
        return True

    def release(self, resource_key: str) -> None:
        return None


class ComplexWorkflowToolAdapter(ToolAdapter):
    """把模拟器自身类型映射到统一 Runtime Contract。"""

    spec = ToolExecutionSpec(
        tool_name="complex_workflow_simulator",
        side_effect_kind=ToolSideEffectKind.UNKNOWN,
        idempotency=OperationIdempotency.UNKNOWN,
        requires_resource_key=True,
        supports_cooperative_cancellation=True,
        supports_side_effect_checkpoint=True,
        default_timeout_seconds=10.0,
        max_output_bytes=32_768,
        max_concurrency=8,
    )

    _ERROR_CATEGORY = {
        "TOOL_VALIDATION_ERROR": ToolErrorCategory.VALIDATION,
        "TOOL_RESOURCE_CONFLICT": ToolErrorCategory.RESOURCE_CONFLICT,
        "TOOL_TRANSIENT_FAILURE": ToolErrorCategory.TRANSIENT,
        "TOOL_TIMEOUT": ToolErrorCategory.TIMEOUT,
        "TOOL_CANCELLED": ToolErrorCategory.CANCELLED,
        "TOOL_IDEMPOTENCY_CONFLICT": ToolErrorCategory.VALIDATION,
        "TOOL_PARTIAL_FAILURE": ToolErrorCategory.INTERNAL,
        "TOOL_SIDE_EFFECT_FAILURE": ToolErrorCategory.POST_COMMIT_RESPONSE_FAILURE,
        "TOOL_COMPENSATION_FAILURE": ToolErrorCategory.COMPENSATION_FAILED,
        "TOOL_UNKNOWN_FAILURE": ToolErrorCategory.INTERNAL,
    }

    def __init__(
        self,
        *,
        state_store: WorkflowStateStore | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._state_store = state_store or InMemoryWorkflowStateStore()
        self._sleeper = sleeper

    def build_invocation(self, argument_text: str) -> ToolInvocation:
        try:
            payload = json.loads(argument_text)
            request = ComplexWorkflowRequest.from_dict(payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_VALIDATION_ERROR",
                safe_message="复杂流程 Tool 参数无效。",
                phase=ToolExecutionPhase.VALIDATION,
            ) from None
        timeout = payload.get("requested_timeout_seconds")
        return ToolInvocation.create(
            tool_name=self.spec.tool_name,
            arguments=payload,
            idempotency_key=request.idempotency_key,
            resource_key=request.resource_key,
            requested_timeout_seconds=timeout,
        )

    def spec_for(self, invocation: ToolInvocation) -> ToolExecutionSpec:
        super().spec_for(invocation)
        arguments = thaw_json(invocation.arguments)
        try:
            mode = WorkflowExecutionMode(arguments.get("execution_mode"))
        except (TypeError, ValueError):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_VALIDATION_ERROR",
                safe_message="复杂流程 execution_mode 无效。",
                phase=ToolExecutionPhase.VALIDATION,
            ) from None
        if mode == WorkflowExecutionMode.DRY_RUN:
            return replace(
                self.spec,
                side_effect_kind=ToolSideEffectKind.NONE,
                idempotency=OperationIdempotency.READ_ONLY,
            )
        if mode == WorkflowExecutionMode.IDEMPOTENT_COMMIT:
            return replace(
                self.spec,
                side_effect_kind=ToolSideEffectKind.LOCAL_STATE_MUTATION,
                idempotency=OperationIdempotency.IDEMPOTENT_WITH_KEY,
                supports_idempotency_replay=True,
            )
        return replace(
            self.spec,
            side_effect_kind=ToolSideEffectKind.LOCAL_STATE_MUTATION,
            idempotency=OperationIdempotency.NON_IDEMPOTENT,
        )

    def invoke_once(
        self, invocation: ToolInvocation, context: ToolAdapterContext
    ) -> ToolAdapterResponse:
        context.raise_if_cancelled()
        try:
            request = ComplexWorkflowRequest.from_dict(thaw_json(invocation.arguments))
        except (TypeError, ValueError):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_VALIDATION_ERROR",
                safe_message="复杂流程 Tool 参数无效。",
                phase=ToolExecutionPhase.VALIDATION,
            ) from None
        if (
            request.resource_key != invocation.resource_key
            or request.idempotency_key != invocation.idempotency_key
        ):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_KEY_MISMATCH",
                safe_message="Tool 参数中的 Key 与 Invocation Contract 不一致。",
                phase=ToolExecutionPhase.VALIDATION,
            )
        kwargs: dict[str, Any] = {
            "state_store": self._state_store,
            "lock_manager": _RuntimeOwnedWorkflowLockManager(),
            "cancellation_probe": lambda: _cancelled(context),
            "before_side_effect": context.before_side_effect,
        }
        if self._sleeper is not None:
            kwargs["sleeper"] = self._sleeper
        result = ComplexWorkflowSimulationTool(**kwargs).execute(request)
        # 模拟器会把自身异常转成安全 Result；正式 Run Cancellation 必须在
        # Adapter 边界重新传播，不能被业务结果吞掉。
        context.raise_if_cancelled()
        return self._map_result(result)

    def _map_result(self, result: ComplexWorkflowResult) -> ToolAdapterResponse:
        side_effect_state = ToolSideEffectState.NOT_STARTED
        if result.compensation_attempted and result.compensation_succeeded:
            side_effect_state = ToolSideEffectState.COMPENSATED
        elif result.side_effect_committed or result.idempotency_replayed:
            side_effect_state = ToolSideEffectState.COMMITTED
        content = json.dumps(
            result.to_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if result.status in {
            WorkflowResultStatus.SUCCEEDED,
            WorkflowResultStatus.IDEMPOTENCY_REPLAY,
        }:
            return ToolAdapterResponse(
                content=content,
                content_type="application/json",
                safe_summary="复杂流程 Tool 已完成。",
                side_effect_state=side_effect_state,
                idempotency_replayed=result.idempotency_replayed,
            )
        if result.status == WorkflowResultStatus.PARTIALLY_SUCCEEDED:
            return ToolAdapterResponse(
                content=content,
                content_type="application/json",
                safe_summary="复杂流程 Tool 部分完成。",
                status=ToolExecutionStatus.PARTIALLY_SUCCEEDED,
                side_effect_state=side_effect_state,
                idempotency_replayed=result.idempotency_replayed,
            )
        code = result.safe_error_code or "TOOL_UNKNOWN_FAILURE"
        raise ToolAdapterInvocationError(
            category=self._ERROR_CATEGORY.get(code, ToolErrorCategory.INTERNAL),
            safe_error_code=code,
            safe_message=result.safe_message,
            phase=(
                ToolExecutionPhase.COMPENSATION
                if result.compensation_attempted
                else ToolExecutionPhase.INVOCATION
            ),
            side_effect_state=side_effect_state,
            side_effect_state_authoritative=True,
            compensation_attempted=result.compensation_attempted,
            compensation_succeeded=result.compensation_succeeded,
            partial_result={
                "status": result.status.value,
                "item_count": len(result.item_results),
                "audit_digest": result.audit_digest,
            },
        )


class LegacyStringToolAdapter(ToolAdapter):
    """只适配已确认只读且输出可控的 Legacy ``str -> str`` Tool。"""

    def __init__(
        self,
        *,
        tool_name: str,
        function: Callable[[str], str],
        default_timeout_seconds: float = 3.0,
        max_output_bytes: int = 4096,
        max_concurrency: int = 4,
        error_prefixes: tuple[str, ...] = (),
    ) -> None:
        self._function = function
        self._error_prefixes = error_prefixes
        self.spec = ToolExecutionSpec(
            tool_name=tool_name,
            side_effect_kind=ToolSideEffectKind.NONE,
            idempotency=OperationIdempotency.READ_ONLY,
            requires_resource_key=False,
            supports_cooperative_cancellation=False,
            supports_side_effect_checkpoint=False,
            default_timeout_seconds=default_timeout_seconds,
            max_output_bytes=max_output_bytes,
            max_concurrency=max_concurrency,
        )

    def build_invocation(self, argument_text: str) -> ToolInvocation:
        if not isinstance(argument_text, str):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_VALIDATION_ERROR",
                safe_message="Legacy Tool 参数必须是字符串。",
                phase=ToolExecutionPhase.VALIDATION,
            )
        return ToolInvocation.create(
            tool_name=self.spec.tool_name,
            arguments={"argument_text": argument_text},
        )

    def invoke_once(
        self, invocation: ToolInvocation, context: ToolAdapterContext
    ) -> ToolAdapterResponse:
        context.raise_if_cancelled()
        arguments = thaw_json(invocation.arguments)
        argument_text = arguments.get("argument_text")
        if not isinstance(argument_text, str):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_VALIDATION_ERROR",
                safe_message="Legacy Tool 参数无效。",
                phase=ToolExecutionPhase.VALIDATION,
            )
        try:
            result = self._function(argument_text)
        except Exception:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.INTERNAL,
                safe_error_code="LEGACY_TOOL_INVOCATION_FAILED",
                safe_message="Legacy Tool 调用失败。",
            ) from None
        if not isinstance(result, str):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.OUTPUT_INVALID,
                safe_error_code="LEGACY_TOOL_OUTPUT_INVALID",
                safe_message="Legacy Tool 返回了无效输出。",
                phase=ToolExecutionPhase.OUTPUT,
            )
        if any(result.startswith(prefix) for prefix in self._error_prefixes):
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.OUTPUT_INVALID,
                safe_error_code="LEGACY_TOOL_REPORTED_ERROR",
                safe_message="Legacy Tool 返回了错误文本。",
                phase=ToolExecutionPhase.OUTPUT,
            )
        return ToolAdapterResponse(
            content=result,
            content_type="text/plain",
            safe_summary="只读 Tool 已完成。",
        )


def _cancelled(context: ToolAdapterContext) -> bool:
    try:
        context.raise_if_cancelled()
    except BaseException:
        return True
    return False


__all__ = [
    "ComplexWorkflowToolAdapter",
    "LegacyStringToolAdapter",
    "ToolAdapter",
    "ToolAdapterContext",
    "ToolAdapterInvocationError",
    "ToolAdapterResponse",
]
