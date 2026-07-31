from __future__ import annotations

from datetime import UTC, datetime

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
        return ToolAdapterResponse("ok", "text/plain", "ok")


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
