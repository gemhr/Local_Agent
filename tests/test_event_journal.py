from __future__ import annotations

import pytest

from core.runtime.event_journal import JournalAppendStatus, JournalError, JournalErrorCode
from core.runtime.event_journal_store import PostgresRunEventJournal
from core.runtime.events import RuntimeEvent, RuntimeEventDraft, RuntimeEventType, StepCompletedPayload

pytestmark = pytest.mark.asyncio


def event(run_id: str, sequence: int, *, component: str = "test") -> RuntimeEvent:
    return RuntimeEvent.from_draft(RuntimeEventDraft(
        run_id=run_id, trace_id="trace", event_type=RuntimeEventType.STEP_COMPLETED,
        component=component, payload=StepCompletedPayload(status="SUCCEEDED"),
    ), sequence)


async def test_postgres_journal_append_duplicate_and_sequence_contract(clean_database):
    journal = PostgresRunEventJournal(clean_database)
    first = event("journal-contract", 1)
    assert await journal.append(first) is JournalAppendStatus.APPENDED
    assert await journal.append(first) is JournalAppendStatus.DUPLICATE
    with pytest.raises(JournalError) as exc_info:
        await journal.append(event("journal-contract", 1, component="conflict"))
    assert exc_info.value.error_code is JournalErrorCode.SEQUENCE_CONFLICT


async def test_postgres_journal_rejects_out_of_order(clean_database):
    journal = PostgresRunEventJournal(clean_database)
    await journal.append(event("journal-order", 2))
    with pytest.raises(JournalError) as exc_info:
        await journal.append(event("journal-order", 1))
    assert exc_info.value.error_code is JournalErrorCode.OUT_OF_ORDER


async def test_postgres_journal_database_evidence_survives_new_store(clean_database):
    await PostgresRunEventJournal(clean_database).append(event("journal-restart", 1))
    reopened = PostgresRunEventJournal(clean_database)
    assert await reopened.last_sequence("journal-restart") == 1
    (await reopened.read_after("journal-restart", 0, 10))[0].verify()
