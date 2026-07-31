from __future__ import annotations

from datetime import UTC, datetime
import threading
from typing import Mapping

from core.runtime import (
    BudgetLedger,
    FaultAction,
    FaultInjectionController,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InjectedFaultCode,
    OperationIdempotency,
    RunBudget,
    ToolAdapter,
    ToolAdapterResponse,
    ToolExecutionSpec,
    ToolInvocation,
    ToolSideEffectKind,
    ToolSideEffectState,
    create_run_context,
)


NOW = datetime(2026, 7, 31, tzinfo=UTC)


class CountingToolAdapter(ToolAdapter):
    def __init__(
        self,
        *,
        idempotency: OperationIdempotency = OperationIdempotency.READ_ONLY,
        resource_key: str | None = None,
    ) -> None:
        self.provider_call_count = 0
        self.external_side_effect_count = 0
        self.side_effect_commit_count = 0
        self.compensation_call_count = 0
        self.detached_worker_count = 0
        self.resource_key = resource_key
        self.spec = ToolExecutionSpec(
            tool_name="fault-safe-tool",
            side_effect_kind=(
                ToolSideEffectKind.NONE
                if idempotency is OperationIdempotency.READ_ONLY
                else ToolSideEffectKind.LOCAL_STATE_MUTATION
            ),
            idempotency=idempotency,
            max_concurrency=1,
        )

    def build_invocation(self, text: str = "TOOL_ARGUMENT_SECRET") -> ToolInvocation:
        return ToolInvocation.create(
            tool_name=self.spec.tool_name,
            arguments={"text": text},
            resource_key=self.resource_key,
        )

    def invoke_once(self, invocation, context):
        self.provider_call_count += 1
        if self.spec.side_effect_kind is not ToolSideEffectKind.NONE:
            context.before_side_effect()
            self.external_side_effect_count += 1
            self.side_effect_commit_count += 1
        return ToolAdapterResponse(
            "ok",
            "text/plain",
            "ok",
            side_effect_state=(
                ToolSideEffectState.NOT_STARTED
                if self.spec.side_effect_kind is ToolSideEffectKind.NONE
                else ToolSideEffectState.COMMITTED
            ),
        )


class PhaseBarrier:
    """Test-only in-memory barrier for one Adapter phase."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()


class PhaseAwareToolAdapter(ToolAdapter):
    """In-memory-only fake that exposes the real provider/side-effect phases."""

    def __init__(
        self,
        *,
        idempotency: OperationIdempotency = OperationIdempotency.READ_ONLY,
        response_state: ToolSideEffectState | None = None,
        supports_replay: bool = False,
        idempotency_key: str | None = None,
        barriers: Mapping[str, PhaseBarrier] | None = None,
    ) -> None:
        self.provider_entered_count = 0
        self.before_side_effect_called_count = 0
        self.side_effect_marker_committed_count = 0
        self.external_effect_applied_count = 0
        self.provider_returned_count = 0
        self.compensation_called_count = 0
        self.detached_worker_count = 0
        self._response_state = response_state
        self._idempotency_key = idempotency_key
        self._barriers = dict(barriers or {})
        self._committed_keys: set[str] = set()
        self.spec = ToolExecutionSpec(
            tool_name="phase-aware-tool",
            side_effect_kind=(
                ToolSideEffectKind.NONE
                if idempotency is OperationIdempotency.READ_ONLY
                else ToolSideEffectKind.LOCAL_STATE_MUTATION
            ),
            idempotency=idempotency,
            supports_idempotency_replay=supports_replay,
            max_concurrency=1,
        )

    def build_invocation(
        self,
        text: str = "TOOL_ARGUMENT_SECRET",
        *,
        requested_timeout_seconds: float | None = None,
    ) -> ToolInvocation:
        return ToolInvocation.create(
            tool_name=self.spec.tool_name,
            arguments={"text": text},
            idempotency_key=self._idempotency_key,
            requested_timeout_seconds=requested_timeout_seconds,
        )

    def _cross(self, phase: str, context) -> None:
        barrier = self._barriers.get(phase)
        if barrier is None:
            return
        barrier.entered.set()
        while not barrier.release.wait(0.01):
            context.raise_if_cancelled()

    def invoke_once(self, invocation, context):
        self.provider_entered_count += 1
        self._cross("provider_entered", context)
        replayed = bool(
            self.spec.supports_idempotency_replay
            and invocation.idempotency_key
            and invocation.idempotency_key in self._committed_keys
        )
        state = ToolSideEffectState.NOT_STARTED
        if replayed:
            state = ToolSideEffectState.COMMITTED
        elif self.spec.side_effect_kind is not ToolSideEffectKind.NONE:
            self.before_side_effect_called_count += 1
            self._cross("before_side_effect", context)
            context.before_side_effect()
            self.side_effect_marker_committed_count += 1
            self._cross("side_effect_marker_committed", context)
            self.external_effect_applied_count += 1
            state = ToolSideEffectState.COMMITTED
            if invocation.idempotency_key:
                self._committed_keys.add(invocation.idempotency_key)
            self._cross("external_effect_applied", context)
        if self._response_state is not None:
            state = self._response_state
        self.provider_returned_count += 1
        self._cross("provider_returned", context)
        return ToolAdapterResponse(
            "ok",
            "text/plain",
            "ok",
            side_effect_state=state,
            idempotency_replayed=replayed,
            side_effect_state_authoritative=True,
        )


def make_context(*, max_tool_calls: int = 4, max_retries: int = 3):
    context, cancellation_source = create_run_context(
        entry_agent_id="test", timeout_seconds=2
    )
    context.attach_budget_ledger(
        BudgetLedger(
            RunBudget(max_tool_calls=max_tool_calls, max_retries=max_retries)
        )
    )
    return context, cancellation_source


def make_controller(
    point: FaultPoint,
    code: InjectedFaultCode = InjectedFaultCode.INJECTED_PERMANENT_FAILURE,
    *,
    action: FaultAction = FaultAction.RAISE_TYPED_ERROR,
    max_hits: int = 1,
    enabled: bool = True,
    invocation_id_digest: str | None = None,
) -> FaultInjectionController:
    rule = FaultRule(
        rule_id="tool-fault",
        fault_point=point,
        action=action,
        trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.ATTEMPT_SCOPE,
        max_hits=max_hits,
        component="tool",
        invocation_id_digest=invocation_id_digest,
        safe_fault_code=(
            code if action is FaultAction.RAISE_TYPED_ERROR else None
        ),
        delay_seconds=0.001 if action is FaultAction.DELAY else None,
        dangerous_window=point in {
            FaultPoint.TOOL_BEFORE_SIDE_EFFECT_COMMIT,
            FaultPoint.TOOL_AFTER_PROVIDER_RETURN,
            FaultPoint.TOOL_AFTER_SIDE_EFFECT_COMMIT,
            FaultPoint.TOOL_BEFORE_COMPLETION_EVENT,
        },
    )
    return FaultInjectionController(
        FaultPlan("tool-plan", (rule,), created_at=NOW), enabled=enabled
    )


def zero_side_effects(adapter: CountingToolAdapter) -> tuple[int, ...]:
    return (
        adapter.provider_call_count,
        adapter.external_side_effect_count,
        adapter.side_effect_commit_count,
        adapter.compensation_call_count,
        adapter.detached_worker_count,
    )
