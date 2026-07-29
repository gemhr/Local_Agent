from __future__ import annotations

from core.runtime.checkpoint_contract import CheckpointKind
from core.runtime.event_journal_store import InMemoryRunEventJournal
from core.runtime.events import (
    RunCompletedPayload,
    RuntimeEventType,
    ToolCompletedPayload,
    ToolStartedPayload,
)
from core.runtime.planning import (
    Plan,
    PlanSource,
    PlanStep,
    TaskCapabilityRequirements,
)
from core.runtime.recovery_contract import (
    RecoveryReason,
    RecoveryStatus,
    ToolRecoveryDecisionStatus,
)
from core.runtime.recovery_validation import assess_recovery
from core.runtime.state import AgentState
from core.runtime.tool_contract import safe_key_digest
from tests._recovery_fixtures import NOW, recovery_plan, recovery_snapshot, runtime_event


class AccountingFake:
    def __init__(self):
        self.model_call_count = 0
        self.tool_call_count = 0
        self.retrieval_call_count = 0
        self.compensation_call_count = 0
        self.resource_lease_call_count = 0
        self.idempotency_store_write_count = 0

    def counts(self):
        return {
            name: getattr(self, name)
            for name in (
                "model_call_count",
                "tool_call_count",
                "retrieval_call_count",
                "compensation_call_count",
                "resource_lease_call_count",
                "idempotency_store_write_count",
            )
        }


def two_step_plan() -> Plan:
    requirements = TaskCapabilityRequirements()
    return Plan(
        plan_id="two-step",
        version=1,
        task_summary="summary",
        steps=(
            PlanStep(
                "first",
                "first",
                "first",
                (),
                "done",
                "router",
                requirements,
            ),
            PlanStep(
                "second",
                "second",
                "second",
                ("first",),
                "done",
                "router",
                requirements,
            ),
        ),
        created_at=NOW,
        source=PlanSource.DETERMINISTIC,
    )


def assess(snapshot, plan, journal=None):
    return assess_recovery(
        snapshot=snapshot,
        current_plan=plan,
        journal=journal or InMemoryRunEventJournal(),
    )


def tool_started(*, idempotency_kind="NON_IDEMPOTENT"):
    return ToolStartedPayload(
        tool_name="writer",
        retry_index=0,
        tool_evidence_schema_version=1,
        invocation_identity_digest=safe_key_digest("invocation"),
        attempt_identity_digest=safe_key_digest("attempt"),
        side_effect_kind="EXTERNAL_STATE_MUTATION",
        idempotency_kind=idempotency_kind,
        replay_supported=False,
        side_effect_state="NOT_STARTED",
        compensation_state="NOT_ATTEMPTED",
        retry_disposition="PENDING",
        outcome_classification="PENDING",
        execution_detached=False,
        worker_terminated=False,
        provider_started=False,
    )


def tool_completed(*, unknown=False):
    return ToolCompletedPayload(
        tool_name="writer",
        succeeded=not unknown,
        safe_error_code=(
            "POST_COMMIT_RESPONSE_FAILURE" if unknown else None
        ),
        retry_index=0,
        side_effect_state="UNKNOWN" if unknown else "COMMITTED",
        retry_disposition="OUTCOME_UNKNOWN" if unknown else "UNSAFE",
        worker_terminated=True,
        execution_detached=False,
        duration_ms=1,
        status="FAILED" if unknown else "SUCCEEDED",
        tool_evidence_schema_version=1,
        invocation_identity_digest=safe_key_digest("invocation"),
        attempt_identity_digest=safe_key_digest("attempt"),
        side_effect_kind="EXTERNAL_STATE_MUTATION",
        idempotency_kind="NON_IDEMPOTENT",
        replay_supported=False,
        compensation_state="NOT_ATTEMPTED",
        outcome_classification=(
            "POST_COMMIT_RESPONSE_FAILURE" if unknown else "SUCCEEDED"
        ),
        provider_started=True,
    )


def test_pre_run_has_resume_prerequisites_but_no_resume_capability():
    plan = recovery_plan()
    state = AgentState("run")
    state.add_step("step", "step")
    snapshot = recovery_snapshot(
        plan=plan, state=state, kind=CheckpointKind.PRE_RUN
    )
    result = assess(snapshot, plan)
    assert result.status is RecoveryStatus.RESUMABLE
    assert result.resume_prerequisites_satisfied
    assert result.resume_data_availability.pending_steps_present
    assert not result.automatic_resume_supported
    assert not result.tool_replay_allowed


def test_step_boundary_without_completed_dependency_output_is_unsupported():
    plan = two_step_plan()
    state = AgentState("run")
    state.mark_running()
    state.add_step("first", "first")
    state.add_step("second", "second")
    state.start_step("first")
    state.succeed_step("first")
    snapshot = recovery_snapshot(plan=plan, state=state)
    result = assess(snapshot, plan)
    availability = result.resume_data_availability
    assert result.status is RecoveryStatus.UNSUPPORTED
    assert availability.completed_dependency_results_required
    assert not availability.completed_dependency_results_available
    assert not availability.result_rehydration_supported
    assert RecoveryReason.DEPENDENCY_OUTPUT_UNAVAILABLE in result.reasons
    assert (
        RecoveryReason.STEP_RESULT_REHYDRATION_UNSUPPORTED
        in result.reasons
    )


def terminal_tail(*, unknown=False):
    journal = InMemoryRunEventJournal()
    journal.append(
        runtime_event(
            1,
            RuntimeEventType.TOOL_STARTED,
            tool_started(),
            step_id="step",
            step_sequence=1,
        )
    )
    journal.append(
        runtime_event(
            2,
            RuntimeEventType.TOOL_COMPLETED,
            tool_completed(unknown=unknown),
            step_id="step",
            step_sequence=2,
        )
    )
    journal.append(
        runtime_event(
            3,
            RuntimeEventType.RUN_COMPLETED,
            RunCompletedPayload("SUCCEEDED", "COMPLETED"),
        )
    )
    return journal


def terminal_base():
    plan = recovery_plan()
    state = AgentState("run")
    state.mark_running()
    state.add_step("step", "step")
    state.start_step("step")
    state.succeed_step("step")
    snapshot = recovery_snapshot(
        plan=plan, state=state, kind=CheckpointKind.TERMINAL
    )
    return plan, snapshot


def test_terminal_completed_non_idempotent_tool_does_not_block_terminal():
    plan, snapshot = terminal_base()
    result = assess(snapshot, plan, terminal_tail())
    assert result.status is RecoveryStatus.TERMINAL
    assert (
        result.tool_decisions[0].status
        is ToolRecoveryDecisionStatus.DO_NOT_RETRY
    )
    assert not result.output_reconstruction_supported


def test_terminal_outcome_unknown_requires_reconciliation():
    plan, snapshot = terminal_base()
    result = assess(snapshot, plan, terminal_tail(unknown=True))
    assert result.status is RecoveryStatus.REQUIRES_RECONCILIATION
    assert (
        result.tool_decisions[0].status
        is ToolRecoveryDecisionStatus.MANUAL_RECONCILIATION
    )
    assert RecoveryReason.TOOL_OUTCOME_UNKNOWN in result.reasons


def test_terminal_never_claims_full_output_reconstruction():
    plan = recovery_plan()
    state = AgentState("run")
    state.mark_running()
    state.add_step("step", "step")
    state.start_step("step")
    state.succeed_step("step")
    state.mark_succeeded(final_output="answer")
    snapshot = recovery_snapshot(
        plan=plan, state=state, kind=CheckpointKind.TERMINAL
    )
    result = assess(snapshot, plan)
    assert result.status is RecoveryStatus.TERMINAL
    assert result.reduced_projection.output_available
    assert not result.output_reconstruction_supported
    assert not result.resume_data_availability.output_reconstruction_supported


def test_decision_and_assessment_have_zero_execution_side_effects():
    accounting = AccountingFake()
    plan = recovery_plan()
    snapshot = recovery_snapshot(plan=plan)
    result = assess(snapshot, plan)
    assert result.status is RecoveryStatus.RESUMABLE
    counts = accounting.counts()
    assert counts == {name: 0 for name in counts}
