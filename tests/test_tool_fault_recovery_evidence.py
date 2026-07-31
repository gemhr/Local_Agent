from __future__ import annotations

import pytest

from core.runtime import (
    FaultPoint,
    OperationIdempotency,
    RetryDisposition,
    RunEventEmitter,
    RuntimeEventChannel,
    RuntimeEventType,
    ToolExecutionError,
    ToolExecutionService,
    ToolSideEffectState,
)
from tests.tool_fault_test_support import (
    PhaseAwareToolAdapter,
    make_context,
    make_controller,
)


async def completed_payload(adapter, point):
    context, _ = make_context()
    channel = RuntimeEventChannel(
        8,
        run_id=context.run_id,
        cancellation_token=context.cancellation_token,
    )
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    ).for_step("step")
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation(),
        adapter=adapter,
        run_context=context,
        step_id="step",
        event_emitter=emitter,
        fault_controller=make_controller(point),
    )
    await channel.close()
    events = [event async for event in channel]
    assert [event.event_type for event in events] == [
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
    ]
    return result, events[-1].payload


@pytest.mark.asyncio
async def test_before_commit_evidence_says_provider_started_but_not_committed():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    result, completed = await completed_payload(
        adapter, FaultPoint.TOOL_BEFORE_SIDE_EFFECT_COMMIT
    )
    assert isinstance(result, ToolExecutionError)
    assert result.provider_started is True
    assert result.side_effect_state is ToolSideEffectState.NOT_STARTED
    assert completed.provider_started is True
    assert completed.side_effect_state == "NOT_STARTED"
    assert completed.compensation_state == "NOT_ATTEMPTED"
    assert completed.execution_detached is False


@pytest.mark.asyncio
async def test_after_return_evidence_cannot_drop_committed_fact():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    result, completed = await completed_payload(
        adapter, FaultPoint.TOOL_AFTER_PROVIDER_RETURN
    )
    assert isinstance(result, ToolExecutionError)
    assert result.side_effect_state is ToolSideEffectState.COMMITTED
    assert result.retry_disposition is RetryDisposition.UNSAFE
    assert completed.provider_started is True
    assert completed.side_effect_state == "COMMITTED"
    assert completed.outcome_classification == "POST_COMMIT_RESPONSE_FAILURE"
    assert completed.retry_disposition == "UNSAFE"
    assert completed.compensation_state == "NOT_ATTEMPTED"
