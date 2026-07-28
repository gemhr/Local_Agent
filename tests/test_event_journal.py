from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from core.runtime import (
    CancellationPayload,
    InMemoryRunEventJournal,
    JournalAppendStatus,
    JournalError,
    JournalErrorCode,
    JournalRecord,
    ModelStartedPayload,
    OutputDeltaPayload,
    RunCompletedPayload,
    RunStartedPayload,
    RuntimeEvent,
    RuntimeEventType,
    SQLiteRunEventJournal,
    ToolCompletedPayload,
)


def event(
    sequence: int,
    *,
    run_id: str = "run-a",
    event_id: str | None = None,
    event_type: RuntimeEventType = RuntimeEventType.RUN_STARTED,
    payload=None,
) -> RuntimeEvent:
    if payload is None:
        payload = RunStartedPayload("RUNNING")
    return RuntimeEvent(
        schema_version=1,
        event_id=event_id or uuid4().hex,
        run_id=run_id,
        trace_id=f"trace-{run_id}",
        sequence=sequence,
        event_type=event_type,
        emitted_at=datetime.now(UTC),
        component="test",
        payload=payload,
    )


@pytest.fixture(params=["memory", "sqlite"])
def journal(request, tmp_path: Path):
    value = (
        InMemoryRunEventJournal()
        if request.param == "memory"
        else SQLiteRunEventJournal(str(tmp_path / "journal.db"))
    )
    yield value
    value.close()
    value.close()


def test_append_duplicate_conflicts_out_of_order_gap_and_runs(journal):
    first = event(1)
    assert journal.append(first) is JournalAppendStatus.APPENDED
    assert journal.append(first) is JournalAppendStatus.DUPLICATE

    with pytest.raises(JournalError) as exc:
        journal.append(
            replace(first, payload=RunStartedPayload("DIFFERENT"))
        )
    assert exc.value.error_code is JournalErrorCode.EVENT_ID_CONFLICT

    with pytest.raises(JournalError) as exc:
        journal.append(event(1))
    assert exc.value.error_code is JournalErrorCode.SEQUENCE_CONFLICT

    assert journal.append(event(3)) is JournalAppendStatus.APPENDED
    with pytest.raises(JournalError) as exc:
        journal.append(event(2))
    assert exc.value.error_code is JournalErrorCode.OUT_OF_ORDER

    other = event(1, run_id="run-b")
    assert journal.append(other) is JournalAppendStatus.APPENDED
    assert journal.last_sequence("run-a") == 3
    assert journal.last_sequence("run-b") == 1
    assert [item.sequence for item in journal.read_after("run-a", 0, 10)] == [
        1,
        3,
    ]
    assert journal.get_by_event_id(first.event_id) is not None


def test_terminal_is_unique_duplicate_and_last(journal):
    terminal = event(
        2,
        event_type=RuntimeEventType.RUN_COMPLETED,
        payload=RunCompletedPayload("SUCCEEDED", "COMPLETED"),
    )
    journal.append(event(1))
    assert journal.append(terminal) is JournalAppendStatus.APPENDED
    assert journal.append(terminal) is JournalAppendStatus.DUPLICATE
    with pytest.raises(JournalError) as exc:
        journal.append(
            event(
                3,
                event_type=RuntimeEventType.RUN_COMPLETED,
                payload=RunCompletedPayload("SUCCEEDED", "COMPLETED"),
            )
        )
    assert exc.value.error_code is JournalErrorCode.RUN_ALREADY_TERMINAL
    with pytest.raises(JournalError) as exc:
        journal.append(
            event(
                3,
                event_type=RuntimeEventType.CANCELLATION,
                payload=CancellationPayload("USER_CANCELLED", "test"),
            )
        )
    assert exc.value.error_code is JournalErrorCode.RUN_ALREADY_TERMINAL


def test_safe_payload_allowlist_and_output_digest(journal):
    secret = "prompt tool-output raw-query rag-chunk memory secret"
    output = event(
        1,
        event_type=RuntimeEventType.OUTPUT_DELTA,
        payload=OutputDeltaPayload(secret),
    )
    journal.append(output)
    record = journal.get_by_event_id(output.event_id)
    assert record is not None
    assert record.safe_payload["text_length"] == len(secret)
    assert len(str(record.safe_payload["text_digest"])) == 64
    assert secret not in repr(record)
    assert secret not in str(record.safe_payload)

    model = event(
        1,
        run_id="run-model",
        event_type=RuntimeEventType.MODEL_STARTED,
        payload=ModelStartedPayload("profile", 0, 0, "NONE", "digest"),
    )
    tool = event(
        1,
        run_id="run-tool",
        event_type=RuntimeEventType.TOOL_COMPLETED,
        payload=ToolCompletedPayload("safe-tool", True),
    )
    for value in (model, tool):
        journal.append(value)
        stored = journal.get_by_event_id(value.event_id)
        assert stored is not None
        serialized = str(stored.safe_payload).lower()
        for forbidden in ("prompt", "messages", "arguments", "output content"):
            assert forbidden not in serialized


def test_read_arguments_and_record_strict_validation(journal):
    for sequence, limit in ((True, 1), (0, True), (-1, 1), (0, 0), (0, 1001)):
        with pytest.raises(ValueError):
            journal.read_after("run-a", sequence, limit)
    good = JournalRecord.from_event(event(1))
    with pytest.raises(ValueError):
        replace(good, journal_schema_version=True)
    with pytest.raises(ValueError):
        replace(good, sequence=True)
    with pytest.raises(ValueError):
        replace(good, safe_payload={"bad": float("nan")})


def test_concurrent_duplicate_append_is_atomic(journal):
    value = event(1)
    with ThreadPoolExecutor(max_workers=12) as pool:
        statuses = list(pool.map(lambda _: journal.append(value), range(24)))
    assert statuses.count(JournalAppendStatus.APPENDED) == 1
    assert statuses.count(JournalAppendStatus.DUPLICATE) == 23
    assert len(journal.read_after("run-a", 0, 10)) == 1


def test_sqlite_restart_recognizes_duplicate(tmp_path: Path):
    path = tmp_path / "restart.db"
    value = event(1)
    first = SQLiteRunEventJournal(str(path))
    assert first.append(value) is JournalAppendStatus.APPENDED
    first.close()
    second = SQLiteRunEventJournal(str(path))
    try:
        assert second.append(value) is JournalAppendStatus.DUPLICATE
    finally:
        second.close()


def test_sqlite_corruption_fails_closed(tmp_path: Path):
    path = tmp_path / "corrupt.db"
    value = event(1)
    journal = SQLiteRunEventJournal(str(path))
    journal.append(value)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE runtime_event_journal
            SET safe_payload = ?
            WHERE event_id = ?
            """,
            ('{"status":"TAMPERED"}', value.event_id),
        )
    try:
        with pytest.raises(JournalError) as exc:
            journal.read_after("run-a", 0, 10)
        assert exc.value.error_code is JournalErrorCode.JOURNAL_CORRUPTED
    finally:
        journal.close()
