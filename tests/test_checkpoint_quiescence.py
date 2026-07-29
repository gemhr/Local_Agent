from datetime import UTC, datetime

from core.runtime.activity import RuntimeActivityProvider, RuntimeActivityTracker
from core.runtime.budget import BudgetLedger, BudgetUsage, RunBudget
from core.runtime.claim_gate import SchedulerClaimGate
from core.runtime.context import create_run_context
from core.runtime.event_channel import RuntimeEventChannel
from core.runtime.state import AgentState


def _provider():
    context, _ = create_run_context(entry_agent_id="router", run_id="run")
    ledger = BudgetLedger(RunBudget(max_model_calls=5))
    context.attach_budget_ledger(ledger)
    tracker = RuntimeActivityTracker(context.run_id)
    context.attach_activity_tracker(tracker)
    state = AgentState(context.run_id)
    channel = RuntimeEventChannel(4, run_id=context.run_id)
    gate = SchedulerClaimGate()
    provider = RuntimeActivityProvider(
        run_id=context.run_id,
        tracker=tracker,
        claim_gate=gate,
        agent_state=state,
        budget_ledger=ledger,
        event_channel=channel,
    )
    return provider, tracker, state, ledger


def test_pre_run_and_terminal_without_business_activity_are_quiescent():
    provider, _, state, _ = _provider()
    assert provider.capture().quiescent
    state.mark_running()
    state.add_step("step", "step")
    state.start_step("step")
    assert not provider.capture().quiescent
    state.succeed_step("step")
    state.mark_succeeded()
    assert provider.capture().quiescent


def test_each_tracked_attempt_and_detached_worker_blocks_quiescence():
    provider, tracker, _, _ = _provider()
    for field in (
        "model_attempts_active",
        "tool_attempts_active",
        "retrievals_active",
        "detached_tool_workers",
        "detached_retrieval_workers",
        "step_workers_active",
    ):
        tracker.increment(field)
        assert not provider.capture().quiescent
        tracker.decrement(field)
        assert provider.capture().quiescent


def test_budget_reservation_and_unknown_activity_block_without_mutating_budget():
    provider, tracker, _, ledger = _provider()
    reservation = ledger.reserve(
        BudgetUsage(model_calls=1), reservation_type="model"
    )
    before = ledger.snapshot()
    assert not provider.capture().quiescent
    assert ledger.snapshot().reserved_usage == before.reserved_usage
    ledger.release(reservation)
    tracker.mark_unknown("legacy-application-tracker")
    assert provider.capture().activity_unknown
    assert not provider.capture().quiescent
