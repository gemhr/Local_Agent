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


async def test_conversation_atomicity_visibility_and_fts(clean_database):
    store = PostgresMemoryManager(clean_database)
    await store.add_message("agent", "user", "legacy visible")
    await store.append_exchange_atomic(
        "agent", "direct", "PostgreSQL atomic user", "PostgreSQL atomic assistant", run_id="run-memory"
    )
    assert [m["role"] for m in await store.get_chat_history("agent", ascending=True)] == ["user", "user", "assistant"]
    assert len(await store.search_messages("PostgreSQL atomic")) == 2
    async with clean_database.transaction() as session:
        await session.execute(text("INSERT INTO message_exchanges (exchange_id, run_id, agent_id, memory_scope, state) VALUES ('pending-ex', 'pending-run', 'agent', 'direct', 'PENDING')"))
        await session.execute(text("INSERT INTO messages (agent_id, role, content, memory_scope, exchange_id, run_id, sequence) VALUES ('agent', 'user', 'hidden pending', 'direct', 'pending-ex', 'pending-run', 0)"))
    assert not await store.search_messages("hidden pending")
    assert await store.count_messages("agent") == 3


async def test_atomic_exchange_rolls_back_on_constraint_failure(clean_database):
    store = PostgresMemoryManager(clean_database)
    await store.append_exchange_atomic("agent", "direct", "u", "a", run_id="rollback-run")
    with pytest.raises(Exception):
        await store.append_exchange_atomic("agent", "direct", "u2", "a2", run_id="rollback-run")
    async with clean_database.session() as session:
        result = await session.execute(text("SELECT count(*) FROM messages WHERE run_id = 'rollback-run'"))
        assert result.scalar_one() == 2


def _semantic(memory_id: str, *, value: str = "PostgreSQL", key: str | None = "database"):
    now = datetime.now(UTC)
    return SemanticMemoryRecord(
        memory_id=memory_id, agent_id="agent", memory_scope="direct",
        canonical_text=f"database is {value}", payload={"value": value}, logical_key=key,
        origin=MemoryOrigin("TEST", "run-" + memory_id, "exchange-" + memory_id, "agent", "direct"),
        memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE,
        created_at=now, updated_at=now,
    )


async def test_advanced_jsonb_lifecycle_and_constraint(clean_database):
    store = PostgresAdvancedMemoryStore(clean_database)
    first = await store.create(_semantic("memory-a"))
    assert (await store.list_active_semantic_for_scope("agent", "direct", candidate_limit=10)).records == (first,)
    replaced = await store.resolve_semantic(_semantic("memory-b", value="SQLite"))
    assert replaced.outcome == "OK"
    assert {m.status for m in await store.list_by_agent("agent", active_only=False)} == {MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED}
    with pytest.raises(Exception):
        async with clean_database.engine.begin() as connection:
            await connection.execute(text("INSERT INTO long_term_memory (memory_id, memory_type, status, agent_id, memory_scope, canonical_text, payload, logical_key, origin_type, origin_run_id, origin_exchange_id, origin_agent_id, origin_memory_scope, created_at, updated_at) VALUES ('episode-duplicate', 'EPISODIC', 'ACTIVE', 'agent', 'direct', 'x', '{\"schema_version\":2,\"episode_kind\":\"RUN\"}', NULL, 'T', 'same-run', 'e', 'agent', 'direct', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"))


async def test_episodic_partial_identity_and_project_store(clean_database):
    store = PostgresAdvancedMemoryStore(clean_database)
    now = datetime.now(UTC)
    episode = EpisodicMemoryRecord(
        memory_id="episode-1", agent_id="agent", memory_scope="direct", origin_run_id="episode-run",
        situation=EpisodeSituation("repair"), goal=EpisodeGoal("finish", EpisodeGoalAuthority.USER_PROVIDED),
        observations=(EpisodeObservation("STEP", "db", "SUCCEEDED"),),
        result=EpisodeResult("SUCCEEDED", "COMPLETED", "DELIVERED"),
        origin=MemoryOrigin("TEST", "episode-run", "episode-exchange", "agent", "direct"),
        created_at=now, updated_at=now,
    )
    assert await store.create_or_get_episode(episode) == episode
    assert await store.create_or_get_episode(episode) == episode
    project = PostgresProjectSemanticMemoryStore(clean_database)
    from core.runtime.project_memory import ProjectSemanticRecord
    record = ProjectSemanticRecord("project-1", "project", "PROJECT", "text", {"value": "x"}, "ACTIVE", "agent", "run", "agent", "agent")
    assert (await project.mutate(record, supersede=False)).outcome == "CREATED"
    assert len(await project.active("project")) == 1


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
