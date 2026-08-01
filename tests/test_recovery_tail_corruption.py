from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from core.runtime import (
    InMemoryRunEventJournal,
    JournalError,
    JournalRecord,
    RecoveryReason,
    RecoveryStatus,
    RecoveryValidator,
    RunCompletedPayload,
    RunStartedPayload,
    RuntimeEventType,
    SQLiteRunEventJournal,
    ToolCompletedPayload,
    ToolStartedPayload,
)
from core.runtime.journal_tail_reducer import (
    JournalTailValidationError,
    JournalTailValidator,
    LimitedJournalTailReducer,
)
from tests._recovery_fixtures import (
    recovery_plan,
    recovery_snapshot,
    runtime_event,
)


def record(sequence, event_type, payload, *, run_id="run") -> JournalRecord:
    return JournalRecord.from_event(
        runtime_event(sequence, event_type, payload, run_id=run_id)
    )


def started(sequence=1) -> JournalRecord:
    return record(sequence, RuntimeEventType.TOOL_STARTED, ToolStartedPayload("writer"))


def completed(sequence=2) -> JournalRecord:
    return record(
        sequence,
        RuntimeEventType.TOOL_COMPLETED,
        ToolCompletedPayload("writer", True),
    )


def validate(records, *, watermark=0):
    return JournalTailValidator.validate(
        run_id="run", snapshot_sequence=watermark, records=tuple(records)
    )


def test_real_records_accept_continuous_tail_legal_gap_and_missing_terminal():
    continuous = validate(
        (
            record(1, RuntimeEventType.RUN_STARTED, RunStartedPayload("RUNNING")),
            started(2),
        )
    )
    gap = validate((started(10), completed(20)))
    missing_terminal = validate((started(30),))

    assert continuous.last_sequence == 2
    assert gap.last_sequence == 20
    assert missing_terminal.terminal_event_seen is False


@pytest.mark.parametrize(
    ("records", "reason"),
    [
        ((started(1), started(1)), RecoveryReason.JOURNAL_SEQUENCE_CONFLICT),
        ((started(2), started(1)), RecoveryReason.JOURNAL_SEQUENCE_CONFLICT),
        (
            (started(1), replace(started(2), run_id="other-run")),
            RecoveryReason.JOURNAL_SEQUENCE_CONFLICT,
        ),
    ],
)
def test_duplicate_out_of_order_and_cross_run_tail_fail_closed(records, reason):
    with pytest.raises(JournalTailValidationError) as exc:
        validate(records)
    assert exc.value.status is RecoveryStatus.JOURNAL_GAP_OR_CONFLICT
    assert exc.value.reason is reason


def test_record_at_or_before_snapshot_watermark_is_rejected():
    with pytest.raises(JournalTailValidationError) as exc:
        validate((started(5),), watermark=5)
    assert exc.value.reason is RecoveryReason.JOURNAL_SEQUENCE_CONFLICT


def test_event_digest_damage_and_unknown_event_schema_are_distinguished():
    damaged = replace(started(), event_digest="0" * 64)
    with pytest.raises(JournalTailValidationError) as exc:
        validate((damaged,))
    assert exc.value.status is RecoveryStatus.CORRUPTED
    assert exc.value.reason is RecoveryReason.JOURNAL_RECORD_CORRUPTED

    unknown = started()
    object.__setattr__(unknown, "event_schema_version", 999)
    with pytest.raises(JournalTailValidationError) as exc:
        validate((unknown,))
    assert exc.value.status is RecoveryStatus.UNSUPPORTED
    assert exc.value.reason is RecoveryReason.EVENT_SCHEMA_UNSUPPORTED


def test_sqlite_event_decoder_rejects_payload_allowlist_failure(tmp_path):
    path = tmp_path / "tail-corrupt.db"
    journal = SQLiteRunEventJournal(str(path))
    event = runtime_event(1, RuntimeEventType.TOOL_STARTED, ToolStartedPayload("writer"))
    journal.append(event)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE runtime_event_journal SET safe_payload = ? WHERE event_id = ?",
            (json.dumps({"not_allowlisted": "raw-snapshot-payload"}), event.event_id),
        )
    try:
        with pytest.raises(JournalError):
            journal.read_after("run", 0, 10)
    finally:
        journal.close()


def test_duplicate_terminal_and_business_event_after_terminal_fail_closed():
    terminal = record(
        1,
        RuntimeEventType.RUN_COMPLETED,
        RunCompletedPayload("SUCCEEDED", "COMPLETED"),
    )
    for after in (
        record(
            2,
            RuntimeEventType.RUN_COMPLETED,
            RunCompletedPayload("SUCCEEDED", "COMPLETED"),
        ),
        started(2),
    ):
        with pytest.raises(JournalTailValidationError) as exc:
            validate((terminal, after))
        assert exc.value.status is RecoveryStatus.JOURNAL_GAP_OR_CONFLICT
        assert exc.value.reason is RecoveryReason.JOURNAL_TERMINAL_CONFLICT


def test_unpaired_started_and_completed_are_durable_reconciliation_facts():
    snapshot = recovery_snapshot()
    started_only = LimitedJournalTailReducer.reduce(snapshot, (started(),))
    completed_only = LimitedJournalTailReducer.reduce(snapshot, (completed(1),))

    assert RecoveryReason.TOOL_OUTCOME_UNKNOWN in started_only.reconciliation_reasons
    assert RecoveryReason.TOOL_OUTCOME_UNKNOWN in completed_only.reconciliation_reasons
    assert started_only.tool_evidence[0].event_kind == "STARTED"
    assert completed_only.tool_evidence[0].event_kind == "COMPLETED"


class TruncatedTailJournal:
    def last_sequence(self, run_id):
        return 2

    def read_after(self, run_id, sequence, limit):
        return (started(1),) if sequence == 0 else ()


def test_truncated_tail_is_not_treated_as_empty_tail():
    result = RecoveryValidator(journal=TruncatedTailJournal()).assess_snapshot(
        snapshot=recovery_snapshot(), current_plan=recovery_plan()
    )
    assert result.status is RecoveryStatus.JOURNAL_GAP_OR_CONFLICT
    assert result.reasons == (RecoveryReason.JOURNAL_SEQUENCE_CONFLICT,)
    assert result.reduced_projection is None
    assert not result.automatic_resume_supported


def test_cross_run_journal_never_supplies_another_runs_tail():
    journal = InMemoryRunEventJournal()
    journal.append(
        runtime_event(
            1,
            RuntimeEventType.RUN_STARTED,
            RunStartedPayload("RUNNING"),
            run_id="other-run",
        )
    )
    result = RecoveryValidator(journal=journal).assess_snapshot(
        snapshot=recovery_snapshot(), current_plan=recovery_plan()
    )
    assert result.journal_last_sequence == 0
    assert result.last_applied_sequence == 0
