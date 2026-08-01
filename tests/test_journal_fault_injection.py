from __future__ import annotations

import pytest

from core.runtime import (
    EventPublicationError,
    FaultPoint,
    JournalError,
    RuntimeEventChannel,
    SQLiteRunEventJournal,
)
from tests._event_fault_fixtures import event_controller, run_started_draft


@pytest.mark.asyncio
async def test_sqlite_before_append_fault_leaves_transaction_without_record(tmp_path):
    path = tmp_path / "before.db"
    journal = SQLiteRunEventJournal(str(path))
    channel = RuntimeEventChannel(
        2,
        run_id="run-a",
        journal=journal,
        fault_controller=event_controller(FaultPoint.EVENT_BEFORE_JOURNAL_APPEND),
    )
    with pytest.raises(EventPublicationError):
        await channel.publish(run_started_draft())
    assert journal.read_after("run-a", 0, 10) == ()
    journal.close()

    reopened = SQLiteRunEventJournal(str(path))
    assert reopened.read_after("run-a", 0, 10) == ()
    reopened.close()


@pytest.mark.asyncio
async def test_sqlite_after_append_fault_occurs_after_commit_and_survives_reopen(tmp_path):
    path = tmp_path / "after.db"
    journal = SQLiteRunEventJournal(str(path))
    channel = RuntimeEventChannel(
        2,
        run_id="run-a",
        journal=journal,
        fault_controller=event_controller(FaultPoint.EVENT_AFTER_JOURNAL_APPEND),
    )
    with pytest.raises(EventPublicationError) as captured:
        await channel.publish(run_started_draft())
    event = captured.value.event
    assert captured.value.partially_persisted is True
    assert channel.buffered_count == 0
    journal.close()

    reopened = SQLiteRunEventJournal(str(path))
    records = reopened.read_after("run-a", 0, 10)
    assert [(item.event_id, item.sequence) for item in records] == [
        (event.event_id, 1)
    ]
    reopened.close()


@pytest.mark.asyncio
async def test_sqlite_insert_failure_rolls_back_transaction_and_next_append_can_commit(tmp_path):
    journal = SQLiteRunEventJournal(str(tmp_path / "rollback.db"))
    journal._connection.execute(
        """
        CREATE TRIGGER reject_event BEFORE INSERT ON runtime_event_journal
        BEGIN SELECT RAISE(ABORT, 'test-only'); END
        """
    )
    event = await _event_from_channel(run_started_draft())
    with pytest.raises(JournalError):
        journal.append(event)
    assert journal.read_after("run-a", 0, 10) == ()

    journal._connection.execute("DROP TRIGGER reject_event")
    journal.append(event)
    assert journal.last_sequence("run-a") == 1
    journal.close()
    with pytest.raises(JournalError):
        journal.append(event)


async def _event_from_channel(draft):
    channel = RuntimeEventChannel(1, run_id=draft.run_id)
    return await channel.publish(draft)
