from dataclasses import FrozenInstanceError, replace
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
    StepStateSnapshot,
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
    assert snapshot.state_snapshot.step_states[0].attempt_count is None
    assert snapshot.state_snapshot.step_states[0].execution_started is False
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


def test_cross_field_status_stop_reason_and_steps_fail_closed():
    snapshot = make_snapshot()
    with pytest.raises(ValueError, match="run status"):
        replace(snapshot, run_status="RUNNING")
    with pytest.raises(ValueError, match="stop reason"):
        replace(snapshot, stop_reason="COMPLETED")
    with pytest.raises(ValueError, match="step states"):
        replace(snapshot, step_states=())


def test_plan_step_ids_and_fingerprint_must_match_state_projection():
    snapshot = make_snapshot()
    extra_plan = replace(
        _plan(),
        steps=(
            *_plan().steps,
            replace(_plan().steps[0], step_id="step-2"),
        ),
    )
    with pytest.raises(ValueError, match="step ID"):
        RunSnapshot.create(
            snapshot_id="bad-plan",
            run_id=snapshot.run_id,
            trace_id=snapshot.trace_id,
            plan_snapshot=PlanSnapshot.from_plan(extra_plan),
            plan_fingerprint=PlanFingerprinter.fingerprint(extra_plan),
            state_snapshot=snapshot.state_snapshot,
            budget_snapshot=snapshot.budget_snapshot,
            last_journal_sequence=0,
            runtime_metadata=snapshot.runtime_metadata,
            checkpoint_kind="OBSERVATION",
            quiescent=False,
        )
    with pytest.raises(ValueError, match="fingerprint"):
        replace(snapshot, plan_fingerprint="0" * 64)

    extra_state = AgentState(snapshot.run_id)
    extra_state.add_step("step-1", "one")
    extra_state.add_step("step-2", "two")
    with pytest.raises(ValueError, match="step ID"):
        RunSnapshot.create(
            snapshot_id="bad-state",
            run_id=snapshot.run_id,
            trace_id=snapshot.trace_id,
            plan_snapshot=snapshot.plan_snapshot,
            plan_fingerprint=snapshot.plan_fingerprint,
            state_snapshot=AgentStateSnapshot.from_agent_state(extra_state),
            budget_snapshot=snapshot.budget_snapshot,
            last_journal_sequence=0,
            runtime_metadata=snapshot.runtime_metadata,
            checkpoint_kind="OBSERVATION",
            quiescent=False,
        )


def test_attempt_unknown_round_trips_as_null_and_started_contradiction_fails():
    snapshot = make_snapshot()
    payload = snapshot_to_json(snapshot)
    assert '"attempt_count":null' in payload
    assert snapshot_from_json(payload).step_states[0].attempt_count is None
    step = snapshot.step_states[0]
    with pytest.raises(ValueError, match="execution_started"):
        replace(step, execution_started=True)
    with pytest.raises(ValueError, match="attempt_count"):
        replace(step, attempt_count=True)


def test_v1_plan_snapshot_payload_remains_readable_without_v2_output_policy():
    from core.runtime.snapshot_serialization import sha256_digest

    snapshot = make_snapshot()
    payload = snapshot.to_payload()
    plan_payload = payload["plan_snapshot"]
    plan_payload["plan_schema_version"] = 1
    for step in plan_payload["steps"]:
        step.pop("output_policy")
    legacy_plan = PlanSnapshot.from_payload(plan_payload)
    payload["plan_fingerprint"] = PlanFingerprinter.fingerprint_snapshot(
        legacy_plan
    )
    payload["payload_digest"] = sha256_digest(
        {key: value for key, value in payload.items() if key != "payload_digest"}
    )

    restored = RunSnapshot.from_payload(payload)

    assert restored.plan_snapshot.plan_schema_version == 1
    assert restored.plan_snapshot.steps[0].output_policy == "FINAL_PASSTHROUGH"


def test_v2_plan_snapshot_rejects_missing_output_policy():
    payload = make_snapshot().to_payload()
    payload["plan_snapshot"]["steps"][0].pop("output_policy")
    with pytest.raises(ValueError, match="requires output_policy"):
        RunSnapshot.from_payload(payload)


def test_unknown_plan_snapshot_schema_and_execution_contract_fail_closed():
    payload = make_snapshot().to_payload()
    payload["plan_snapshot"]["plan_schema_version"] = 3
    with pytest.raises(ValueError, match="unsupported plan snapshot schema"):
        RunSnapshot.from_payload(payload)

    payload = make_snapshot().to_payload()
    payload["plan_snapshot"]["steps"][0]["static_execution_kind"] = "TOOL"
    with pytest.raises(ValueError):
        RunSnapshot.from_payload(payload)
