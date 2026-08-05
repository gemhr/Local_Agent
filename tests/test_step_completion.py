"""StepResultCommitter: minimal result completion skeleton failure mapping."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from core.runtime import (
    AgentState,
    AgentStateMachine,
    CancellationSource,
    InMemoryRunEventJournal,
    ResultContentType,
    RunEventEmitter,
    RunEventType,
    RunStateEvent,
    RuntimeEventChannel,
    StepClaim,
    StepCommitStatus,
    StepCompletionErrorCode,
    StepEventType,
    StepResult,
    StepResultCommitter,
    StepResultStore,
    StepResultStoreError,
    StepResultStoreErrorCode,
    StepStateEvent,
    TaskCapabilityRequirements,
    create_run_context,
)
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.planning import (
    ExecutionKind,
    OutputPolicy,
    Plan,
    PlanSource,
    PlanStep,
)
from tests._runtime_assembly_fixtures import FakeDispatcher


def build_shape2_plan():
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


def make_state(plan, *, running: tuple[str, ...] = (), succeeded: tuple[str, ...] = ()):
    state = AgentState.for_run_context("run-1")
    machine = AgentStateMachine()
    for step in plan.steps:
        machine.register_plan_step(state, step_id=step.step_id, name=step.title)
    machine.apply_run_event(state, RunStateEvent(RunEventType.STARTED))
    for step_id in running:
        machine.apply_step_event(
            state,
            StepStateEvent(StepEventType.STARTED, step_id, occurred_at=datetime.now(UTC)),
        )
    for step_id in succeeded:
        machine.apply_step_event(
            state,
            StepStateEvent(StepEventType.STARTED, step_id, occurred_at=datetime.now(UTC)),
        )
        machine.apply_step_event(
            state,
            StepStateEvent(StepEventType.SUCCEEDED, step_id, occurred_at=datetime.now(UTC)),
        )
    return state, machine


def claim_for(plan, step_id: str) -> StepClaim:
    step = next(item for item in plan.steps if item.step_id == step_id)
    return StepClaim(
        plan.plan_id,
        plan.version,
        step_id,
        datetime.now(UTC),
        step.capability_requirements,
        step.preferred_agent,
    )


def make_committer(plan, *, event_emitter=None):
    store = StepResultStore(plan, run_id="run-1")
    return store, StepResultCommitter(
        store=store,
        state_machine=AgentStateMachine(),
        event_emitter=event_emitter,
        plan=plan,
    )


async def make_emitter(*, aborted: bool = False):
    context, source = create_run_context(entry_agent_id="test")
    channel = RuntimeEventChannel(
        8,
        run_id=context.run_id,
        cancellation_token=source.token,
        journal=InMemoryRunEventJournal(),
        observability_dispatcher=FakeDispatcher(),
    )
    if aborted:
        await channel.abort()
    return RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    )


@pytest.mark.asyncio
async def test_successful_internal_commit_order() -> None:
    plan = build_shape2_plan()
    state, _ = make_state(plan, running=("task-code",))
    store, committer = make_committer(
        plan, event_emitter=await make_emitter()
    )
    result = StepResult(
        "task-code", "code_expert", ResultContentType.TEXT, "ok"
    )
    completion = await committer.commit(claim_for(plan, "task-code"), result, state)
    assert completion.succeeded is True
    assert completion.commit_status is StepCommitStatus.COMMITTED
    assert completion.final_result_ready is False
    assert completion.error_code is None
    assert store.has_readable("task-code") is True
    assert state.steps["task-code"].status.value == "SUCCEEDED"


@pytest.mark.asyncio
async def test_synthesis_commit_reports_final_result_ready() -> None:
    plan = build_shape2_plan()
    state, _ = make_state(plan, running=("synthesis",))
    store, committer = make_committer(
        plan, event_emitter=await make_emitter()
    )
    result = StepResult(
        "synthesis", "synthesis_agent", ResultContentType.TEXT, "final"
    )
    completion = await committer.commit(claim_for(plan, "synthesis"), result, state)
    assert completion.succeeded is True
    assert completion.final_result_ready is True
    assert store.has_readable("synthesis") is True


@pytest.mark.asyncio
async def test_invalid_result_marks_step_failed() -> None:
    plan = build_shape2_plan()
    state, _ = make_state(plan, running=("task-code",))
    _, committer = make_committer(
        plan, event_emitter=await make_emitter()
    )
    wrong = StepResult(
        "synthesis", "synthesis_agent", ResultContentType.TEXT, "wrong"
    )
    completion = await committer.commit(claim_for(plan, "task-code"), wrong, state)
    assert (
        completion.error_code
        == StepCompletionErrorCode.STEP_RESULT_INVALID.value
    )
    assert state.steps["task-code"].status.value == "FAILED"


@pytest.mark.asyncio
async def test_incomplete_result_fails_closed() -> None:
    plan = build_shape2_plan()
    state, _ = make_state(plan, running=("task-code",))
    _, committer = make_committer(
        plan, event_emitter=await make_emitter()
    )
    result = StepResult(
        "task-code",
        "code_expert",
        ResultContentType.TEXT,
        "partial",
        complete=False,
    )
    completion = await committer.commit(claim_for(plan, "task-code"), result, state)
    assert (
        completion.error_code
        == StepCompletionErrorCode.STEP_RESULT_INVALID.value
    )


@pytest.mark.asyncio
async def test_prepare_failure_maps_to_step_result_prepare_failed() -> None:
    plan = build_shape2_plan()
    state, _ = make_state(plan, running=("task-code",))
    store, committer = make_committer(
        plan, event_emitter=await make_emitter()
    )
    with patch.object(
        store,
        "write_prepared",
        side_effect=StepResultStoreError(
            StepResultStoreErrorCode.CAPACITY_EXCEEDED,
            "capacity",
        ),
    ):
        completion = await committer.commit(
            claim_for(plan, "task-code"),
            StepResult("task-code", "code_expert", ResultContentType.TEXT, "ok"),
            state,
        )
    assert (
        completion.error_code
        == StepCompletionErrorCode.STEP_RESULT_PREPARE_FAILED.value
    )
    assert state.steps["task-code"].status.value == "FAILED"


@pytest.mark.asyncio
async def test_state_commit_failure_leaves_step_running() -> None:
    plan = build_shape2_plan()
    # Step is PENDING, not RUNNING, so RUNNING -> SUCCEEDED is invalid.
    state, _ = make_state(plan)
    store, committer = make_committer(
        plan, event_emitter=await make_emitter()
    )
    completion = await committer.commit(
        claim_for(plan, "task-code"),
        StepResult("task-code", "code_expert", ResultContentType.TEXT, "ok"),
        state,
    )
    assert (
        completion.error_code
        == StepCompletionErrorCode.STEP_STATE_COMMIT_FAILED.value
    )
    assert state.steps["task-code"].status.value == "PENDING"
    assert store.has_readable("task-code") is False


@pytest.mark.asyncio
async def test_mark_readable_failure_keeps_step_succeeded() -> None:
    plan = build_shape2_plan()
    state, _ = make_state(plan, running=("task-code",))
    store, committer = make_committer(
        plan, event_emitter=await make_emitter()
    )
    with patch.object(
        store,
        "mark_readable",
        side_effect=StepResultStoreError(
            StepResultStoreErrorCode.PRODUCER_NOT_SUCCEEDED,
            "not succeeded",
        ),
    ):
        completion = await committer.commit(
            claim_for(plan, "task-code"),
            StepResult("task-code", "code_expert", ResultContentType.TEXT, "ok"),
            state,
        )
    assert (
        completion.error_code
        == StepCompletionErrorCode.STEP_RESULT_COMMIT_FAILED.value
    )
    assert state.steps["task-code"].status.value == "SUCCEEDED"
    assert store.has_readable("task-code") is False


@pytest.mark.asyncio
async def test_step_completed_event_failure_is_reported() -> None:
    plan = build_shape2_plan()
    state, _ = make_state(plan, running=("task-code",))
    store, committer = make_committer(
        plan, event_emitter=await make_emitter(aborted=True)
    )
    completion = await committer.commit(
        claim_for(plan, "task-code"),
        StepResult("task-code", "code_expert", ResultContentType.TEXT, "ok"),
        state,
    )
    assert (
        completion.error_code
        == StepCompletionErrorCode.STEP_COMPLETION_EVENT_FAILED.value
    )
    assert completion.commit_status is StepCommitStatus.COMMITTED
    assert completion.event_emitted is False
    assert store.has_readable("task-code") is True


@pytest.mark.asyncio
async def test_duplicate_commit_is_rejected() -> None:
    plan = build_shape2_plan()
    state, _ = make_state(plan, running=("task-code",))
    _, committer = make_committer(
        plan, event_emitter=await make_emitter()
    )
    result = StepResult(
        "task-code", "code_expert", ResultContentType.TEXT, "ok"
    )
    claim = claim_for(plan, "task-code")
    first = await committer.commit(claim, result, state)
    second = await committer.commit(claim, result, state)
    assert first.succeeded is True
    assert (
        second.error_code
        == StepCompletionErrorCode.STEP_RESULT_DUPLICATE_COMMIT.value
    )


@pytest.mark.asyncio
async def test_late_commit_after_seal_is_rejected() -> None:
    plan = build_shape2_plan()
    state, _ = make_state(plan, running=("task-code",))
    store, committer = make_committer(
        plan, event_emitter=await make_emitter()
    )
    store.seal()
    completion = await committer.commit(
        claim_for(plan, "task-code"),
        StepResult("task-code", "code_expert", ResultContentType.TEXT, "ok"),
        state,
    )
    assert (
        completion.error_code
        == StepCompletionErrorCode.STEP_RESULT_LATE_COMMIT.value
    )


@pytest.mark.asyncio
async def test_safe_completion_result_never_carries_raw_content() -> None:
    plan = build_shape2_plan()
    state, _ = make_state(plan, running=("task-code",))
    _, committer = make_committer(
        plan, event_emitter=await make_emitter()
    )
    secret = "SECRET_SPECIALIST_RESULT_DO_NOT_PERSIST"
    completion = await committer.commit(
        claim_for(plan, "task-code"),
        StepResult("task-code", "code_expert", ResultContentType.TEXT, secret),
        state,
    )
    rendered = repr(completion)
    assert secret not in rendered
    assert secret not in str(completion)
