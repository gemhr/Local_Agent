from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.runtime import (
    AgentState,
    AgentStateMachine,
    CancellationSource,
    DeliveryStatus,
    InMemoryMetricsRecorder,
    InMemoryRunEventJournal,
    InMemorySpanRecorder,
    OutputGate,
    OutputGateState,
    ResultContentType,
    RunEventEmitter,
    RunEventType,
    RunStateEvent,
    RuntimeEventChannel,
    StepClaim,
    StepEventType,
    StepResult,
    StepResultStore,
    StepStateEvent,
    TaskCapabilityRequirements,
    create_run_context,
)
from core.runtime.event_channel import (
    EventChannelClosedError,
    EventPublicationError,
    EventPublicationEvidence,
    EventPublicationStage,
)
from core.runtime.fault_injection_contract import (
    FaultPoint,
    InjectedFaultCode,
)
from core.runtime.planning import (
    ExecutionKind,
    OutputPolicy,
    Plan,
    PlanSource,
    PlanStep,
)
from core.runtime.trace_contract import RUNTIME_OUTPUT_DELIVERY_SPAN
from tests._runtime_assembly_fixtures import FakeDispatcher


def build_plan() -> Plan:
    capabilities = TaskCapabilityRequirements()
    return Plan(
        "plan",
        1,
        "summary",
        (
            PlanStep(
                "answer",
                "answer",
                "desc",
                (),
                "done",
                "knowledge_expert",
                capabilities,
                ExecutionKind.AGENT,
                OutputPolicy.FINAL_PASSTHROUGH,
            ),
        ),
        datetime.now(UTC),
        PlanSource.DETERMINISTIC,
    )


def make_state(plan: Plan) -> AgentState:
    state = AgentState.for_run_context("run-1")
    machine = AgentStateMachine()
    for step in plan.steps:
        machine.register_plan_step(state, step_id=step.step_id, name=step.title)
    machine.apply_run_event(state, RunStateEvent(RunEventType.STARTED))
    machine.apply_step_event(
        state,
        StepStateEvent(
            StepEventType.STARTED,
            "answer",
            occurred_at=datetime.now(UTC),
        ),
    )
    machine.apply_step_event(
        state,
        StepStateEvent(
            StepEventType.SUCCEEDED,
            "answer",
            occurred_at=datetime.now(UTC),
        ),
    )
    return state


def claim_for(plan: Plan) -> StepClaim:
    step = plan.steps[0]
    return StepClaim(
        plan.plan_id,
        plan.version,
        step.step_id,
        datetime.now(UTC),
        step.capability_requirements,
        step.preferred_agent,
    )


async def make_emitter():
    context, source = create_run_context(entry_agent_id="test")
    channel = RuntimeEventChannel(
        8,
        run_id=context.run_id,
        cancellation_token=source.token,
        journal=InMemoryRunEventJournal(),
        observability_dispatcher=FakeDispatcher(),
    )
    return RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    )


def make_gate(
    plan: Plan,
    *,
    emitter,
    state: AgentState,
    span_recorder=None,
    metrics_recorder=None,
):
    store = StepResultStore(plan, run_id="run-1")
    plan_step = plan.steps[0]
    store.write_prepared(
        StepResult(
            plan_step.step_id,
            plan_step.preferred_agent,
            ResultContentType.TEXT,
            "FINAL-CANDIDATE",
        ),
        expected_agent_id=plan_step.preferred_agent,
    )
    store.mark_readable(plan_step.step_id, state)
    gate = OutputGate(
        plan=plan,
        store=store,
        event_emitter=emitter,
        state_getter=lambda: state,
        run_active=lambda: True,
        span_recorder=span_recorder,
        metrics_recorder=metrics_recorder,
    )
    return gate, store


@pytest.mark.asyncio
async def test_delivered_publish_records_span_and_metrics():
    plan = build_plan()
    state = make_state(plan)
    emitter = await make_emitter()
    recorder = InMemorySpanRecorder()
    metrics = InMemoryMetricsRecorder()
    gate, _ = make_gate(
        plan,
        emitter=emitter,
        state=state,
        span_recorder=recorder,
        metrics_recorder=metrics,
    )
    attempt = await gate.attempt_publish(
        claim=claim_for(plan),
        result=StepResult(
            "answer",
            "knowledge_expert",
            ResultContentType.TEXT,
            "FINAL-CANDIDATE",
        ),
    )
    assert attempt.delivery_status is DeliveryStatus.DELIVERED
    assert gate.state is OutputGateState.PUBLISHED

    delivery_spans = [
        record
        for record in recorder.snapshot()
        if record.operation == RUNTIME_OUTPUT_DELIVERY_SPAN
    ]
    assert len(delivery_spans) == 1
    span = delivery_spans[0]
    assert span.attributes["delivery_status"] == "DELIVERED"
    assert span.attributes["gate_terminal_state"] == "PUBLISHED"
    assert span.attributes["publish_attempt_count"] == 1
    assert span.attributes["output_char_count"] == len("FINAL-CANDIDATE")
    assert "FINAL-CANDIDATE" not in repr(span)

    snap = metrics.snapshot()
    assert snap.counter(
        "runtime_output_delivery_total",
        {"status": "DELIVERED", "error_code": "OK"},
    ) == 1
    assert snap.histogram(
        "runtime_output_delivery_duration_seconds", {"status": "DELIVERED"}
    )
    await emitter.channel.abort()


@pytest.mark.asyncio
async def test_unknown_partial_persistence_records_unknown_and_partial_metric():
    plan = build_plan()
    state = make_state(plan)
    emitter = await make_emitter()
    recorder = InMemorySpanRecorder()
    metrics = InMemoryMetricsRecorder()
    gate, _ = make_gate(
        plan,
        emitter=emitter,
        state=state,
        span_recorder=recorder,
        metrics_recorder=metrics,
    )

    async def fail_publish(self, claim, result):
        raise EventPublicationError(
            evidence=EventPublicationEvidence(
                event_id="event-1",
                sequence=1,
                event_type="OUTPUT_DELTA",
                publication_stage=EventPublicationStage.AFTER_JOURNAL_APPEND,
                partially_persisted=True,
            ),
            fault_point=FaultPoint.EVENT_AFTER_JOURNAL_APPEND,
            fault_code=InjectedFaultCode.INJECTED_JOURNAL_FAILURE,
        )

    import core.runtime.output_gate as output_gate_module

    original = output_gate_module.OutputGate._publish_output
    output_gate_module.OutputGate._publish_output = fail_publish
    try:
        attempt = await gate.attempt_publish(
            claim=claim_for(plan),
            result=StepResult(
                "answer",
                "knowledge_expert",
                ResultContentType.TEXT,
                "FINAL-CANDIDATE",
            ),
        )
    finally:
        output_gate_module.OutputGate._publish_output = original

    assert (
        attempt.delivery_status is DeliveryStatus.OUTCOME_UNKNOWN
    )
    assert attempt.error_code == "FINAL_OUTPUT_DELIVERY_UNKNOWN"
    snap = metrics.snapshot()
    assert snap.counter(
        "runtime_output_delivery_total",
        {"status": "OUTCOME_UNKNOWN", "error_code": "FINAL_OUTPUT_DELIVERY_UNKNOWN"},
    ) == 1
    assert snap.counter("runtime_output_partial_persisted_total") == 1
    spans = [
        record
        for record in recorder.snapshot()
        if record.operation == RUNTIME_OUTPUT_DELIVERY_SPAN
    ]
    assert spans[0].attributes["partially_persisted"] is True
    assert spans[0].attributes["delivery_status"] == "OUTCOME_UNKNOWN"
    await emitter.channel.abort()


@pytest.mark.asyncio
async def test_failed_publish_records_failed_metric_without_retry():
    plan = build_plan()
    state = make_state(plan)
    emitter = await make_emitter()
    metrics = InMemoryMetricsRecorder()
    gate, _ = make_gate(
        plan,
        emitter=emitter,
        state=state,
        span_recorder=None,
        metrics_recorder=metrics,
    )

    async def fail_publish(self, claim, result):
        raise EventChannelClosedError("closed")

    import core.runtime.output_gate as output_gate_module

    original = output_gate_module.OutputGate._publish_output
    output_gate_module.OutputGate._publish_output = fail_publish
    try:
        attempt = await gate.attempt_publish(
            claim=claim_for(plan),
            result=StepResult(
                "answer",
                "knowledge_expert",
                ResultContentType.TEXT,
                "FINAL-CANDIDATE",
            ),
        )
    finally:
        output_gate_module.OutputGate._publish_output = original

    assert attempt.delivery_status is DeliveryStatus.FAILED
    snap = metrics.snapshot()
    assert snap.counter(
        "runtime_output_delivery_total",
        {"status": "FAILED", "error_code": "FINAL_OUTPUT_DELIVERY_FAILED"},
    ) == 1
    # Gate 已终态，重复尝试必须被拒绝，绝不产生第二次交付。
    duplicate = await gate.attempt_publish(
        claim=claim_for(plan),
        result=StepResult(
            "answer",
            "knowledge_expert",
            ResultContentType.TEXT,
            "FINAL-CANDIDATE",
        ),
    )
    assert duplicate.error_code == "OUTPUT_GATE_DUPLICATE_ATTEMPT"
    assert snap.counter(
        "runtime_output_delivery_total",
        {"status": "FAILED", "error_code": "FINAL_OUTPUT_DELIVERY_FAILED"},
    ) == 1
    await emitter.channel.abort()
