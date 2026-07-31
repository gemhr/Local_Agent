from __future__ import annotations

import pytest

from core.runtime import (
    FaultPoint,
    InjectedFaultCode,
    OperationIdempotency,
    RetryDisposition,
    RetryPolicy,
    ToolErrorCategory,
    ToolExecutionError,
    ToolExecutionService,
    ToolSideEffectState,
)
from core.runtime.retry import RetryExecutor
from tests.tool_fault_test_support import (
    PhaseAwareToolAdapter,
    make_context,
    make_controller,
)


def one_attempt_service() -> ToolExecutionService:
    return ToolExecutionService(
        retry_executor=RetryExecutor(
            RetryPolicy(
                max_attempts=1,
                base_delay_seconds=0,
                max_delay_seconds=0,
            )
        )
    )


@pytest.mark.asyncio
async def test_before_side_effect_fault_is_after_provider_and_before_tracker_commit():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    context, _ = make_context()
    service = one_attempt_service()
    result = await service.execute(
        invocation=adapter.build_invocation(),
        adapter=adapter,
        run_context=context,
        step_id="step",
        fault_controller=make_controller(
            FaultPoint.TOOL_BEFORE_SIDE_EFFECT_COMMIT
        ),
    )

    assert isinstance(result, ToolExecutionError)
    assert result.provider_started is True
    assert result.side_effect_state is ToolSideEffectState.NOT_STARTED
    assert result.retry_disposition is RetryDisposition.UNSAFE
    assert adapter.provider_entered_count == 1
    assert adapter.before_side_effect_called_count == 1
    assert adapter.side_effect_marker_committed_count == 0
    assert adapter.external_effect_applied_count == 0
    assert adapter.provider_returned_count == 0
    assert adapter.compensation_called_count == 0
    budget = context.budget_ledger.snapshot()
    assert budget.committed_usage.tool_calls == 1
    assert budget.active_reservation_count == 0
    assert service.concurrency_controller.worker_snapshot()["active_worker_count"] == 0


@pytest.mark.asyncio
async def test_after_provider_read_only_transient_uses_existing_retry_policy():
    adapter = PhaseAwareToolAdapter()
    context, _ = make_context()
    result = await ToolExecutionService(
        retry_executor=RetryExecutor(
            RetryPolicy(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
            )
        )
    ).execute(
        invocation=adapter.build_invocation(),
        adapter=adapter,
        run_context=context,
        step_id="step",
        fault_controller=make_controller(
            FaultPoint.TOOL_AFTER_PROVIDER_RETURN,
            InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
        ),
    )

    assert not isinstance(result, ToolExecutionError)
    assert adapter.provider_entered_count == 2
    assert adapter.provider_returned_count == 2
    assert adapter.external_effect_applied_count == 0
    budget = context.budget_ledger.snapshot()
    assert budget.committed_usage.tool_calls == 2
    assert budget.committed_usage.retries == 1
    assert budget.active_reservation_count == 0


@pytest.mark.asyncio
async def test_after_provider_idempotency_key_replay_does_not_repeat_effect():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.IDEMPOTENT_WITH_KEY,
        supports_replay=True,
        idempotency_key="stable-key",
    )
    context, _ = make_context()
    result = await ToolExecutionService(
        retry_executor=RetryExecutor(
            RetryPolicy(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
            )
        )
    ).execute(
        invocation=adapter.build_invocation(),
        adapter=adapter,
        run_context=context,
        step_id="step",
        fault_controller=make_controller(
            FaultPoint.TOOL_AFTER_PROVIDER_RETURN,
            InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
        ),
    )

    assert not isinstance(result, ToolExecutionError)
    assert result.idempotency_replayed is True
    assert result.side_effect_state is ToolSideEffectState.COMMITTED
    assert adapter.provider_entered_count == 2
    assert adapter.external_effect_applied_count == 1
    assert adapter.compensation_called_count == 0


@pytest.mark.asyncio
async def test_after_provider_non_idempotent_commit_fails_closed_without_retry():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    context, _ = make_context()
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation(),
        adapter=adapter,
        run_context=context,
        step_id="step",
        fault_controller=make_controller(
            FaultPoint.TOOL_AFTER_PROVIDER_RETURN,
            InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
        ),
    )

    assert isinstance(result, ToolExecutionError)
    assert result.category is ToolErrorCategory.POST_COMMIT_RESPONSE_FAILURE
    assert result.safe_error_code == "TOOL_POST_PROVIDER_FAILURE"
    assert result.provider_started is True
    assert result.side_effect_state is ToolSideEffectState.COMMITTED
    assert result.retry_disposition is RetryDisposition.UNSAFE
    assert adapter.provider_entered_count == 1
    assert adapter.provider_returned_count == 1
    assert adapter.external_effect_applied_count == 1
    assert adapter.compensation_called_count == 0


@pytest.mark.asyncio
async def test_after_provider_unknown_outcome_stays_unknown_and_fail_closed():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.UNKNOWN,
        response_state=ToolSideEffectState.UNKNOWN,
    )
    context, _ = make_context()
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation(),
        adapter=adapter,
        run_context=context,
        step_id="step",
        fault_controller=make_controller(
            FaultPoint.TOOL_AFTER_PROVIDER_RETURN,
            InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
        ),
    )

    assert isinstance(result, ToolExecutionError)
    assert result.provider_started is True
    assert result.side_effect_state is ToolSideEffectState.UNKNOWN
    assert result.retry_disposition is RetryDisposition.OUTCOME_UNKNOWN
    assert adapter.provider_entered_count == 1
    assert adapter.external_effect_applied_count == 1
    assert adapter.compensation_called_count == 0
