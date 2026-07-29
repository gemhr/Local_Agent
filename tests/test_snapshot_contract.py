from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from core.runtime.budget import BudgetLedger, BudgetUsage, RunBudget
from core.runtime.plan_fingerprint import PlanFingerprinter
from core.runtime.planning import (
    Plan,
    PlanSource,
    PlanStep,
    TaskCapabilityRequirements,
)
from core.runtime.snapshot_contract import (
    AgentStateSnapshot,
    BudgetSnapshot,
    PlanSnapshot,
    RunSnapshot,
    RuntimeMetadata,
    TextSummary,
)
from core.runtime.snapshot_serialization import snapshot_from_json, snapshot_to_json
from core.runtime.state import AgentState


def _plan(secret: str = "SECRET_PROMPT_TEXT") -> Plan:
    return Plan(
        plan_id="plan-1",
        version=1,
        task_summary=secret,
        steps=(
            PlanStep(
                "step-1",
                secret,
                secret,
                (),
                secret,
                "router",
                TaskCapabilityRequirements(requires_tools=True),
            ),
        ),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        source=PlanSource.DETERMINISTIC,
    )


def make_snapshot(
    *,
    snapshot_id: str = "snapshot-1",
    run_id: str = "run-1",
    sensitive_text: str = "SECRET_PROMPT_TEXT",
) -> RunSnapshot:
    plan = _plan(sensitive_text)
    state = AgentState(run_id)
    state.add_step("step-1", sensitive_text)
    state.final_output = sensitive_text
    state.error_message = sensitive_text
    state.steps["step-1"].error_message = sensitive_text
    return RunSnapshot.create(
        snapshot_id=snapshot_id,
        run_id=run_id,
        trace_id="trace-1",
        plan_snapshot=PlanSnapshot.from_plan(plan),
        plan_fingerprint=PlanFingerprinter.fingerprint(plan),
        state_snapshot=AgentStateSnapshot.from_agent_state(
            state, step_results={"step-1": sensitive_text}
        ),
        budget_snapshot=BudgetSnapshot.from_runtime_snapshot(
            BudgetLedger(RunBudget(max_model_calls=2)).snapshot()
        ),
        last_journal_sequence=0,
        runtime_metadata=RuntimeMetadata(
            runtime_schema_version=1,
            runtime_mode="test",
            planner_version="1",
            scheduler_version="1",
            model_routing_policy_version="1",
            tool_contract_version="1",
            retrieval_contract_version="1",
            event_schema_version="2",
            journal_schema_version="2",
        ),
        checkpoint_kind="OBSERVATION",
        quiescent=False,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


def test_run_snapshot_is_versioned_immutable_strict_and_round_trips():
    snapshot = make_snapshot()
    assert snapshot.snapshot_schema_version == 1
    assert snapshot_from_json(snapshot_to_json(snapshot)) == snapshot
    snapshot.verify_digest()
    with pytest.raises(FrozenInstanceError):
        snapshot.run_id = "other"
    with pytest.raises(TypeError):
        snapshot.plan_snapshot.steps[0].static_inputs["x"] = TextSummary.from_text("x")
    with pytest.raises(ValueError):
        RunSnapshot.create(
            snapshot_id="bad",
            run_id="run-1",
            trace_id="trace-1",
            plan_snapshot=snapshot.plan_snapshot,
            plan_fingerprint=snapshot.plan_fingerprint,
            state_snapshot=snapshot.state_snapshot,
            budget_snapshot=snapshot.budget_snapshot,
            last_journal_sequence=True,
            runtime_metadata=snapshot.runtime_metadata,
            checkpoint_kind="OBSERVATION",
            quiescent=False,
        )


def test_safe_plan_and_state_store_only_summaries():
    snapshot = make_snapshot()
    payload = snapshot_to_json(snapshot)
    assert "SECRET_PROMPT_TEXT" not in payload
    assert snapshot.plan_snapshot.task_summary.present
    assert snapshot.plan_snapshot.task_summary.length == len("SECRET_PROMPT_TEXT")
    assert snapshot.state_snapshot.final_output.present is True
    assert snapshot.state_snapshot.step_states[0].result.present is True
    assert snapshot.state_snapshot.step_states[0].attempt_count == 0
    assert snapshot.state_snapshot.step_states[0].in_flight is False


def test_budget_snapshot_preserves_reserved_and_uses_none_for_unlimited():
    ledger = BudgetLedger(RunBudget(max_model_calls=3))
    ledger.reserve(
        BudgetUsage(model_calls=1),
        reservation_type="model",
    )
    snapshot = BudgetSnapshot.from_runtime_snapshot(ledger.snapshot())
    assert snapshot.reserved["model_calls"] == 1
    assert snapshot.remaining["model_calls"] == 2
    assert snapshot.limits["tool_calls"] is None
    assert snapshot.remaining["tool_calls"] is None
    with pytest.raises(ValueError):
        BudgetSnapshot(
            budget_schema_version=1,
            limits={"x": 1},
            used={"x": float("nan")},
            reserved={"x": 0},
            remaining={"x": 1},
            ledger_version=1,
            reservation_count=0,
            generated_at=datetime.now(UTC),
        )


def test_repr_does_not_expand_snapshot_payload():
    value = repr(make_snapshot())
    assert "SECRET_PROMPT_TEXT" not in value
    assert "plan_snapshot" not in value
    assert "state_snapshot" not in value
