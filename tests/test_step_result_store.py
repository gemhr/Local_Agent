"""StepResultStore contract: states, once-write, ACL, capacity, lifecycle."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from core.runtime import (
    AgentState,
    AgentStateMachine,
    DelegatedPlanDecision,
    DelegatedTaskDecision,
    PlanCompiler,
    PlanningSource,
    ResultContentType,
    RunEventType,
    RunStateEvent,
    StepClaim,
    StepEventType,
    StepResult,
    StepResultStore,
    StepResultStoreError,
    StepResultStoreErrorCode,
    StepStateEvent,
)
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.planning import (
    ExecutionKind,
    OutputPolicy,
    Plan,
    PlanSource,
    PlanStep,
    TaskCapabilityRequirements,
)


def build_shape3_plan():
    decision = DelegatedPlanDecision(
        tasks=(
            DelegatedTaskDecision(
                "code",
                "code_expert",
                "Inspect the code contract.",
                required_capabilities=frozenset({"code_reasoning"}),
            ),
            DelegatedTaskDecision(
                "knowledge",
                "knowledge_expert",
                "Inspect the knowledge contract.",
                required_capabilities=frozenset({"rag"}),
            ),
        ),
        synthesis_required=True,
    )
    return PlanCompiler(DEFAULT_AGENT_REGISTRY).compile(
        decision, planning_source=PlanningSource.MODEL
    ).plan


def make_state(plan, *, succeeded=(), failed=()):
    state = AgentState.for_run_context("run-1")
    machine = AgentStateMachine()
    for step in plan.steps:
        machine.register_plan_step(state, step_id=step.step_id, name=step.title)
    machine.apply_run_event(state, RunStateEvent(RunEventType.STARTED))
    for step_id in succeeded:
        machine.apply_step_event(
            state,
            StepStateEvent(StepEventType.STARTED, step_id, occurred_at=datetime.now(UTC)),
        )
        machine.apply_step_event(
            state,
            StepStateEvent(StepEventType.SUCCEEDED, step_id, occurred_at=datetime.now(UTC)),
        )
    for step_id in failed:
        machine.apply_step_event(
            state,
            StepStateEvent(StepEventType.STARTED, step_id, occurred_at=datetime.now(UTC)),
        )
        machine.apply_step_event(
            state,
            StepStateEvent(
                StepEventType.FAILED,
                step_id,
                occurred_at=datetime.now(UTC),
                error_code="AGENT_STEP_FAILED",
                error_message="failed",
            ),
        )
    return state


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


def result(step_id: str, agent_id: str, content: str = "ok") -> StepResult:
    return StepResult(
        step_id,
        agent_id,
        ResultContentType.TEXT,
        content,
        complete=True,
    )


@pytest.fixture()
def shape3():
    plan = build_shape3_plan()
    return plan


def test_prepared_then_readable_and_dependency_view_order(shape3) -> None:
    store = StepResultStore(shape3, run_id="run-1")
    state = make_state(shape3, succeeded=("task-code", "task-knowledge"))
    code_claim = claim_for(shape3, "task-code")
    knowledge_claim = claim_for(shape3, "task-knowledge")

    store.write_prepared(result("task-code", "code_expert"), expected_agent_id="code_expert")
    store.write_prepared(
        result("task-knowledge", "knowledge_expert"),
        expected_agent_id="knowledge_expert",
    )
    assert store.has_readable("task-code") is False

    store.mark_readable("task-code", state)
    store.mark_readable("task-knowledge", state)
    assert store.has_readable("task-code") is True
    assert store.has_readable("task-knowledge") is True

    synthesis_claim = claim_for(shape3, "synthesis")
    view = store.dependency_view_for(synthesis_claim, state)
    assert [entry.step_id for entry in view.entries] == [
        "task-code",
        "task-knowledge",
    ]
    assert view.entries[0].producer_agent_id == "code_expert"
    assert view.entries[1].content == "ok"
    assert view.get("task-code").content_type is ResultContentType.TEXT


def test_once_write_per_step(shape3) -> None:
    store = StepResultStore(shape3, run_id="run-1")
    store.write_prepared(result("task-code", "code_expert"), expected_agent_id="code_expert")
    with pytest.raises(StepResultStoreError) as exc_info:
        store.write_prepared(result("task-code", "code_expert"), expected_agent_id="code_expert")
    assert exc_info.value.error_code is StepResultStoreErrorCode.DUPLICATE_WRITE


def test_duplicate_callback_mark_readable_fails(shape3) -> None:
    store = StepResultStore(shape3, run_id="run-1")
    state = make_state(shape3, succeeded=("task-code",))
    store.write_prepared(result("task-code", "code_expert"), expected_agent_id="code_expert")
    store.mark_readable("task-code", state)
    with pytest.raises(StepResultStoreError) as exc_info:
        store.mark_readable("task-code", state)
    assert exc_info.value.error_code is StepResultStoreErrorCode.DUPLICATE_WRITE


def test_per_result_limit_fails_closed(shape3) -> None:
    store = StepResultStore(
        shape3, run_id="run-1", per_result_chars=5, run_total_chars=100
    )
    with pytest.raises(StepResultStoreError) as exc_info:
        store.write_prepared(
            result("task-code", "code_expert", "x" * 6),
            expected_agent_id="code_expert",
        )
    assert exc_info.value.error_code is StepResultStoreErrorCode.CAPACITY_EXCEEDED
    assert store.entry_count() == 0


def test_run_total_limit_fails_closed(shape3) -> None:
    store = StepResultStore(
        shape3, run_id="run-1", per_result_chars=6, run_total_chars=9
    )
    store.write_prepared(result("task-code", "code_expert", "x" * 5), expected_agent_id="code_expert")
    with pytest.raises(StepResultStoreError) as exc_info:
        store.write_prepared(
            result("task-knowledge", "knowledge_expert", "x" * 5),
            expected_agent_id="knowledge_expert",
        )
    assert exc_info.value.error_code is StepResultStoreErrorCode.CAPACITY_EXCEEDED
    assert store.entry_count() == 1


def test_entry_count_limit_fails_closed(shape3) -> None:
    store = StepResultStore(shape3, run_id="run-1", max_entries=2)
    store.write_prepared(result("task-code", "code_expert"), expected_agent_id="code_expert")
    store.write_prepared(result("task-knowledge", "knowledge_expert"), expected_agent_id="knowledge_expert")
    with pytest.raises(StepResultStoreError) as exc_info:
        store.write_prepared(result("synthesis", "synthesis_agent"), expected_agent_id="synthesis_agent")
    assert exc_info.value.error_code is StepResultStoreErrorCode.CAPACITY_EXCEEDED


def test_unknown_producer_rejected(shape3) -> None:
    store = StepResultStore(shape3, run_id="run-1")
    with pytest.raises(StepResultStoreError) as exc_info:
        store.write_prepared(result("ghost", "code_expert"), expected_agent_id="code_expert")
    assert exc_info.value.error_code is StepResultStoreErrorCode.UNKNOWN_PRODUCER


def test_identity_mismatch_rejected(shape3) -> None:
    store = StepResultStore(shape3, run_id="run-1")
    with pytest.raises(StepResultStoreError) as exc_info:
        store.write_prepared(result("task-code", "data_analyst"), expected_agent_id="code_expert")
    assert exc_info.value.error_code is StepResultStoreErrorCode.IDENTITY_MISMATCH


def test_consumer_not_claimed(shape3) -> None:
    store = StepResultStore(shape3, run_id="run-1")
    state = make_state(shape3, succeeded=("task-code",))
    store.write_prepared(result("task-code", "code_expert"), expected_agent_id="code_expert")
    store.mark_readable("task-code", state)
    bad_claim = StepClaim(
        "other-plan",
        shape3.version,
        "synthesis",
        datetime.now(UTC),
        shape3.steps[-1].capability_requirements,
        "synthesis_agent",
    )
    with pytest.raises(StepResultStoreError) as exc_info:
        store.dependency_view_for(bad_claim, state)
    assert exc_info.value.error_code is StepResultStoreErrorCode.CONSUMER_NOT_CLAIMED


def test_prepared_not_readable(shape3) -> None:
    store = StepResultStore(shape3, run_id="run-1")
    state = make_state(shape3, succeeded=())
    store.write_prepared(result("task-code", "code_expert"), expected_agent_id="code_expert")
    with pytest.raises(StepResultStoreError) as exc_info:
        store.dependency_view_for(claim_for(shape3, "synthesis"), state)
    assert exc_info.value.error_code is StepResultStoreErrorCode.ENTRY_NOT_READABLE


def test_producer_not_succeeded_blocks_readable(shape3) -> None:
    store = StepResultStore(shape3, run_id="run-1")
    state = make_state(shape3, succeeded=())
    store.write_prepared(result("task-code", "code_expert"), expected_agent_id="code_expert")
    with pytest.raises(StepResultStoreError) as exc_info:
        store.mark_readable("task-code", state)
    assert (
        exc_info.value.error_code
        is StepResultStoreErrorCode.PRODUCER_NOT_SUCCEEDED
    )


def test_non_dependency_entries_are_not_exposed() -> None:
    capabilities = TaskCapabilityRequirements()
    plan = Plan(
        "custom-plan",
        1,
        "custom",
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
                "task-knowledge",
                "knowledge",
                "k",
                (),
                "done",
                "knowledge_expert",
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
    store = StepResultStore(plan, run_id="run-1")
    state = make_state(plan, succeeded=("task-code", "task-knowledge"))
    store.write_prepared(result("task-code", "code_expert"), expected_agent_id="code_expert")
    store.write_prepared(
        result("task-knowledge", "knowledge_expert"),
        expected_agent_id="knowledge_expert",
    )
    store.mark_readable("task-code", state)
    store.mark_readable("task-knowledge", state)
    view = store.dependency_view_for(claim_for(plan, "synthesis"), state)
    # Only task-code is an explicit dependency; task-knowledge must not leak.
    assert [entry.step_id for entry in view.entries] == ["task-code"]
    assert view.get("task-knowledge") is None


def test_dependency_view_fails_when_one_required_missing(shape3) -> None:
    store = StepResultStore(shape3, run_id="run-1")
    state = make_state(shape3, succeeded=("task-code",))
    store.write_prepared(result("task-code", "code_expert"), expected_agent_id="code_expert")
    store.mark_readable("task-code", state)
    with pytest.raises(StepResultStoreError) as exc_info:
        store.dependency_view_for(claim_for(shape3, "synthesis"), state)
    assert exc_info.value.error_code is StepResultStoreErrorCode.ENTRY_NOT_READABLE


def test_sealed_rejects_write_and_read(shape3) -> None:
    store = StepResultStore(shape3, run_id="run-1")
    state = make_state(shape3, succeeded=("task-code",))
    store.seal()
    with pytest.raises(StepResultStoreError) as exc_info:
        store.write_prepared(result("task-code", "code_expert"), expected_agent_id="code_expert")
    assert exc_info.value.error_code is StepResultStoreErrorCode.STORE_SEALED
    with pytest.raises(StepResultStoreError) as exc_info:
        store.dependency_view_for(claim_for(shape3, "synthesis"), state)
    assert exc_info.value.error_code is StepResultStoreErrorCode.STORE_SEALED


def test_cleared_rejects_write_and_read(shape3) -> None:
    store = StepResultStore(shape3, run_id="run-1")
    state = make_state(shape3, succeeded=("task-code",))
    store.write_prepared(result("task-code", "code_expert"), expected_agent_id="code_expert")
    store.clear()
    with pytest.raises(StepResultStoreError) as exc_info:
        store.write_prepared(result("task-knowledge", "knowledge_expert"), expected_agent_id="knowledge_expert")
    assert exc_info.value.error_code is StepResultStoreErrorCode.STORE_CLEARED
    with pytest.raises(StepResultStoreError) as exc_info:
        store.dependency_view_for(claim_for(shape3, "synthesis"), state)
    assert exc_info.value.error_code is StepResultStoreErrorCode.STORE_CLEARED


def test_no_get_all(shape3) -> None:
    store = StepResultStore(shape3, run_id="run-1")
    assert not hasattr(store, "get_all")


def test_concurrent_writes_are_thread_safe(shape3) -> None:
    store = StepResultStore(shape3, run_id="run-1")

    def write(step_id: str, agent_id: str) -> None:
        store.write_prepared(result(step_id, agent_id), expected_agent_id=agent_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(write, "task-code", "code_expert"),
            pool.submit(write, "task-knowledge", "knowledge_expert"),
        ]
        for future in futures:
            future.result()
    assert store.entry_count() == 2


def test_cleanup_is_idempotent(shape3) -> None:
    store = StepResultStore(shape3, run_id="run-1")
    store.write_prepared(result("task-code", "code_expert"), expected_agent_id="code_expert")
    store.seal()
    store.clear()
    store.clear()
    assert store.is_cleared is True
    assert store.entry_count() == 0


def test_store_safe_repr_redacts_content(shape3) -> None:
    secret = "SECRET_SPECIALIST_RESULT_DO_NOT_PERSIST"
    store = StepResultStore(shape3, run_id="run-1")
    store.write_prepared(
        result("task-code", "code_expert", secret),
        expected_agent_id="code_expert",
    )
    rendered = repr(store)
    assert secret not in rendered
    assert "entries=1" in rendered
