from dataclasses import replace

import pytest

from core.runtime.event_journal import JournalRecord
from core.runtime.events import (
    BudgetExhaustedPayload,
    ModelStartedPayload,
    OutputDeltaPayload,
    RunCompletedPayload,
    RuntimeEventType,
    StepCompletedPayload,
    StepStartedPayload,
    ToolCompletedPayload,
    ToolStartedPayload,
)
from core.runtime.journal_tail_reducer import (
    JournalTailValidationError,
    JournalTailValidator,
    LimitedJournalTailReducer,
)
from core.runtime.recovery_contract import RecoveryReason, RecoveryStatus
from tests._recovery_fixtures import recovery_snapshot, runtime_event


def record(sequence, event_type, payload, **kwargs):
    return JournalRecord.from_event(
        runtime_event(sequence, event_type, payload, **kwargs)
    )


def test_tail_validator_accepts_numeric_gaps_and_missing_legacy_span():
    records = (
        record(
            10,
            RuntimeEventType.OUTPUT_DELTA,
            OutputDeltaPayload("secret"),
        ),
        replace(
            record(
                12,
                RuntimeEventType.MODEL_STARTED,
                ModelStartedPayload("profile", 0, 0, "NONE", "digest"),
            ),
            event_schema_version=1,
            span_id=None,
            parent_span_id=None,
        ),
    )
    # Replacing event_schema_version changes the digest, so preserve a genuine
    # legacy-compatible record for this ordering assertion.
    records = (records[0], record(
        12,
        RuntimeEventType.MODEL_STARTED,
        ModelStartedPayload("profile", 0, 0, "NONE", "digest"),
    ))
    result = JournalTailValidator.validate(
        run_id="run", snapshot_sequence=0, records=records
    )
    assert [item.sequence for item in result.records] == [10, 12]


def test_tail_validator_rejects_second_terminal_and_unknown_schema():
    terminal = record(
        10,
        RuntimeEventType.RUN_COMPLETED,
        RunCompletedPayload("SUCCEEDED", "COMPLETED"),
    )
    after_terminal = record(
        12,
        RuntimeEventType.RUN_COMPLETED,
        RunCompletedPayload("SUCCEEDED", "COMPLETED"),
    )
    with pytest.raises(JournalTailValidationError) as exc:
        JournalTailValidator.validate(
            run_id="run",
            snapshot_sequence=0,
            records=(terminal, after_terminal),
        )
    assert exc.value.status is RecoveryStatus.JOURNAL_GAP_OR_CONFLICT
    assert exc.value.reason is RecoveryReason.JOURNAL_TERMINAL_CONFLICT

    unsupported = replace(
        record(
            10,
            RuntimeEventType.OUTPUT_DELTA,
            OutputDeltaPayload("secret"),
        ),
        event_schema_version=99,
    )
    with pytest.raises(JournalTailValidationError) as exc:
        JournalTailValidator.validate(
            run_id="run", snapshot_sequence=0, records=(unsupported,)
        )
    assert exc.value.status is RecoveryStatus.UNSUPPORTED
    assert exc.value.reason is RecoveryReason.EVENT_SCHEMA_UNSUPPORTED


def test_limited_reducer_updates_safe_projection_without_rebuilding_output_or_budget():
    snapshot = recovery_snapshot()
    original_budget = snapshot.budget_snapshot
    records = (
        record(
            10,
            RuntimeEventType.STEP_STARTED,
            StepStartedPayload("RUNNING"),
            step_id="step",
            step_sequence=1,
        ),
        record(
            12,
            RuntimeEventType.OUTPUT_DELTA,
            OutputDeltaPayload("SECRET USER OUTPUT"),
            step_id="step",
            step_sequence=2,
        ),
        record(
            18,
            RuntimeEventType.BUDGET_EXHAUSTED,
            BudgetExhaustedPayload("run"),
        ),
        record(
            20,
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload("FAILED", "BUDGET_EXHAUSTED", 2),
            step_id="step",
            step_sequence=3,
        ),
        record(
            30,
            RuntimeEventType.RUN_COMPLETED,
            RunCompletedPayload("FAILED", "BUDGET_EXHAUSTED"),
        ),
    )
    result = LimitedJournalTailReducer.reduce(snapshot, records)
    projection = result.projection
    assert projection.run_status == "FAILED"
    assert projection.stop_reason == "BUDGET_EXHAUSTED"
    assert projection.step_states["step"].status == "FAILED"
    assert projection.step_states["step"].attempt_count is None
    assert projection.output_available is True
    assert not hasattr(projection, "output")
    assert projection.budget_snapshot is original_budget
    assert projection.budget_exhausted
    assert projection.last_applied_sequence == 30


def test_step_started_never_infers_or_increments_attempt_count():
    snapshot = recovery_snapshot()
    started = record(
        10,
        RuntimeEventType.STEP_STARTED,
        StepStartedPayload("RUNNING"),
        step_id="step",
        step_sequence=1,
    )
    projection = LimitedJournalTailReducer.reduce(
        snapshot, (started,)
    ).projection
    assert projection.step_states["step"].execution_started
    assert projection.step_states["step"].attempt_count is None


def test_tool_evidence_hashes_identities_and_blocks_committed_or_unknown_outcome():
    snapshot = recovery_snapshot()
    records = (
        record(
            10,
            RuntimeEventType.TOOL_STARTED,
            ToolStartedPayload(
                "writer",
                invocation_id="invocation-secret",
                attempt_id="attempt-secret",
            ),
            step_id="step",
            step_sequence=1,
        ),
        record(
            12,
            RuntimeEventType.TOOL_COMPLETED,
            ToolCompletedPayload(
                "writer",
                True,
                invocation_id="invocation-secret",
                attempt_id="attempt-secret",
                side_effect_state="COMMITTED",
                retry_disposition="UNSAFE",
            ),
            step_id="step",
            step_sequence=2,
        ),
    )
    result = LimitedJournalTailReducer.reduce(snapshot, records)
    assert RecoveryReason.TOOL_SIDE_EFFECT_EVIDENCE in (
        result.reconciliation_reasons
    )
    completed = result.tool_evidence[-1]
    assert completed.invocation_identity_digest != "invocation-secret"
    assert completed.attempt_identity_digest != "attempt-secret"
    assert len(completed.invocation_identity_digest) == 64
    assert "invocation-secret" not in repr(completed)

    unmatched = LimitedJournalTailReducer.reduce(
        snapshot,
        (
            record(
                20,
                RuntimeEventType.TOOL_STARTED,
                ToolStartedPayload("reader"),
                step_id="step",
                step_sequence=1,
            ),
        ),
    )
    assert RecoveryReason.TOOL_OUTCOME_UNKNOWN in (
        unmatched.reconciliation_reasons
    )
