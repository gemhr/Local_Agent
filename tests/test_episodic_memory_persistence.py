"""WP6-B Episodic Memory domain/persistence foundation contracts."""

from __future__ import annotations

import sqlite3

import pytest

from core.advanced_memory import (
    AdvancedMemoryStore,
    EpisodeGoal,
    EpisodeGoalAuthority,
    EpisodeObservation,
    EpisodeResult,
    EpisodeSituation,
    EpisodicMemoryRecord,
    MemoryDomainError,
    MemoryOrigin,
    MemoryStatus,
    SemanticMemoryRecord,
)
from core.memory_manager import MEMORY_SCHEMA_VERSION, MemoryManager, memory_migrate, memory_preflight
from core.persistence_migration import PreflightStatus


def origin(run_id: str = "run-a") -> MemoryOrigin:
    return MemoryOrigin(
        origin_type="runtime_terminal",
        origin_run_id=run_id,
        origin_exchange_id=f"exchange-{run_id}",
        origin_agent_id="core_router",
        origin_memory_scope="direct",
        formation_method="EPISODIC_V1",
    )


def episode(**changes) -> EpisodicMemoryRecord:
    run_id = changes.pop("origin_run_id", "run-a")
    values = {
        "memory_id": "episode-a",
        "agent_id": "core_router",
        "memory_scope": "direct",
        "origin_run_id": run_id,
        "situation": EpisodeSituation("修复数据库迁移问题"),
        "goal": EpisodeGoal("安全完成迁移", EpisodeGoalAuthority.USER_PROVIDED),
        "observations": (
            EpisodeObservation("TOOL", "sqlite", "SUCCEEDED", result_digest="sha256:abc"),
        ),
        "result": EpisodeResult("SUCCEEDED", "COMPLETED", "DELIVERED"),
        "lesson": "先验证现有 schema。",
        "origin": origin(run_id),
    }
    values.update(changes)
    return EpisodicMemoryRecord(**values)


def store(tmp_path) -> AdvancedMemoryStore:
    path = tmp_path / "episode.db"
    MemoryManager(db_path=str(path))
    return AdvancedMemoryStore(str(path))


def test_episode_round_trip_idempotency_and_deterministic_renderer(tmp_path) -> None:
    memory = store(tmp_path)
    candidate = episode()
    assert candidate.canonical_text == episode(memory_id="episode-other").canonical_text
    created = memory.create_or_get_episode(candidate)
    retried = memory.create_or_get_episode(episode(memory_id="episode-new"))
    assert created.memory_id == retried.memory_id == "episode-a"
    loaded = memory.get_episode("episode-a", "core_router", "direct")
    assert loaded.to_payload() == candidate.to_payload()
    assert loaded.canonical_text == candidate.canonical_text


def test_different_runs_with_same_content_coexist_and_scope_isolates(tmp_path) -> None:
    memory = store(tmp_path)
    memory.create_or_get_episode(episode())
    memory.create_or_get_episode(episode(memory_id="episode-b", origin_run_id="run-b"))
    found = memory.list_active_episodic_for_scope("core_router", "direct", candidate_limit=10)
    assert [item.memory_id for item in found.records] == ["episode-b", "episode-a"]
    assert memory.list_by_agent("core_router") == []
    assert memory.list_active_semantic_for_scope("core_router", "direct", candidate_limit=10).records == ()
    with pytest.raises(MemoryDomainError):
        memory.get_episode("episode-a", "wrong-agent", "direct")
    with pytest.raises(MemoryDomainError):
        memory.get_episode("episode-a", "core_router", "wrong-scope")


def test_episode_rejects_non_active_type_mismatch_and_forbidden_logical_key(tmp_path) -> None:
    with pytest.raises(MemoryDomainError):
        episode(status=MemoryStatus.SUPERSEDED)
    with pytest.raises(MemoryDomainError):
        episode(memory_type="SEMANTIC")
    memory = store(tmp_path)
    memory.create_or_get_episode(episode())
    with sqlite3.connect(memory.db_path) as conn:
        conn.execute("UPDATE long_term_memory SET logical_key = 'forbidden' WHERE memory_id = 'episode-a'")
    assert memory.list_active_episodic_for_scope("core_router", "direct", candidate_limit=10).malformed_count == 1


@pytest.mark.parametrize("payload", [
    '{"schema_version":2}',
    '{"schema_version":1,"unknown":"opaque"}',
])
def test_episode_payload_version_and_unknown_fields_fail_closed(tmp_path, payload) -> None:
    memory = store(tmp_path)
    memory.create_or_get_episode(episode())
    with sqlite3.connect(memory.db_path) as conn:
        conn.execute("UPDATE long_term_memory SET payload = ? WHERE memory_id = 'episode-a'", [payload])
    with pytest.raises(MemoryDomainError):
        memory.get_episode("episode-a", "core_router", "direct")


def test_v2_to_v3_migration_preserves_semantic_row(tmp_path) -> None:
    path = tmp_path / "v2.db"
    manager = MemoryManager(db_path=str(path))
    manager.add_message("agent", "user", "hello")
    advanced = AdvancedMemoryStore(str(path))
    semantic = SemanticMemoryRecord(
        memory_id="semantic-1",
        agent_id="agent",
        memory_scope="direct",
        canonical_text="数据库使用 SQLite",
        payload={"value": "SQLite"},
        logical_key="project.database",
        origin=MemoryOrigin(
            origin_type="delivered_exchange",
            origin_run_id="semantic-run",
            origin_exchange_id="semantic-exchange",
            origin_agent_id="agent",
            origin_memory_scope="direct",
        ),
    )
    advanced.create(semantic)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX idx_long_term_memory_episodic_run_identity")
        conn.execute("DROP INDEX idx_long_term_memory_episodic_step_identity")
        conn.execute(
            "CREATE UNIQUE INDEX idx_long_term_memory_episodic_origin_run "
            "ON long_term_memory(memory_type, origin_run_id) "
            "WHERE memory_type = 'EPISODIC'"
        )
        conn.execute("PRAGMA user_version = 3")
    assert memory_preflight(str(path)).status is PreflightStatus.MIGRATION_REQUIRED
    memory_migrate(str(path))
    assert memory_preflight(str(path)).status is PreflightStatus.CURRENT
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == MEMORY_SCHEMA_VERSION
        assert conn.execute("SELECT content FROM messages").fetchone()[0] == "hello"
    assert advanced.get_by_memory_id("semantic-1") == semantic
