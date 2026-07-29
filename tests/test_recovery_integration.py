import asyncio

import pytest

from core.runtime.budget import BudgetLedger, RunBudget
from core.runtime.checkpoint import CheckpointCoordinator, default_runtime_metadata
from core.runtime.checkpoint_contract import (
    CheckpointKind,
    CheckpointMode,
    CheckpointStatus,
)
from core.runtime.claim_gate import SchedulerClaimGate
from core.runtime.context import create_run_context
from core.runtime.event_channel import RuntimeEventChannel
from core.runtime.event_emitter import RunEventEmitter
from core.runtime.event_journal_store import InMemoryRunEventJournal
from core.runtime.events import (
    RuntimeEventType,
    RuntimeEventDraft,
    ToolCompletedPayload,
    ToolStartedPayload,
)
from core.runtime.parallel_execution import (
    ParallelExecutionPolicy,
    ParallelExecutor,
)
from core.runtime.recovery_contract import RecoveryReason, RecoveryStatus
from core.runtime.recovery_validation import RecoveryValidator
from core.runtime.scheduler import SerialScheduler
from core.runtime.snapshot_store import InMemorySnapshotStore
from core.runtime.state import AgentState
from core.runtime.state_machine import AgentStateMachine
from core.runtime.activity import RuntimeActivityProvider, RuntimeActivityTracker
from tests._recovery_fixtures import (
    recovery_plan,
    recovery_snapshot,
    runtime_event,
)


class CountingAdapter:
    def __init__(self):
        self.call_count = 0

    def call(self):
        self.call_count += 1


def test_store_to_journal_recovery_collects_tool_evidence_with_zero_replay():
    plan = recovery_plan()
    snapshot = recovery_snapshot(plan=plan)
    store = InMemorySnapshotStore()
    store.save(snapshot)
    journal = InMemoryRunEventJournal()
    journal.append(
        runtime_event(
            10,
            RuntimeEventType.TOOL_STARTED,
            ToolStartedPayload(
                "writer", invocation_id="invocation", attempt_id="attempt"
            ),
            step_id="step",
            step_sequence=1,
        )
    )
    journal.append(
        runtime_event(
            12,
            RuntimeEventType.TOOL_COMPLETED,
            ToolCompletedPayload(
                "writer",
                True,
                invocation_id="invocation",
                attempt_id="attempt",
                side_effect_state="COMMITTED",
                retry_disposition="UNSAFE",
            ),
            step_id="step",
            step_sequence=2,
        )
    )
    model = CountingAdapter()
    tool = CountingAdapter()
    retrieval = CountingAdapter()

    result = RecoveryValidator(
        snapshot_store=store, journal=journal
    ).validate(snapshot_id=snapshot.snapshot_id, current_plan=plan)

    assert result.status is RecoveryStatus.REQUIRES_RECONCILIATION
    assert RecoveryReason.TOOL_SIDE_EFFECT_EVIDENCE in result.reasons
    assert len(result.tool_evidence) == 2
    assert model.call_count == 0
    assert tool.call_count == 0
    assert retrieval.call_count == 0


class DelayedCompletionExecutor(ParallelExecutor):
    def __init__(self, *args, transition_reached, release_transition, **kwargs):
        super().__init__(*args, **kwargs)
        self.transition_reached = transition_reached
        self.release_transition = release_transition

    async def _emit_step_completed(self, *args, **kwargs):
        self.transition_reached.set()
        await self.release_transition.wait()
        await super()._emit_step_completed(*args, **kwargs)


class ImmediateDriver:
    async def execute(self, claim, run_context):
        return "done"


@pytest.mark.asyncio
async def test_state_commit_to_step_event_window_cannot_save_quiescent_snapshot():
    plan = recovery_plan()
    context, _ = create_run_context(entry_agent_id="router", run_id="run")
    ledger = BudgetLedger(RunBudget(max_step_starts=2))
    context.attach_budget_ledger(ledger)
    tracker = RuntimeActivityTracker("run")
    context.attach_activity_tracker(tracker)
    state = AgentState("run")
    state.mark_running()
    machine = AgentStateMachine()
    scheduler = SerialScheduler(machine)
    scheduler.prepare(plan, state, state.updated_at)
    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(8, run_id="run", journal=journal)
    emitter = RunEventEmitter(
        run_id="run", trace_id=context.trace_id, channel=channel
    )
    reached = asyncio.Event()
    release = asyncio.Event()
    executor = DelayedCompletionExecutor(
        machine,
        event_emitter=emitter,
        transition_reached=reached,
        release_transition=release,
    )
    store = InMemorySnapshotStore()
    provider = RuntimeActivityProvider(
        run_id="run",
        tracker=tracker,
        claim_gate=scheduler.claim_gate,
        agent_state=state,
        budget_ledger=ledger,
        event_channel=channel,
    )
    checkpoint = CheckpointCoordinator(
        run_context=context,
        plan=plan,
        agent_state=state,
        budget_ledger=ledger,
        event_channel=channel,
        snapshot_store=store,
        claim_gate=scheduler.claim_gate,
        activity_provider=provider,
        runtime_metadata=default_runtime_metadata(),
    )

    executing = asyncio.create_task(
        executor.execute_ready(
            scheduler=scheduler,
            plan=plan,
            state=state,
            occurred_at=state.updated_at,
            run_context=context,
            driver=ImmediateDriver(),
            policy=ParallelExecutionPolicy(1),
        )
    )
    await reached.wait()
    assert state.steps["step"].status.value == "SUCCEEDED"
    # STEP_COMPLETED is deliberately not published yet, but the worker
    # lifecycle epoch remains active across the whole transition.
    assert journal.last_sequence("run") == 1
    result = await checkpoint.capture(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=0.01,
    )
    assert result.status is CheckpointStatus.NOT_QUIESCENT
    assert store.latest("run") is None

    release.set()
    await executing
    assert journal.last_sequence("run") == 2


@pytest.mark.asyncio
async def test_completed_transition_between_capture_samples_is_detected(
    monkeypatch,
):
    plan = recovery_plan()
    context, _ = create_run_context(entry_agent_id="router", run_id="run")
    ledger = BudgetLedger(RunBudget())
    context.attach_budget_ledger(ledger)
    tracker = RuntimeActivityTracker("run")
    context.attach_activity_tracker(tracker)
    state = AgentState("run")
    state.mark_running()
    state.add_step("step", "step")
    gate = SchedulerClaimGate()
    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(4, run_id="run", journal=journal)
    store = InMemorySnapshotStore()
    checkpoint = CheckpointCoordinator(
        run_context=context,
        plan=plan,
        agent_state=state,
        budget_ledger=ledger,
        event_channel=channel,
        snapshot_store=store,
        claim_gate=gate,
        activity_provider=RuntimeActivityProvider(
            run_id="run",
            tracker=tracker,
            claim_gate=gate,
            agent_state=state,
            budget_ledger=ledger,
            event_channel=channel,
        ),
        runtime_metadata=default_runtime_metadata(),
    )

    async def watermark_with_completed_transition():
        tracker.increment("state_event_transitions_in_flight")
        tracker.decrement("state_event_transitions_in_flight")
        return 0

    monkeypatch.setattr(
        channel,
        "capture_journal_watermark",
        watermark_with_completed_transition,
    )
    result = await checkpoint.capture(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=1,
    )
    assert result.status is CheckpointStatus.NOT_QUIESCENT
    assert result.activity_summary.state_event_transition_observed
    assert store.latest("run") is None


@pytest.mark.asyncio
async def test_journal_append_before_channel_enqueue_consumes_watermark_safely():
    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(1, run_id="run", journal=journal)
    await channel.publish(
        RuntimeEventDraft(
            run_id="run",
            trace_id="trace",
            event_type=RuntimeEventType.TOOL_STARTED,
            component="test",
            payload=ToolStartedPayload("one"),
            step_id="step",
            step_sequence=1,
        )
    )
    # RuntimeEventChannel accepts drafts, so use the already proven journal
    # state here: the second publication consumes sequence 2 before queue put.
    blocked = asyncio.create_task(
        channel.publish(
            RuntimeEventDraft(
                run_id="run",
                trace_id="trace",
                event_type=RuntimeEventType.TOOL_STARTED,
                component="test",
                payload=ToolStartedPayload("two"),
                step_id="step",
                step_sequence=2,
            )
        )
    )
    while journal.last_sequence("run") != 2:
        await asyncio.sleep(0)
    assert channel.publications_in_flight == 1
    await channel.abort()
    with pytest.raises(Exception):
        await blocked
    assert await channel.capture_journal_watermark() == 2
