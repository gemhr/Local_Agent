from dataclasses import replace

from core.runtime.checkpoint_contract import CheckpointKind
from core.runtime.event_journal_store import InMemoryRunEventJournal
from core.runtime.events import (
    ModelCompletedPayload,
    ModelStartedPayload,
    OutputDeltaPayload,
    RuntimeEventType,
)
from core.runtime.recovery_contract import RecoveryReason, RecoveryStatus
from core.runtime.recovery_validation import RecoveryValidator
from core.runtime.state import AgentState
from tests._recovery_fixtures import (
    activity,
    recovery_plan,
    recovery_snapshot,
    runtime_event,
)


def assess(snapshot, *, plan=None, journal=None):
    return RecoveryValidator(
        journal=journal or InMemoryRunEventJournal()
    ).assess_snapshot(
        snapshot=snapshot,
        current_plan=plan or recovery_plan(),
    )


def test_safe_step_boundary_is_resumable_but_never_auto_resumes():
    result = assess(recovery_snapshot())
    assert result.status is RecoveryStatus.RESUMABLE
    assert result.resume_prerequisites_satisfied
    assert result.reasons == (
        RecoveryReason.SAFE_RESUME_PREREQUISITES_MET,
    )
    assert not result.automatic_resume_supported
    assert not result.model_replay_allowed
    assert not result.tool_replay_allowed
    assert not result.retrieval_replay_allowed


def test_current_plan_mismatch_and_invalid_snapshot_digest_fail_at_priority():
    snapshot = recovery_snapshot()
    mismatch = assess(snapshot, plan=recovery_plan(summary="changed"))
    assert mismatch.status is RecoveryStatus.PLAN_MISMATCH
    assert mismatch.reasons == (
        RecoveryReason.PLAN_FINGERPRINT_MISMATCH,
    )

    corrupted = assess(replace(snapshot, payload_digest="0" * 64))
    assert corrupted.status is RecoveryStatus.CORRUPTED
    assert corrupted.reasons == (RecoveryReason.SNAPSHOT_DIGEST_INVALID,)


def test_snapshot_sequence_ahead_of_journal_fails_closed():
    result = assess(recovery_snapshot(sequence=10))
    assert result.status is RecoveryStatus.JOURNAL_GAP_OR_CONFLICT
    assert result.reasons == (
        RecoveryReason.SNAPSHOT_SEQUENCE_AHEAD_OF_JOURNAL,
    )
    assert result.journal_last_sequence == 0


def test_numeric_journal_gaps_are_legal_and_paired_model_events_are_not_replayed():
    journal = InMemoryRunEventJournal()
    journal.append(
        runtime_event(
            10,
            RuntimeEventType.MODEL_STARTED,
            ModelStartedPayload("profile", 0, 0, "NONE", "digest"),
        )
    )
    journal.append(
        runtime_event(
            18,
            RuntimeEventType.MODEL_COMPLETED,
            ModelCompletedPayload("profile", 0, 0, True),
        )
    )
    result = assess(recovery_snapshot(), journal=journal)
    assert result.status is RecoveryStatus.RESUMABLE
    assert result.last_applied_sequence == 18


def test_running_and_non_quiescent_snapshots_require_reconciliation():
    state = AgentState("run")
    state.mark_running()
    state.add_step("step", "step")
    state.start_step("step")
    snapshot = recovery_snapshot(
        state=state,
        kind=CheckpointKind.NON_QUIESCENT_AUDIT,
        activity_snapshot=activity(
            running_step_count=1,
            step_workers_active=1,
        ),
        quiescent=False,
    )
    result = assess(snapshot)
    assert result.status is RecoveryStatus.REQUIRES_RECONCILIATION
    assert RecoveryReason.NON_QUIESCENT_SNAPSHOT in result.reasons
    assert RecoveryReason.RUNNING_STEP_PRESENT in result.reasons
    assert result.blocking_step_ids == ("step",)
    assert result.reduced_projection.step_states["step"].status == "RUNNING"
    assert result.reduced_projection.step_states["step"].attempt_count is None


def test_terminal_snapshot_is_terminal_only_after_all_safety_checks():
    state = AgentState("run")
    state.mark_running()
    state.add_step("step", "step")
    state.start_step("step")
    state.succeed_step("step")
    state.mark_succeeded()
    terminal = recovery_snapshot(
        state=state,
        kind=CheckpointKind.TERMINAL,
    )
    result = assess(terminal)
    assert result.status is RecoveryStatus.TERMINAL
    assert result.reasons == (RecoveryReason.RUN_TERMINAL,)

    audited = replace(
        terminal,
        checkpoint_kind=CheckpointKind.NON_QUIESCENT_AUDIT.value,
        quiescent=False,
        activity_snapshot=activity(detached_tool_workers=1),
    )
    # Replacing digest-relevant fields without recomputing must be rejected
    # before the lower-priority reconciliation fact.
    result = assess(audited)
    assert result.status is RecoveryStatus.CORRUPTED
    assert result.reasons == (RecoveryReason.SNAPSHOT_DIGEST_INVALID,)


def test_output_delta_only_marks_metadata_presence():
    journal = InMemoryRunEventJournal()
    journal.append(
        runtime_event(
            10,
            RuntimeEventType.OUTPUT_DELTA,
            OutputDeltaPayload("SECRET COMPLETE ANSWER"),
        )
    )
    result = assess(recovery_snapshot(), journal=journal)
    assert result.status is RecoveryStatus.RESUMABLE
    assert result.reduced_projection.output_available
    assert not hasattr(result.reduced_projection, "output")


def test_checkpoint_kind_semantics_are_not_silently_rewritten():
    invalid = recovery_snapshot(kind=CheckpointKind.PRE_RUN)
    result = assess(invalid)
    assert result.status is RecoveryStatus.CORRUPTED
    assert result.reasons == (
        RecoveryReason.SNAPSHOT_INTERNAL_INCONSISTENCY,
    )
