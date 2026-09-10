#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stage6-WP1 real PostgreSQL integration tests: Runtime evidence stores.

These tests require a real PostgreSQL server (see ``tests/_pg_fixtures.py``).
Fakes/mocks are not accepted as WP1 evidence.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from core.persistence.database import Database, DatabaseConfig
from core.persistence.errors import DatabaseErrorCode, PersistenceError
from core.runtime.event_consumer import (
    EventConsumptionCheckpoint,
    PostgresEventConsumptionCheckpointStore,
)
from core.runtime.event_journal import (
    JournalAppendStatus,
    JournalError,
    JournalErrorCode,
)
from core.runtime.event_journal_store import PostgresRunEventJournal
from core.runtime.events import (
    RunCompletedPayload,
    RuntimeEvent,
    RuntimeEventDraft,
    RuntimeEventType,
    StepCompletedPayload,
)
from core.runtime.snapshot_store import (
    PostgresSnapshotStore,
    SnapshotSaveStatus,
    SnapshotStoreError,
)

pytestmark = pytest.mark.asyncio


def _draft(
    run_id: str,
    sequence: int,
    event_type: RuntimeEventType,
    component: str = "test_component",
    payload=None,
) -> RuntimeEvent:
    if payload is None:
        if event_type is RuntimeEventType.RUN_COMPLETED:
            payload = RunCompletedPayload(status="SUCCEEDED", stop_reason="COMPLETED")
        else:
            payload = StepCompletedPayload(status="SUCCEEDED")
    draft = RuntimeEventDraft(
        run_id=run_id,
        trace_id=f"trace-{run_id}",
        event_type=event_type,
        component=component,
        payload=payload,
    )
    return RuntimeEvent.from_draft(draft, sequence)


async def _journal(database: Database) -> PostgresRunEventJournal:
    return PostgresRunEventJournal(database)


# ---------------------------------------------------------------------------
# Journal contract
# ---------------------------------------------------------------------------


async def test_journal_append_and_read_round_trip(clean_database: Database) -> None:
    journal = await _journal(clean_database)
    run_id = "run-roundtrip"
    assert (
        await journal.append(_draft(run_id, 1, RuntimeEventType.STEP_COMPLETED))
        is JournalAppendStatus.APPENDED
    )
    assert (
        await journal.append(_draft(run_id, 2, RuntimeEventType.STEP_COMPLETED))
        is JournalAppendStatus.APPENDED
    )
    page = await journal.read_after(run_id, 0, 10)
    assert [record.sequence for record in page] == [1, 2]
    assert await journal.last_sequence(run_id) == 2
    first = await journal.get_by_event_id(page[0].event_id)
    assert first is not None and first.sequence == 1


async def test_journal_duplicate_same_event_is_idempotent(
    clean_database: Database,
) -> None:
    journal = await _journal(clean_database)
    event = _draft("run-duplicate", 1, RuntimeEventType.STEP_COMPLETED)
    assert await journal.append(event) is JournalAppendStatus.APPENDED
    # 同一 event 重复写入必须是幂等 DUPLICATE，不产生第二行。
    assert await journal.append(event) is JournalAppendStatus.DUPLICATE
    assert await journal.last_sequence("run-duplicate") == 1
    page = await journal.read_after("run-duplicate", 0, 10)
    assert len(page) == 1


async def test_journal_same_event_id_different_payload_is_rejected(
    clean_database: Database,
) -> None:
    journal = await _journal(clean_database)
    event = _draft("run-conflict", 1, RuntimeEventType.STEP_COMPLETED)
    assert await journal.append(event) is JournalAppendStatus.APPENDED
    conflicting = _draft("run-conflict", 1, RuntimeEventType.STEP_COMPLETED,
                         component="other_component")
    object.__setattr__(conflicting, "event_id", event.event_id)
    with pytest.raises(JournalError) as excinfo:
        await journal.append(conflicting)
    assert excinfo.value.error_code is JournalErrorCode.EVENT_ID_CONFLICT


async def test_journal_sequence_conflict_is_rejected(
    clean_database: Database,
) -> None:
    journal = await _journal(clean_database)
    assert (
        await journal.append(_draft("run-seq", 1, RuntimeEventType.STEP_COMPLETED))
        is JournalAppendStatus.APPENDED
    )
    with pytest.raises(JournalError) as excinfo:
        await journal.append(
            _draft("run-seq", 1, RuntimeEventType.STEP_COMPLETED)
        )
    assert excinfo.value.error_code is JournalErrorCode.SEQUENCE_CONFLICT


async def test_journal_out_of_order_is_rejected(clean_database: Database) -> None:
    journal = await _journal(clean_database)
    await journal.append(_draft("run-order", 3, RuntimeEventType.STEP_COMPLETED))
    with pytest.raises(JournalError) as excinfo:
        await journal.append(_draft("run-order", 2, RuntimeEventType.STEP_COMPLETED))
    assert excinfo.value.error_code is JournalErrorCode.OUT_OF_ORDER


async def test_journal_terminal_invariant_holds(clean_database: Database) -> None:
    journal = await _journal(clean_database)
    run_id = "run-terminal"
    await journal.append(_draft(run_id, 1, RuntimeEventType.STEP_COMPLETED))
    assert (
        await journal.append(_draft(run_id, 2, RuntimeEventType.RUN_COMPLETED))
        is JournalAppendStatus.APPENDED
    )
    # 终态之后不得再追加。
    with pytest.raises(JournalError) as excinfo:
        await journal.append(_draft(run_id, 3, RuntimeEventType.STEP_COMPLETED))
    assert excinfo.value.error_code is JournalErrorCode.RUN_ALREADY_TERMINAL


async def test_journal_database_rejects_second_terminal_row(
    clean_database: Database,
) -> None:
    """terminal 唯一性由数据库 partial unique index 强制，不只是应用校验。"""
    from sqlalchemy import text

    journal = await _journal(clean_database)
    run_id = "run-terminal-db"
    await journal.append(_draft(run_id, 1, RuntimeEventType.RUN_COMPLETED))
    async with clean_database.engine.begin() as connection:
        with pytest.raises(Exception):
            await connection.execute(
                text(
                    "INSERT INTO runtime_event_journal ("
                    "journal_schema_version, event_schema_version, event_id,"
                    "run_id, trace_id, sequence, emitted_at, journaled_at,"
                    "event_type, component, safe_payload, payload_digest,"
                    "event_digest) VALUES ("
                    "2, 1, 'manual-event', :run_id, 'trace', 2, now(), now(),"
                    "'RUN_COMPLETED', 'manual', '{}', :digest, :digest)"
                ),
                {"run_id": run_id, "digest": "0" * 64},
            )


async def test_journal_concurrent_append_same_sequence_is_consistent(
    clean_database: Database,
) -> None:
    """并发 append 必须收敛为 APPENDED + typed conflict，不能静默重复。"""
    run_id = "run-concurrent"
    journal_a = PostgresRunEventJournal(clean_database)
    journal_b = PostgresRunEventJournal(clean_database)

    results = await asyncio.gather(
        journal_a.append(_draft(run_id, 1, RuntimeEventType.STEP_COMPLETED)),
        journal_b.append(_draft(run_id, 1, RuntimeEventType.STEP_COMPLETED)),
        return_exceptions=True,
    )
    appended = [r for r in results if r is JournalAppendStatus.APPENDED]
    assert len(appended) == 1
    assert await journal_a.last_sequence(run_id) == 1
    page = await journal_a.read_after(run_id, 0, 10)
    assert len(page) == 1


async def test_journal_evidence_survives_reconnect(clean_database: Database) -> None:
    run_id = "run-restart"
    journal = PostgresRunEventJournal(clean_database)
    await journal.append(_draft(run_id, 1, RuntimeEventType.STEP_COMPLETED))
    # 新的 Store + 新的连接：证据必须仍然存在。
    reopened = PostgresRunEventJournal(clean_database)
    assert await reopened.last_sequence(run_id) == 1
    page = await reopened.read_after(run_id, 0, 10)
    assert len(page) == 1
    page[0].verify()


# ---------------------------------------------------------------------------
# Snapshot contract
# ---------------------------------------------------------------------------


async def test_snapshot_write_read_and_digest(clean_database: Database) -> None:
    from tests._snapshot_helpers import build_snapshot

    store = PostgresSnapshotStore(clean_database)
    snapshot = build_snapshot("snap-1", "run-snap")
    assert await store.save(snapshot) is SnapshotSaveStatus.SAVED
    loaded = await store.get("snap-1")
    assert loaded is not None
    assert loaded.payload_digest == snapshot.payload_digest
    assert loaded.run_id == "run-snap"


async def test_snapshot_duplicate_and_conflict(clean_database: Database) -> None:
    from tests._snapshot_helpers import build_snapshot

    store = PostgresSnapshotStore(clean_database)
    snapshot = build_snapshot("snap-2", "run-snap2")
    assert await store.save(snapshot) is SnapshotSaveStatus.SAVED
    assert await store.save(snapshot) is SnapshotSaveStatus.DUPLICATE
    conflicting = build_snapshot("snap-2", "run-snap2", salt="other")
    with pytest.raises(SnapshotStoreError):
        await store.save(conflicting)


async def test_snapshot_latest_and_list(clean_database: Database) -> None:
    from tests._snapshot_helpers import build_snapshot

    store = PostgresSnapshotStore(clean_database)
    await store.save(build_snapshot("snap-old", "run-list"))
    await store.save(build_snapshot("snap-new", "run-list"))
    listed = await store.list_for_run("run-list", 10)
    assert {item.snapshot_id for item in listed} == {"snap-old", "snap-new"}
    # created_at DESC 排序必须自洽：latest 必须是列表首项。
    latest = await store.latest("run-list")
    assert latest is not None and latest.snapshot_id == listed[0].snapshot_id
    # 列表按 created_at 降序，因此不回退到升序。
    assert listed[0].created_at >= listed[1].created_at
    limited = await store.list_for_run("run-list", 1)
    assert len(limited) == 1


# ---------------------------------------------------------------------------
# Checkpoint contract
# ---------------------------------------------------------------------------


async def test_checkpoint_initial_advance_and_duplicate(
    clean_database: Database,
) -> None:
    store = PostgresEventConsumptionCheckpointStore(clean_database)
    assert await store.last_sequence("consumer", "run-cp") is None
    first = EventConsumptionCheckpoint(
        consumer_id="consumer",
        event_id="e1",
        run_id="run-cp",
        sequence=1,
        processed_at=datetime.now(UTC),
    )
    await store.save(first)
    assert await store.last_sequence("consumer", "run-cp") == 1
    # Duplicate 必须无损幂等。
    await store.save(first)
    assert await store.last_sequence("consumer", "run-cp") == 1
    await store.save(
        EventConsumptionCheckpoint(
            consumer_id="consumer",
            event_id="e2",
            run_id="run-cp",
            sequence=2,
            processed_at=datetime.now(UTC),
        )
    )
    assert await store.last_sequence("consumer", "run-cp") == 2
    assert (await store.get("consumer", "e1")).sequence == 1


async def test_checkpoint_stale_update_is_conflict(clean_database: Database) -> None:
    from core.runtime.event_consumer import EventConsumerError

    store = PostgresEventConsumptionCheckpointStore(clean_database)
    await store.save(
        EventConsumptionCheckpoint(
            consumer_id="c",
            event_id="e1",
            run_id="run-stale",
            sequence=5,
            processed_at=datetime.now(UTC),
        )
    )
    with pytest.raises(EventConsumerError):
        await store.save(
            EventConsumptionCheckpoint(
                consumer_id="c",
                event_id="e1",
                run_id="run-stale",
                sequence=4,
                processed_at=datetime.now(UTC),
            )
        )


async def test_checkpoint_survives_reconnect(clean_database: Database) -> None:
    store = PostgresEventConsumptionCheckpointStore(clean_database)
    await store.save(
        EventConsumptionCheckpoint(
            consumer_id="c",
            event_id="e1",
            run_id="run-persist",
            sequence=7,
            processed_at=datetime.now(UTC),
        )
    )
    reopened = PostgresEventConsumptionCheckpointStore(clean_database)
    assert await reopened.last_sequence("c", "run-persist") == 7
