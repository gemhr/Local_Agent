from dataclasses import FrozenInstanceError

import pytest

from core.runtime.recovery_contract import (
    RECOVERY_REASON_TEXT,
    RECOVERY_STATUS_PRIORITY,
    RecoveryAssessment,
    RecoveryReason,
    RecoveryStatus,
    select_recovery_status,
)


def test_recovery_statuses_and_priority_are_fixed_and_explicit():
    assert {item.value for item in RecoveryStatus} == {
        "TERMINAL",
        "RESUMABLE",
        "REQUIRES_RECONCILIATION",
        "INCOMPATIBLE_SCHEMA",
        "PLAN_MISMATCH",
        "CORRUPTED",
        "JOURNAL_GAP_OR_CONFLICT",
        "UNSUPPORTED",
    }
    assert RECOVERY_STATUS_PRIORITY == (
        RecoveryStatus.CORRUPTED,
        RecoveryStatus.INCOMPATIBLE_SCHEMA,
        RecoveryStatus.PLAN_MISMATCH,
        RecoveryStatus.JOURNAL_GAP_OR_CONFLICT,
        RecoveryStatus.UNSUPPORTED,
        RecoveryStatus.REQUIRES_RECONCILIATION,
        RecoveryStatus.TERMINAL,
        RecoveryStatus.RESUMABLE,
    )
    assert (
        select_recovery_status(
            {
                RecoveryStatus.RESUMABLE,
                RecoveryStatus.REQUIRES_RECONCILIATION,
                RecoveryStatus.CORRUPTED,
            }
        )
        is RecoveryStatus.CORRUPTED
    )


def test_recovery_reasons_have_fixed_safe_text():
    required = {
        "SNAPSHOT_DIGEST_INVALID",
        "SNAPSHOT_SCHEMA_UNSUPPORTED",
        "SNAPSHOT_INTERNAL_INCONSISTENCY",
        "PLAN_FINGERPRINT_MISMATCH",
        "SNAPSHOT_SEQUENCE_AHEAD_OF_JOURNAL",
        "JOURNAL_RECORD_CORRUPTED",
        "JOURNAL_SEQUENCE_CONFLICT",
        "JOURNAL_TERMINAL_CONFLICT",
        "UNSUPPORTED_EVENT_TYPE",
        "NON_QUIESCENT_SNAPSHOT",
        "ACTIVITY_UNKNOWN",
        "RUNNING_STEP_PRESENT",
        "BUDGET_RESERVATION_PRESENT",
        "DETACHED_WORKER_PRESENT",
        "MODEL_ACTIVITY_PRESENT",
        "TOOL_ACTIVITY_PRESENT",
        "RETRIEVAL_ACTIVITY_PRESENT",
        "EVENT_PUBLICATION_PRESENT",
        "TOOL_SIDE_EFFECT_EVIDENCE",
        "TOOL_OUTCOME_UNKNOWN",
        "TOOL_COMPENSATION_FAILED",
        "RUN_TERMINAL",
        "SAFE_RESUME_PREREQUISITES_MET",
    }
    assert required <= {item.value for item in RecoveryReason}
    assert set(RECOVERY_REASON_TEXT) == set(RecoveryReason)
    forbidden = ("{", "traceback", "select ", "\\", "/")
    assert all(
        not any(item in text.lower() for item in forbidden)
        for text in RECOVERY_REASON_TEXT.values()
    )


def test_assessment_is_immutable_and_can_never_enable_replay():
    assessment = RecoveryAssessment(
        status=RecoveryStatus.RESUMABLE,
        snapshot_id="snapshot",
        run_id="run",
        snapshot_sequence=0,
        journal_last_sequence=0,
        last_applied_sequence=0,
        reasons=(RecoveryReason.SAFE_RESUME_PREREQUISITES_MET,),
        blocking_step_ids=(),
        reduced_projection=None,
        tool_evidence=(),
        resume_prerequisites_satisfied=True,
    )
    assert not assessment.automatic_resume_supported
    assert not assessment.model_replay_allowed
    assert not assessment.tool_replay_allowed
    assert not assessment.retrieval_replay_allowed
    with pytest.raises(FrozenInstanceError):
        assessment.status = RecoveryStatus.TERMINAL
    with pytest.raises(ValueError, match="never enable replay"):
        RecoveryAssessment(
            status=RecoveryStatus.RESUMABLE,
            snapshot_id="snapshot",
            run_id="run",
            snapshot_sequence=0,
            journal_last_sequence=0,
            last_applied_sequence=0,
            reasons=(RecoveryReason.SAFE_RESUME_PREREQUISITES_MET,),
            blocking_step_ids=(),
            reduced_projection=None,
            tool_evidence=(),
            resume_prerequisites_satisfied=True,
            model_replay_allowed=True,
        )
