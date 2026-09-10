from __future__ import annotations

import pytest
from sqlalchemy import text

from core.persistence.errors import DatabaseErrorCode, PersistenceError

pytestmark = pytest.mark.asyncio


async def test_postgres_transaction_failure_rolls_back_without_fake_journal_row(clean_database):
    with pytest.raises(PersistenceError) as exc_info:
        async with clean_database.transaction() as session:
            await session.execute(text("INSERT INTO runtime_event_journal (journal_schema_version, event_schema_version, event_id, run_id, trace_id, sequence, emitted_at, journaled_at, event_type, component, safe_payload, payload_digest, event_digest) VALUES (2, 1, 'fault-event', 'fault-run', 'trace', 0, now(), now(), 'STEP_COMPLETED', 'test', '{}', '0', '0')"))
    assert exc_info.value.error_code is DatabaseErrorCode.DATABASE_INTEGRITY_VIOLATION
    async with clean_database.session() as session:
        assert (await session.execute(text("SELECT count(*) FROM runtime_event_journal WHERE run_id = 'fault-run'"))).scalar_one() == 0
