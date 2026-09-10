from __future__ import annotations

import pytest
from sqlalchemy import text

from core.runtime.event_journal_store import PostgresRunEventJournal
from core.runtime.events import RuntimeEvent, RuntimeEventDraft, RuntimeEventType, StepCompletedPayload
from core.runtime.recovery_contract import RecoveryStatus
from core.runtime.recovery_validation import RecoveryValidator
from core.runtime.snapshot_store import PostgresSnapshotStore
from tests._recovery_fixtures import recovery_plan, recovery_snapshot

pytestmark = pytest.mark.asyncio


async def test_postgres_recovery_rejects_corrupted_persisted_tail(clean_database):
    plan = recovery_plan()
    snapshot = recovery_snapshot(plan=plan, sequence=1)
    await PostgresSnapshotStore(clean_database).save(snapshot)
    event = RuntimeEvent.from_draft(RuntimeEventDraft(
        run_id="run", trace_id="trace", event_type=RuntimeEventType.STEP_COMPLETED,
        component="test", payload=StepCompletedPayload(status="SUCCEEDED"),
    ), 1)
    await PostgresRunEventJournal(clean_database).append(event)
    async with clean_database.engine.begin() as connection:
        await connection.execute(text("UPDATE runtime_event_journal SET safe_payload = '{}' WHERE run_id = 'run' AND sequence = 1"))
    assessment = await RecoveryValidator(
        snapshot_store=PostgresSnapshotStore(clean_database),
        journal=PostgresRunEventJournal(clean_database),
    ).assess_async(snapshot_id=snapshot.snapshot_id, current_plan=plan)
    assert assessment.status in {RecoveryStatus.CORRUPTED, RecoveryStatus.UNSUPPORTED}
