import asyncio
from datetime import UTC, datetime

import pytest

from core.runtime.budget import BudgetLedger, RunBudget
from core.runtime.cancellation import CancellationSource
from core.runtime.checkpoint_contract import (
    CheckpointKind,
    CheckpointMode,
    CheckpointStatus,
    SchedulerClaimGateState,
)
from core.runtime.context import create_run_context
from core.runtime.event_channel import RuntimeEventChannel
from core.runtime.event_emitter import RunEventEmitter
from core.runtime.event_journal_store import InMemoryRunEventJournal
from core.runtime.parallel_execution import ParallelExecutionPolicy, ParallelExecutor
from core.runtime.planning import (
    Plan,
    PlanSource,
    PlanStep,
    TaskCapabilityRequirements,
)
from core.runtime.run_coordinator import RunCoordinator
from core.runtime.run_registry import RunHandle, RunRegistry
from core.runtime.scheduler import SerialScheduler
from core.runtime.snapshot_store import (
    InMemorySnapshotStore,
    SnapshotErrorCode,
    SnapshotStoreError,
)
from core.runtime.snapshot_serialization import snapshot_from_json, snapshot_to_json
from core.runtime.state import AgentState
from core.runtime.state_machine import AgentStateMachine


def _plan() -> Plan:
    return Plan(
        "plan",
        1,
        "summary",
        (
            PlanStep(
                "step",
                "step",
                "description",
                (),
                "done",
                "router",
                TaskCapabilityRequirements(),
            ),
        ),
        datetime(2026, 1, 1, tzinfo=UTC),
        PlanSource.DETERMINISTIC,
    )


def _coordinator(store=None, *, run_id="run", started=True):
    context, source = create_run_context(entry_agent_id="router", run_id=run_id)
    ledger = BudgetLedger(RunBudget(max_step_starts=2))
    context.attach_budget_ledger(ledger)
    state = AgentState(context.run_id)
    machine = AgentStateMachine()
    scheduler = SerialScheduler(machine)
    if started:
        state.mark_running()
        scheduler.prepare(_plan(), state, datetime.now(UTC))
    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(
        8,
        run_id=context.run_id,
        cancellation_token=context.cancellation_token,
        journal=journal,
    )
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    )
    coordinator = RunCoordinator(
        run_context=context,
        plan=_plan(),
        agent_state=state,
        budget_ledger=ledger,
        run_handle=RunHandle(context.run_id, source, state, "checkpoint-test"),
        scheduler=scheduler,
        executor=ParallelExecutor(machine, event_emitter=emitter),
        run_registry=RunRegistry(),
        policy=ParallelExecutionPolicy(max_concurrency=1),
        state_machine=machine,
        event_emitter=emitter,
        snapshot_store=store or InMemorySnapshotStore(),
    )
    return coordinator, source, state


@pytest.mark.asyncio
async def test_pre_run_checkpoint_registers_plan_projection_without_starting_run():
    store = InMemorySnapshotStore()
    coordinator, _, state = _coordinator(store, started=False)
    assert state.status.value == "CREATED"
    assert tuple(state.steps) == ("step",)
    result = await coordinator.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.PRE_RUN,
        timeout=1,
    )
    assert result.status is CheckpointStatus.SAVED
    snapshot = store.get(result.snapshot_id)
    assert snapshot is not None
    assert snapshot.run_status == "CREATED"
    assert snapshot.checkpoint_kind == "PRE_RUN"


@pytest.mark.asyncio
async def test_step_boundary_checkpoint_is_saved_and_scheduler_resumes():
    store = InMemorySnapshotStore()
    coordinator, _, _ = _coordinator(store)
    result = await coordinator.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=1,
    )
    assert result.status is CheckpointStatus.SAVED
    assert result.quiescent
    snapshot = store.get(result.snapshot_id)
    assert snapshot is not None
    assert snapshot.activity_snapshot == result.activity_summary
    assert snapshot_from_json(snapshot_to_json(snapshot)) == snapshot
    assert snapshot.checkpoint_kind == CheckpointKind.STEP_BOUNDARY.value
    assert coordinator.scheduler.claim_gate.snapshot().state is (
        SchedulerClaimGateState.OPEN
    )


@pytest.mark.asyncio
async def test_inconsistent_checkpoint_kind_is_rejected_not_rewritten():
    store = InMemorySnapshotStore()
    coordinator, _, _ = _coordinator(store)
    result = await coordinator.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.PRE_RUN,
        timeout=1,
    )
    assert result.status is CheckpointStatus.CORRUPTED
    assert result.safe_error_code == "SNAPSHOT_CORRUPTED"
    assert store.latest("run") is None


@pytest.mark.asyncio
async def test_terminal_checkpoint_remains_subject_to_detached_activity():
    store = InMemorySnapshotStore()
    coordinator, _, state = _coordinator(store)
    state.start_step("step")
    state.succeed_step("step")
    state.mark_succeeded()
    saved = await coordinator.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.TERMINAL,
        timeout=1,
    )
    assert saved.status is CheckpointStatus.SAVED
    assert store.get(saved.snapshot_id).checkpoint_kind == "TERMINAL"

    coordinator.activity_tracker.increment("detached_tool_workers")
    try:
        audit = await coordinator.create_checkpoint(
            mode=CheckpointMode.ALLOW_NON_QUIESCENT_AUDIT,
            checkpoint_kind=CheckpointKind.TERMINAL,
            timeout=1,
        )
    finally:
        coordinator.activity_tracker.decrement("detached_tool_workers")
    assert audit.status is CheckpointStatus.SAVED_NON_QUIESCENT_AUDIT
    assert not audit.quiescent


@pytest.mark.asyncio
async def test_non_quiescent_audit_is_saved_but_not_marked_recoverable():
    store = InMemorySnapshotStore()
    coordinator, _, _ = _coordinator(store)
    coordinator.activity_tracker.increment("tool_attempts_active")
    try:
        result = await coordinator.create_checkpoint(
            mode=CheckpointMode.ALLOW_NON_QUIESCENT_AUDIT,
            checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
            timeout=1,
        )
    finally:
        coordinator.activity_tracker.decrement("tool_attempts_active")
    assert result.status is CheckpointStatus.SAVED_NON_QUIESCENT_AUDIT
    assert not result.quiescent
    assert result.checkpoint_kind is CheckpointKind.NON_QUIESCENT_AUDIT
    snapshot = store.get(result.snapshot_id)
    assert snapshot is not None and not snapshot.quiescent


@pytest.mark.asyncio
async def test_require_quiescent_timeout_does_not_save_or_mutate_running_step():
    store = InMemorySnapshotStore()
    coordinator, _, state = _coordinator(store)
    state.start_step("step")
    result = await coordinator.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=0.01,
    )
    assert result.status is CheckpointStatus.NOT_QUIESCENT
    assert store.latest("run") is None
    assert state.steps["step"].status.value == "RUNNING"
    assert coordinator.scheduler.claim_gate.snapshot().state is (
        SchedulerClaimGateState.OPEN
    )


class _FailingStore(InMemorySnapshotStore):
    def save(self, snapshot):
        raise SnapshotStoreError(SnapshotErrorCode.SNAPSHOT_STORE_FAILED)


@pytest.mark.asyncio
async def test_store_failure_and_cancellation_always_restore_claim_gate():
    coordinator, _, _ = _coordinator(_FailingStore())
    failed = await coordinator.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=1,
    )
    assert failed.status is CheckpointStatus.STORE_FAILED
    assert failed.snapshot_id is None
    assert coordinator.scheduler.claim_gate.snapshot().state is (
        SchedulerClaimGateState.OPEN
    )

    coordinator, _, _ = _coordinator()
    shutdown = CancellationSource()
    shutdown.cancel()
    stopped = await coordinator.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.TERMINAL,
        timeout=1,
        shutdown_token=shutdown.token,
    )
    assert stopped.status is CheckpointStatus.CANCELLED
    assert coordinator.scheduler.claim_gate.snapshot().state is (
        SchedulerClaimGateState.OPEN
    )

    coordinator, source, _ = _coordinator()
    source.cancel()
    cancelled = await coordinator.create_checkpoint(
        mode=CheckpointMode.ALLOW_NON_QUIESCENT_AUDIT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=1,
    )
    assert cancelled.status is CheckpointStatus.CANCELLED
    assert cancelled.snapshot_id is None
    assert coordinator.scheduler.claim_gate.snapshot().state is (
        SchedulerClaimGateState.OPEN
    )


@pytest.mark.asyncio
async def test_same_run_concurrent_checkpoint_is_rejected():
    coordinator, source, state = _coordinator()
    state.start_step("step")
    first = asyncio.create_task(
        coordinator.create_checkpoint(
            mode=CheckpointMode.REQUIRE_QUIESCENT,
            checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
            timeout=1,
        )
    )
    while coordinator.scheduler.claim_gate.snapshot().state is not (
        SchedulerClaimGateState.PAUSED
    ):
        await asyncio.sleep(0)
    second = await coordinator.create_checkpoint(
        mode=CheckpointMode.ALLOW_NON_QUIESCENT_AUDIT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=1,
    )
    assert second.status is CheckpointStatus.ALREADY_IN_PROGRESS
    source.cancel()
    assert (await first).status is CheckpointStatus.CANCELLED
    assert coordinator.scheduler.claim_gate.snapshot().state is (
        SchedulerClaimGateState.OPEN
    )


@pytest.mark.asyncio
async def test_different_run_checkpoints_do_not_share_a_global_lock():
    waiting, waiting_source, waiting_state = _coordinator(run_id="run-a")
    independent_store = InMemorySnapshotStore()
    independent, _, _ = _coordinator(
        independent_store, run_id="run-b"
    )
    waiting_state.start_step("step")
    first = asyncio.create_task(
        waiting.create_checkpoint(
            mode=CheckpointMode.REQUIRE_QUIESCENT,
            checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
            timeout=1,
        )
    )
    while waiting.scheduler.claim_gate.snapshot().state is not (
        SchedulerClaimGateState.PAUSED
    ):
        await asyncio.sleep(0)
    second = await independent.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=1,
    )
    assert second.status is CheckpointStatus.SAVED
    assert independent_store.latest("run-b") is not None
    waiting_source.cancel()
    assert (await first).status is CheckpointStatus.CANCELLED


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ("state", "budget", "journal"))
async def test_capture_failures_release_gate_and_checkpoint_lock(
    monkeypatch, failure_stage
):
    coordinator, _, state = _coordinator()

    def fail():
        raise RuntimeError("sensitive internal failure")

    if failure_stage == "state":
        monkeypatch.setattr(state, "snapshot_copy", fail)
    elif failure_stage == "budget":
        monkeypatch.setattr(coordinator.budget_ledger, "snapshot", fail)
    else:
        async def fail_watermark():
            raise RuntimeError("sensitive journal failure")

        monkeypatch.setattr(
            coordinator.event_emitter.channel,
            "capture_journal_watermark",
            fail_watermark,
        )

    result = await coordinator.create_checkpoint(
        mode=CheckpointMode.ALLOW_NON_QUIESCENT_AUDIT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=1,
    )
    assert result.status is CheckpointStatus.CORRUPTED
    assert result.safe_error_code == "CHECKPOINT_CAPTURE_FAILED"
    assert coordinator.scheduler.claim_gate.snapshot().state is (
        SchedulerClaimGateState.OPEN
    )
    again = await coordinator.create_checkpoint(
        mode=CheckpointMode.ALLOW_NON_QUIESCENT_AUDIT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=1,
    )
    assert again.status is not CheckpointStatus.ALREADY_IN_PROGRESS
