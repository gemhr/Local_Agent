from __future__ import annotations

import pytest

from core.runtime import (
    FaultPoint,
    InMemorySpanRecorder,
    InjectedFaultCode,
    OperationIdempotency,
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


async def execute_with_events(adapter, context, controller=None):
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
    recorder = InMemorySpanRecorder()
    result = await ToolExecutionService(span_recorder=recorder).execute(
        invocation=adapter.build_invocation(),
        adapter=adapter,
        run_context=context,
        step_id="step",
        event_emitter=emitter,
        fault_controller=controller,
    )
    await channel.close()
    events = [event async for event in channel]
    return result, events, recorder


@pytest.mark.asyncio
async def test_post_provider_fault_pairs_events_and_preserves_safe_evidence():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    context, _ = make_context()
    result, events, recorder = await execute_with_events(
        adapter,
        context,
        make_controller(
            FaultPoint.TOOL_AFTER_PROVIDER_RETURN,
            InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
        ),
    )

    assert isinstance(result, ToolExecutionError)
    assert [event.event_type for event in events] == [
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
    ]
    completed = events[-1].payload
    assert completed.provider_started is True
    assert completed.side_effect_state == ToolSideEffectState.COMMITTED.value
    assert completed.retry_disposition == "UNSAFE"
    assert completed.outcome_classification == "POST_COMMIT_RESPONSE_FAILURE"
    assert completed.execution_detached is False
    assert completed.worker_terminated is True
    safe = str(events[-1].to_safe_dict())
    assert "tool-fault" not in safe
    assert "TOOL_ARGUMENT_SECRET" not in safe
    assert "stable-key" not in safe
    assert recorder.health_snapshot().active_span_count == 0


@pytest.mark.asyncio
async def test_disabled_after_return_controller_has_no_behavior_or_counter_effect():
    baseline_adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    disabled_adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    baseline_context, _ = make_context()
    disabled_context, _ = make_context()
    baseline, baseline_events, _ = await execute_with_events(
        baseline_adapter, baseline_context
    )
    controller = make_controller(
        FaultPoint.TOOL_AFTER_PROVIDER_RETURN, enabled=False
    )
    disabled, disabled_events, _ = await execute_with_events(
        disabled_adapter, disabled_context, controller
    )

    assert baseline.status is disabled.status
    assert baseline.side_effect_state is disabled.side_effect_state
    assert baseline.output.content == disabled.output.content
    assert baseline_adapter.external_effect_applied_count == 1
    assert disabled_adapter.external_effect_applied_count == 1
    assert [event.event_type for event in baseline_events] == [
        event.event_type for event in disabled_events
    ]
    counter = controller.snapshot().counters[0]
    assert counter.match_count == 0
    assert counter.hit_count == 0


@pytest.mark.asyncio
async def test_faulted_run_and_committed_invocation_do_not_pollute_next_run():
    first = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    second = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    first_context, _ = make_context()
    second_context, _ = make_context()
    controller = make_controller(FaultPoint.TOOL_AFTER_PROVIDER_RETURN)
    first_result = await ToolExecutionService().execute(
        invocation=first.build_invocation(),
        adapter=first,
        run_context=first_context,
        step_id="step-a",
        fault_controller=controller,
    )
    second_result = await ToolExecutionService().execute(
        invocation=second.build_invocation(),
        adapter=second,
        run_context=second_context,
        step_id="step-b",
    )

    assert isinstance(first_result, ToolExecutionError)
    assert first_result.side_effect_state is ToolSideEffectState.COMMITTED
    assert second_result.side_effect_state is ToolSideEffectState.COMMITTED
    assert first.external_effect_applied_count == 1
    assert second.external_effect_applied_count == 1
