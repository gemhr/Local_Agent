import pytest

from core.runtime import (
    FaultPoint,
    InjectedFaultCode,
    OperationIdempotency,
    RetryPolicy,
    ToolExecutionError,
    ToolExecutionService,
)
from core.runtime.retry import RetryExecutor
from tests.tool_fault_test_support import CountingToolAdapter, make_context, make_controller


def service(max_attempts=2):
    return ToolExecutionService(retry_executor=RetryExecutor(RetryPolicy(
        max_attempts=max_attempts, base_delay_seconds=0, max_delay_seconds=0
    )))


@pytest.mark.asyncio
async def test_read_only_transient_uses_existing_retry_owner_only():
    adapter = CountingToolAdapter()
    context, _ = make_context(max_tool_calls=2, max_retries=1)
    result = await service().execute(
        invocation=adapter.build_invocation(), adapter=adapter,
        run_context=context, step_id="step",
        fault_controller=make_controller(
            FaultPoint.TOOL_BEFORE_PROVIDER_CALL,
            InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
        ),
    )
    assert result.output.content == "ok"
    assert adapter.provider_call_count == 1
    usage = context.budget_ledger.snapshot().committed_usage
    # The injected pre-provider attempt releases its Tool-call reservation;
    # only the real Provider attempt commits a call, while retry accounting stays real.
    assert (usage.tool_calls, usage.retries) == (1, 1)


@pytest.mark.asyncio
async def test_non_idempotent_transient_does_not_gain_retry():
    adapter = CountingToolAdapter(idempotency=OperationIdempotency.NON_IDEMPOTENT)
    context, _ = make_context()
    result = await service().execute(
        invocation=adapter.build_invocation(), adapter=adapter,
        run_context=context, step_id="step",
        fault_controller=make_controller(
            FaultPoint.TOOL_BEFORE_PROVIDER_CALL,
            InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
        ),
    )
    assert isinstance(result, ToolExecutionError)
    assert adapter.provider_call_count == 0
    assert context.budget_ledger.snapshot().committed_usage.tool_calls == 0
