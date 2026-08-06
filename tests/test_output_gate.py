"""OutputGate state machine, authorization and at-most-once contract."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.runtime import (
    AgentState,
    AgentStateMachine,
    CancellationSource,
    DeliveryStatus,
    InMemoryRunEventJournal,
    OutputGate,
    OutputGateErrorCode,
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
from core.runtime.planning import (
    ExecutionKind,
    OutputPolicy,
    Plan,
    PlanSource,
    PlanStep,
)
from tests._runtime_assembly_fixtures import FakeDispatcher


def build_shape2_plan() -> Plan:
    capabilities = TaskCapabilityRequirements()
    return Plan(
        "shape2-plan",
        1,
        "shape2",
        (
            PlanStep(
                "task-code",
                "code",
                "c",
                (),
                "done",
                "code_expert",
                capabilities,
                ExecutionKind.AGENT,
                OutputPolicy.INTERNAL,
            ),
            PlanStep(
                "synthesis",
                "synthesis",
                "s",
                ("task-code",),
                "done",
                "synthesis_agent",
                capabilities,
                ExecutionKind.SYNTHESIS,
                OutputPolicy.FINAL_SYNTHESIS,
            ),
        ),
        datetime.now(UTC),
        PlanSource.DETERMINISTIC,
    )


def make_state(plan: Plan, *, running: tuple[str, ...] = ()) -> AgentState:
    state = AgentState.for_run_context("run-1")
    machine = AgentStateMachine()
    for step in plan.steps:
        machine.register_plan_step(state, step_id=step.step_id, name=step.title)
    machine.apply_run_event(state, RunStateEvent(RunEventType.STARTED))
    for step_id in running:
        machine.apply_step_event(
            state,
            StepStateEvent(
                StepEventType.STARTED, step_id, occurred_at=datetime.now(UTC)
            ),
        )
    return state


def claim_for(plan: Plan, step_id: str) -> StepClaim:
    step = next(item for item in plan.steps if item.step_id == step_id)
    return StepClaim(
        plan.plan_id,
        plan.version,
        step_id,
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
    run_active=lambda: True,
) -> tuple[OutputGate, StepResultStore]:
    store = StepResultStore(plan, run_id="run-1")
    gate = OutputGate(
        plan=plan,
        store=store,
        event_emitter=emitter,
        state_getter=lambda: state,
        run_active=run_active,
    )
    return gate, store


def commit_readable(
    store: StepResultStore,
    state: AgentState,
    plan: Plan,
    step_id: str,
    content: str = "final",
) -> None:
    plan_step = next(item for item in plan.steps if item.step_id == step_id)
    machine = AgentStateMachine()
    machine.apply_step_event(
        state,
        StepStateEvent(
            StepEventType.SUCCEEDED,
            step_id,
            occurred_at=datetime.now(UTC),
        ),
    )
    store.write_prepared(
        StepResult(
            step_id,
            plan_step.preferred_agent,
            ResultContentType.TEXT,
            content,
        ),
        expected_agent_id=plan_step.preferred_agent,
    )
    store.mark_readable(step_id, state)


@pytest.mark.asyncio
async def test_success_publish_moves_to_published() -> None:
    plan = build_shape2_plan()
    state = make_state(plan, running=("synthesis",))
    emitter = await make_emitter()
    gate, store = make_gate(plan, emitter=emitter, state=state)
    commit_readable(store, state, plan, "synthesis", "FINAL-CANDIDATE")

    attempt = await gate.attempt_publish(
        claim=claim_for(plan, "synthesis"),
        result=StepResult(
            "synthesis",
            "synthesis_agent",
            ResultContentType.TEXT,
            "FINAL-CANDIDATE",
        ),
    )

    assert attempt.delivery_status is DeliveryStatus.DELIVERED
    assert attempt.error_code is None
    assert gate.state is OutputGateState.PUBLISHED
    assert gate.terminal is True
    iterator = emitter.channel.__aiter__()
    event = await anext(iterator)
    assert event.event_type.value == "OUTPUT_DELTA"
    assert event.payload.text == "FINAL-CANDIDATE"
    await iterator.aclose()
    assert repr(gate) and "FINAL-CANDIDATE" not in repr(gate)
    await emitter.channel.abort()


@pytest.mark.asyncio
async def test_internal_step_is_rejected() -> None:
    plan = build_shape2_plan()
    state = make_state(plan, running=("task-code",))
    emitter = await make_emitter()
    gate, store = make_gate(plan, emitter=emitter, state=state)
    commit_readable(store, state, plan, "task-code", "internal")

    attempt = await gate.attempt_publish(
        claim=claim_for(plan, "task-code"),
        result=StepResult(
            "task-code",
            "code_expert",
            ResultContentType.TEXT,
            "internal",
        ),
    )

    assert attempt.delivery_status is DeliveryStatus.FAILED
    assert attempt.error_code == OutputGateErrorCode.OUTPUT_GATE_INTERNAL_STEP.value
    assert gate.state is OutputGateState.NOT_STARTED
    await emitter.channel.abort()


@pytest.mark.asyncio
async def test_step_not_succeeded_is_rejected() -> None:
    plan = build_shape2_plan()
    state = make_state(plan, running=("synthesis",))
    emitter = await make_emitter()
    gate, store = make_gate(plan, emitter=emitter, state=state)

    attempt = await gate.attempt_publish(
        claim=claim_for(plan, "synthesis"),
        result=StepResult(
            "synthesis",
            "synthesis_agent",
            ResultContentType.TEXT,
            "final",
        ),
    )

    assert attempt.delivery_status is DeliveryStatus.FAILED
    assert (
        attempt.error_code
        == OutputGateErrorCode.OUTPUT_GATE_STEP_NOT_SUCCEEDED.value
    )
    assert gate.state is OutputGateState.NOT_STARTED
    await emitter.channel.abort()


@pytest.mark.asyncio
async def test_store_not_readable_is_rejected() -> None:
    plan = build_shape2_plan()
    state = make_state(plan, running=("synthesis",))
    emitter = await make_emitter()
    gate, store = make_gate(plan, emitter=emitter, state=state)
    machine = AgentStateMachine()
    machine.apply_step_event(
        state,
        StepStateEvent(
            StepEventType.SUCCEEDED,
            "synthesis",
            occurred_at=datetime.now(UTC),
        ),
    )

    attempt = await gate.attempt_publish(
        claim=claim_for(plan, "synthesis"),
        result=StepResult(
            "synthesis",
            "synthesis_agent",
            ResultContentType.TEXT,
            "final",
        ),
    )

    assert attempt.delivery_status is DeliveryStatus.FAILED
    assert (
        attempt.error_code
        == OutputGateErrorCode.OUTPUT_GATE_STORE_NOT_READABLE.value
    )
    await emitter.channel.abort()


@pytest.mark.asyncio
async def test_store_sealed_is_rejected() -> None:
    plan = build_shape2_plan()
    state = make_state(plan, running=("synthesis",))
    emitter = await make_emitter()
    gate, store = make_gate(plan, emitter=emitter, state=state)
    commit_readable(store, state, plan, "synthesis")
    store.seal()

    attempt = await gate.attempt_publish(
        claim=claim_for(plan, "synthesis"),
        result=StepResult(
            "synthesis",
            "synthesis_agent",
            ResultContentType.TEXT,
            "final",
        ),
    )

    assert attempt.delivery_status is DeliveryStatus.FAILED
    assert attempt.error_code == OutputGateErrorCode.OUTPUT_GATE_STORE_SEALED.value
    await emitter.channel.abort()


@pytest.mark.asyncio
async def test_run_not_active_is_rejected() -> None:
    plan = build_shape2_plan()
    state = make_state(plan, running=("synthesis",))
    emitter = await make_emitter()
    gate, store = make_gate(
        plan, emitter=emitter, state=state, run_active=lambda: False
    )
    commit_readable(store, state, plan, "synthesis")

    attempt = await gate.attempt_publish(
        claim=claim_for(plan, "synthesis"),
        result=StepResult(
            "synthesis",
            "synthesis_agent",
            ResultContentType.TEXT,
            "final",
        ),
    )

    assert attempt.delivery_status is DeliveryStatus.FAILED
    assert attempt.error_code == OutputGateErrorCode.OUTPUT_GATE_RUN_NOT_ACTIVE.value
    await emitter.channel.abort()


@pytest.mark.asyncio
async def test_duplicate_after_published_fails_closed() -> None:
    plan = build_shape2_plan()
    state = make_state(plan, running=("synthesis",))
    emitter = await make_emitter()
    gate, store = make_gate(plan, emitter=emitter, state=state)
    commit_readable(store, state, plan, "synthesis")
    result = StepResult(
        "synthesis",
        "synthesis_agent",
        ResultContentType.TEXT,
        "final",
    )

    first = await gate.attempt_publish(
        claim=claim_for(plan, "synthesis"), result=result
    )
    second = await gate.attempt_publish(
        claim=claim_for(plan, "synthesis"), result=result
    )

    assert first.delivery_status is DeliveryStatus.DELIVERED
    assert second.delivery_status is DeliveryStatus.FAILED
    assert (
        second.error_code
        == OutputGateErrorCode.OUTPUT_GATE_DUPLICATE_ATTEMPT.value
    )
    assert gate.state is OutputGateState.PUBLISHED
    iterator = emitter.channel.__aiter__()
    first_event = await anext(iterator)
    assert first_event.event_type.value == "OUTPUT_DELTA"
    await iterator.aclose()
    await emitter.channel.abort()


@pytest.mark.asyncio
async def test_concurrent_duplicate_attempts_allow_only_one_publish() -> None:
    plan = build_shape2_plan()
    state = make_state(plan, running=("synthesis",))
    emitter = await make_emitter()
    gate, store = make_gate(plan, emitter=emitter, state=state)
    commit_readable(store, state, plan, "synthesis")
    result = StepResult(
        "synthesis",
        "synthesis_agent",
        ResultContentType.TEXT,
        "final",
    )

    attempts = await __import__("asyncio").gather(
        gate.attempt_publish(claim=claim_for(plan, "synthesis"), result=result),
        gate.attempt_publish(claim=claim_for(plan, "synthesis"), result=result),
    )
    delivered = [
        attempt
        for attempt in attempts
        if attempt.delivery_status is DeliveryStatus.DELIVERED
    ]
    rejected = [
        attempt
        for attempt in attempts
        if attempt.delivery_status is DeliveryStatus.FAILED
    ]
    assert len(delivered) == 1
    assert len(rejected) == 1
    assert (
        rejected[0].error_code
        == OutputGateErrorCode.OUTPUT_GATE_DUPLICATE_ATTEMPT.value
    )
    iterator = emitter.channel.__aiter__()
    first_event = await anext(iterator)
    assert first_event.event_type.value == "OUTPUT_DELTA"
    await iterator.aclose()
    await emitter.channel.abort()


@pytest.mark.asyncio
async def test_close_rejects_later_publish() -> None:
    plan = build_shape2_plan()
    state = make_state(plan, running=("synthesis",))
    emitter = await make_emitter()
    gate, store = make_gate(plan, emitter=emitter, state=state)
    commit_readable(store, state, plan, "synthesis")
    gate.close()

    attempt = await gate.attempt_publish(
        claim=claim_for(plan, "synthesis"),
        result=StepResult(
            "synthesis",
            "synthesis_agent",
            ResultContentType.TEXT,
            "final",
        ),
    )

    assert attempt.delivery_status is DeliveryStatus.FAILED
    assert (
        attempt.error_code
        == OutputGateErrorCode.OUTPUT_GATE_DUPLICATE_ATTEMPT.value
    )
    await emitter.channel.abort()


@pytest.mark.asyncio
async def test_repr_is_safe() -> None:
    plan = build_shape2_plan()
    state = make_state(plan, running=("synthesis",))
    emitter = await make_emitter()
    gate, _store = make_gate(plan, emitter=emitter, state=state)
    rendered = repr(gate)
    assert "FINAL" not in rendered
    assert "synthesis_agent" in rendered or "final_step_id" in rendered
    await emitter.channel.abort()
