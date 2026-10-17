"""仅保留仍有效的纯合同/本地测试与共享 fixture；废弃 agent-only 节点见 .ai/archive/tests/scoped_memory_lifecycle/。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.advanced_memory import (
    EpisodeGoal,
    EpisodeGoalAuthority,
    EpisodeObservation,
    EpisodeResult,
    EpisodeSituation,
    EpisodicMemoryRecord,
    MemoryOrigin,
    MemoryStatus,
    MemoryType,
    SemanticMemoryRecord,
)
from core.persistence.memory import (
    PostgresAdvancedMemoryStore,
    PostgresMemoryManager,
    PostgresProjectSemanticMemoryStore,
)

pytestmark = pytest.mark.asyncio





def _semantic(memory_id: str, *, value: str = "PostgreSQL", key: str | None = "database"):
    now = datetime.now(UTC)
    return SemanticMemoryRecord(
        memory_id=memory_id, agent_id="agent", memory_scope="direct",
        canonical_text=f"database is {value}", payload={"value": value}, logical_key=key,
        origin=MemoryOrigin("TEST", "run-" + memory_id, "exchange-" + memory_id, "agent", "direct"),
        memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE,
        created_at=now, updated_at=now,
    )





async def test_recovery_validator_async_accepts_postgres_snapshot(clean_database):
    from core.runtime.event_journal_store import PostgresRunEventJournal
    from core.runtime.recovery_validation import RecoveryValidator
    from core.runtime.snapshot_store import PostgresSnapshotStore
    from tests._recovery_fixtures import recovery_plan, recovery_snapshot

    plan = recovery_plan()
    snapshot = recovery_snapshot(plan=plan, sequence=0)
    snapshots = PostgresSnapshotStore(clean_database)
    await snapshots.save(snapshot)
    assessment = await RecoveryValidator(
        snapshot_store=snapshots,
        journal=PostgresRunEventJournal(clean_database),
    ).assess_async(snapshot_id=snapshot.snapshot_id, current_plan=plan)
    assert assessment.snapshot_id == snapshot.snapshot_id
