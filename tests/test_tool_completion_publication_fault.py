from __future__ import annotations

import pytest

from core.runtime import (
    InMemorySpanRecorder,
    FaultPoint,
    OperationIdempotency,
    RunEventEmitter,
    RuntimeActivityTracker,
    RuntimeEventChannel,
    RuntimeEventType,
    ToolErrorCategory,
    ToolAdapterInvocationError,
    ToolExecutionError,
    ToolExecutionPhase,
    ToolExecutionService,
    ToolSideEffectState,
)
from tests.tool_fault_test_support import (
    PhaseAwareToolAdapter,
    make_context,
    make_controller,
)


class BusinessFailureAfterCommitAdapter(PhaseAwareToolAdapter):
    def invoke_once(self, invocation, context):
        self.provider_entered_count += 1
        self.before_side_effect_called_count += 1
        context.before_side_effect()
        self.external_effect_applied_count += 1
        raise ToolAdapterInvocationError(
            category=ToolErrorCategory.TRANSIENT,
            safe_error_code="TOOL_BUSINESS_FAILURE",
            safe_message="provider-secret-error",
            side_effect_state=ToolSideEffectState.COMMITTED,
            side_effect_state_authoritative=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "idempotency",
    [OperationIdempotency.READ_ONLY, OperationIdempotency.NON_IDEMPOTENT],
)
async def test_completion_publication_fault_never_reruns_or_publishes_terminal(
    idempotency,
):
    adapter = PhaseAwareToolAdapter(idempotency=idempotency)
    context, _ = make_context()
    activity = RuntimeActivityTracker(context.run_id)
    context.attach_activity_tracker(activity)
    channel = RuntimeEventChannel(
        8, run_id=context.run_id, cancellation_token=context.cancellation_token
    )
    emitter = RunEventEmitter(
        run_id=context.run_id, trace_id=context.trace_id, channel=channel
    ).for_step("step")
    recorder = InMemorySpanRecorder()
    service = ToolExecutionService(span_recorder=recorder)

    result = await service.execute(
        invocation=adapter.build_invocation(),
        adapter=adapter,
        run_context=context,
        step_id="step",
        event_emitter=emitter,
        fault_controller=make_controller(FaultPoint.TOOL_BEFORE_COMPLETION_EVENT),
    )
    await channel.close()
    events = [event async for event in channel]

    assert isinstance(result, ToolExecutionError)
    assert result.category is ToolErrorCategory.INTERNAL
    assert result.phase is ToolExecutionPhase.EVENT
    assert result.safe_error_code == "TOOL_COMPLETION_PUBLICATION_FAILED"
    expected_state = (
        ToolSideEffectState.NOT_STARTED
        if idempotency is OperationIdempotency.READ_ONLY
        else ToolSideEffectState.COMMITTED
    )
    assert result.side_effect_state is expected_state
    assert result.completion_evidence is not None
    assert result.completion_evidence.side_effect_state == expected_state.value
    assert result.completion_evidence.result_present is True
    assert result.completion_evidence.result_digest is not None
    assert adapter.provider_entered_count == 1
    assert adapter.external_effect_applied_count == (
        0 if idempotency is OperationIdempotency.READ_ONLY else 1
    )
    assert adapter.compensation_called_count == 0
    assert [event.event_type for event in events] == [RuntimeEventType.TOOL_STARTED]
    usage = context.budget_ledger.snapshot().committed_usage
    assert (usage.tool_calls, usage.retries) == (1, 0)
    assert context.budget_ledger.snapshot().active_reservation_count == 0
    assert service.concurrency_controller.active_worker_count == 0
    counts, unknown, _ = activity.counts()
    assert counts["tool_attempts_active"] == 0
    assert unknown is False
    assert recorder.health_snapshot().active_span_count == 0


@pytest.mark.asyncio
async def test_disabled_completion_rule_has_zero_counters_and_normal_terminal():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    context, _ = make_context()
    channel = RuntimeEventChannel(
        8, run_id=context.run_id, cancellation_token=context.cancellation_token
    )
    emitter = RunEventEmitter(
        run_id=context.run_id, trace_id=context.trace_id, channel=channel
    ).for_step("step")
    controller = make_controller(
        FaultPoint.TOOL_BEFORE_COMPLETION_EVENT, enabled=False
    )

    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation(),
        adapter=adapter,
        run_context=context,
        step_id="step",
        event_emitter=emitter,
        fault_controller=controller,
    )
    await channel.close()
    events = [event async for event in channel]

    assert result.output.content == "ok"
    assert [event.event_type for event in events] == [
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
    ]
    counter = controller.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (0, 0)


@pytest.mark.asyncio
async def test_publication_error_keeps_frozen_business_failure_priority():
    adapter = BusinessFailureAfterCommitAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    context, _ = make_context()
    channel = RuntimeEventChannel(
        8, run_id=context.run_id, cancellation_token=context.cancellation_token
    )
    emitter = RunEventEmitter(
        run_id=context.run_id, trace_id=context.trace_id, channel=channel
    ).for_step("step")

    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation(),
        adapter=adapter,
        run_context=context,
        step_id="step",
        event_emitter=emitter,
        fault_controller=make_controller(FaultPoint.TOOL_BEFORE_COMPLETION_EVENT),
    )
    await channel.close()
    events = [event async for event in channel]

    assert result.safe_error_code == "TOOL_COMPLETION_PUBLICATION_FAILED"
    assert result.side_effect_state is ToolSideEffectState.COMMITTED
    assert result.completion_evidence.safe_error_code == "TOOL_BUSINESS_FAILURE"
    assert result.completion_evidence.outcome_classification == "TRANSIENT"
    assert adapter.provider_entered_count == 1
    assert adapter.external_effect_applied_count == 1
    assert adapter.compensation_called_count == 0
    assert [event.event_type for event in events] == [RuntimeEventType.TOOL_STARTED]
    assert "provider-secret-error" not in repr(result)
