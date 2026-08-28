"""WP3-B Memory Lifecycle & Conflict Resolution deterministic tests。

覆盖：INSERT / typed equality / NO_CHANGE / SUPERSEDE / duplicate repair /
relation repair / atomicity / explicit FORGET / tombstone redaction / forget
idempotency / privacy / isolation / concurrency（真实多 SQLite 连接）。

全部使用真实 SQLite（MemoryManager 初始化 v2 DB）+ 真实 resolver/store，
属于 DETERMINISTIC IMPLEMENTATION TEST，不是真实模型或真实实验。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime

import pytest

from core.advanced_memory import (
    AdvancedMemoryStore,
    LifecycleOperation,
    MemoryDomainError,
    MemoryErrorCode,
    MemoryLifecycleResolver,
    MemoryOrigin,
    MemoryStatus,
    MemoryType,
    SemanticMemoryRecord,
    typed_values_equal,
)
from core.memory_manager import MemoryManager


def origin(rid="run-1", eid="exchange-1") -> MemoryOrigin:
    return MemoryOrigin(
        origin_type="DELIVERED_EXCHANGE",
        origin_run_id=rid,
        origin_exchange_id=eid,
        origin_agent_id="core_router",
        origin_memory_scope="direct",
        formation_method="HYBRID",
    )


def make_record(
    memory_id: str,
    value: object,
    key: str | None,
    *,
    agent_id: str = "core_router",
    memory_scope: str = "direct",
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> SemanticMemoryRecord:
    ts = datetime.fromisoformat(created_at)
    return SemanticMemoryRecord(
        memory_id=memory_id,
        agent_id=agent_id,
        memory_scope=memory_scope,
        canonical_text=f"fact {memory_id}",
        payload={"value": value},
        origin=origin(),
        memory_type=MemoryType.SEMANTIC,
        status=MemoryStatus.ACTIVE,
        logical_key=key,
        created_at=ts,
        updated_at=ts,
    )


def make_store(tmp_path, name="memory.db") -> AdvancedMemoryStore:
    db_path = str(tmp_path / name)
    MemoryManager(db_path=db_path)
    return AdvancedMemoryStore(db_path)


def _row_count(store: AdvancedMemoryStore) -> int:
    with sqlite3.connect(store.db_path) as conn:
        return int(
            conn.execute("SELECT COUNT(1) FROM long_term_memory").fetchone()[0]
        )


def _active_count(store: AdvancedMemoryStore, *, key: str) -> int:
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            "SELECT COUNT(1) FROM long_term_memory WHERE logical_key = ? "
            "AND status = ?",
            (key, MemoryStatus.ACTIVE.value),
        ).fetchall()
    return int(rows[0][0])


def _insert_row_raw(
    store: AdvancedMemoryStore,
    *,
    memory_id: str,
    value: object,
    key: str | None,
    status: str = "ACTIVE",
    created_at: str = "2026-01-01T00:00:00+00:00",
    superseded_by: str | None = None,
    agent_id: str = "core_router",
    memory_scope: str = "direct",
) -> None:
    """persistence-level controlled setup：直接插入任意 status 的 keyed row。"""
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO long_term_memory (
                memory_id, memory_type, status, agent_id, memory_scope,
                canonical_text, payload, logical_key,
                origin_type, origin_run_id, origin_exchange_id,
                origin_agent_id, origin_memory_scope, formation_method,
                created_at, updated_at, superseded_by_memory_id
            ) VALUES (?, 'SEMANTIC', ?, ?, ?, ?, ?, ?, 'DELIVERED_EXCHANGE',
                      'run-x', 'exchange-x', ?, 'direct', 'HYBRID', ?, ?, ?)
            """,
            (
                memory_id,
                status,
                agent_id,
                memory_scope,
                f"canonical-{memory_id}",
                json.dumps({"value": value}, ensure_ascii=False),
                key,
                agent_id,
                created_at,
                created_at,
                superseded_by,
            ),
        )


# ---------------------------------------------------------------------------
# INSERT
# ---------------------------------------------------------------------------


def test_no_key_always_inserts(tmp_path) -> None:
    store = make_store(tmp_path)
    result = store.resolve_semantic(make_record("mem-a", "x", None))
    assert result.operation is LifecycleOperation.INSERT
    assert result.candidate_outcome == "PERSISTED"
    assert result.new_memory_id == "mem-a"
    assert result.affected_count == 1
    assert store.get_by_memory_id("mem-a").status is MemoryStatus.ACTIVE


def test_keyed_no_active_inserts(tmp_path) -> None:
    store = make_store(tmp_path)
    result = store.resolve_semantic(
        make_record("mem-1", "PostgreSQL", "project.database")
    )
    assert result.operation is LifecycleOperation.INSERT
    assert store.get_by_memory_id("mem-1").logical_key == "project.database"


def test_same_id_retry_reuses_normalized_string_candidate(tmp_path) -> None:
    store = make_store(tmp_path)
    candidate = make_record("mem-1", "  PostgreSQL  ", "project.database")
    first = store.resolve_semantic(candidate)
    second = store.resolve_semantic(candidate)
    assert first.operation is LifecycleOperation.INSERT
    assert second.operation is LifecycleOperation.NO_CHANGE
    assert second.candidate_outcome == "REUSED"
    assert store.get_by_memory_id("mem-1").payload == {"value": "PostgreSQL"}


# ---------------------------------------------------------------------------
# Typed equality / NO_CHANGE
# ---------------------------------------------------------------------------


def test_typed_values_equal_contract() -> None:
    assert typed_values_equal("postgres", "postgres") is True
    assert typed_values_equal(" postgres ", "postgres") is True
    assert typed_values_equal("Postgres", "postgreSQL") is False
    assert typed_values_equal("Postgres", "PostgreSQL") is False
    assert typed_values_equal(1, 1) is True
    assert typed_values_equal(1, 1.0) is False
    assert typed_values_equal(1.0, 1.0) is True
    assert typed_values_equal(True, True) is True
    assert typed_values_equal(True, 1) is False
    assert typed_values_equal("1", 1) is False
    assert typed_values_equal(float("nan"), float("nan")) is False
    assert typed_values_equal(float("inf"), float("inf")) is False


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("PostgreSQL", " PostgreSQL "),
        (42, 42),
        (3.14, 3.14),
        (True, True),
    ],
)
def test_same_value_is_no_change(tmp_path, first, second) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(
        make_record("mem-1", first, "project.fact")
    )
    before = store.get_by_memory_id("mem-1")
    result = store.resolve_semantic(
        make_record("mem-2", second, "project.fact")
    )
    assert result.operation is LifecycleOperation.NO_CHANGE
    assert result.candidate_outcome == "NO_CHANGE"
    assert result.affected_count == 0
    assert _row_count(store) == 1
    after = store.get_by_memory_id("mem-1")
    # NO_CHANGE 不得修改 winner provenance / timestamps / relation。
    assert after == before
    assert after.updated_at == before.updated_at
    assert after.origin == before.origin


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Postgres", "PostgreSQL"),
        ("postgresql", "PostgreSQL"),
        ("SQLite", "sqlite"),
        (1, 1.0),
        (True, 1),
        ("1", 1),
    ],
)
def test_cross_value_is_conflict_supersede(tmp_path, first, second) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-1", first, "project.fact"))
    result = store.resolve_semantic(
        make_record("mem-2", second, "project.fact")
    )
    assert result.operation is LifecycleOperation.SUPERSEDE
    assert result.candidate_outcome == "PERSISTED"
    assert result.winner_memory_id == "mem-2"
    assert store.get_by_memory_id("mem-1").status is MemoryStatus.SUPERSEDED
    assert store.get_by_memory_id("mem-2").status is MemoryStatus.ACTIVE
    assert store.get_by_memory_id("mem-1").superseded_by_memory_id == "mem-2"


def test_malformed_historical_payload_fails_closed_zero_mutation(tmp_path) -> None:
    store = make_store(tmp_path)
    _insert_row_raw(
        store, memory_id="mem-hist", value={"nested": 1}, key="project.fact"
    )
    before = _row_count(store)
    with pytest.raises(MemoryDomainError) as exc:
        store.resolve_semantic(
            make_record("mem-new", "PostgreSQL", "project.fact")
        )
    assert exc.value.error_code == MemoryErrorCode.MALFORMED_KEYED_PAYLOAD
    assert _row_count(store) == before
    row = store.get_by_memory_id("mem-hist")
    assert row.status is MemoryStatus.ACTIVE
    assert row.payload == {"value": {"nested": 1}}


def test_invalid_json_historical_payload_is_typed_fail_closed(tmp_path) -> None:
    store = make_store(tmp_path)
    _insert_row_raw(
        store, memory_id="mem-hist", value="SQLite", key="project.database"
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE long_term_memory SET payload = ? WHERE memory_id = ?",
            ("{invalid-json", "mem-hist"),
        )
    with pytest.raises(MemoryDomainError) as exc:
        store.resolve_semantic(
            make_record("mem-new", "PostgreSQL", "project.database")
        )
    assert exc.value.error_code == MemoryErrorCode.MALFORMED_KEYED_PAYLOAD
    assert _row_count(store) == 1


# ---------------------------------------------------------------------------
# SUPERSEDE / repair
# ---------------------------------------------------------------------------


def test_one_old_active_becomes_superseded_with_relation(tmp_path) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-old", "SQLite", "project.database"))
    result = store.resolve_semantic(
        make_record("mem-new", "PostgreSQL", "project.database")
    )
    assert result.operation is LifecycleOperation.SUPERSEDE
    assert result.winner_memory_id == "mem-new"
    assert result.affected_count == 2
    old = store.get_by_memory_id("mem-old")
    assert old.status is MemoryStatus.SUPERSEDED
    assert old.superseded_by_memory_id == "mem-new"
    new = store.get_by_memory_id("mem-new")
    assert new.status is MemoryStatus.ACTIVE
    assert new.superseded_by_memory_id is None
    assert _active_count(store, key="project.database") == 1


def test_multiple_conflicting_active_all_point_to_new_winner(tmp_path) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-sqlite", "SQLite", "project.database"))
    store.resolve_semantic(make_record("mem-mysql", "MySQL", "project.database"))
    result = store.resolve_semantic(
        make_record("mem-pg", "PostgreSQL", "project.database")
    )
    assert result.operation is LifecycleOperation.SUPERSEDE
    assert result.winner_memory_id == "mem-pg"
    assert store.get_by_memory_id("mem-sqlite").superseded_by_memory_id == "mem-pg"
    assert store.get_by_memory_id("mem-mysql").superseded_by_memory_id == "mem-pg"
    assert store.get_by_memory_id("mem-pg").superseded_by_memory_id is None
    assert _active_count(store, key="project.database") == 1


def test_same_value_duplicate_active_picks_deterministic_existing_winner(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    _insert_row_raw(
        store,
        memory_id="mem-a",
        value="PostgreSQL",
        key="project.database",
        created_at="2026-01-01T00:00:00+00:00",
    )
    _insert_row_raw(
        store,
        memory_id="mem-b",
        value="PostgreSQL",
        key="project.database",
        created_at="2026-01-02T00:00:00+00:00",
    )
    result = store.resolve_semantic(
        make_record("mem-c", "PostgreSQL", "project.database")
    )
    # 真实发生旧 row mutation → operation SUPERSEDE，但 candidate 不插入。
    assert result.operation is LifecycleOperation.SUPERSEDE
    assert result.candidate_outcome == "NO_CHANGE"
    assert result.winner_memory_id == "mem-a"  # created_at ASC → mem-a
    assert _row_count(store) == 2
    assert store.get_by_memory_id("mem-a").status is MemoryStatus.ACTIVE
    assert store.get_by_memory_id("mem-b").status is MemoryStatus.SUPERSEDED
    assert store.get_by_memory_id("mem-b").superseded_by_memory_id == "mem-a"


def test_existing_pg_winner_is_reused_no_third_pg_row(tmp_path) -> None:
    store = make_store(tmp_path)
    # 手工构造“SQLite ACTIVE + PostgreSQL ACTIVE 同时存在”的 WP2 历史冲突。
    _insert_row_raw(store, memory_id="mem-sqlite", value="SQLite", key="project.database")
    _insert_row_raw(store, memory_id="mem-pg", value="PostgreSQL", key="project.database")
    result = store.resolve_semantic(
        make_record("mem-pg2", "PostgreSQL", "project.database")
    )
    # 已有 PostgreSQL ACTIVE winner → 复用；SQLite 被修复指向 PG；不产生第三条。
    assert result.operation is LifecycleOperation.SUPERSEDE
    assert result.candidate_outcome == "NO_CHANGE"
    assert result.winner_memory_id == "mem-pg"
    assert _row_count(store) == 2
    assert store.get_by_memory_id("mem-sqlite").superseded_by_memory_id == "mem-pg"
    assert store.get_by_memory_id("mem-pg").status is MemoryStatus.ACTIVE
    with pytest.raises(MemoryDomainError) as exc:
        store.get_by_memory_id("mem-pg2")
    assert exc.value.error_code == MemoryErrorCode.NOT_FOUND


def test_historical_supersede_chain_repoints_direct_to_latest_winner(tmp_path) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-sqlite", "SQLite", "project.database"))
    store.resolve_semantic(make_record("mem-pg", "PostgreSQL", "project.database"))
    result = store.resolve_semantic(
        make_record("mem-cockroach", "CockroachDB", "project.database")
    )
    assert result.operation is LifecycleOperation.SUPERSEDE
    assert result.winner_memory_id == "mem-cockroach"
    sqlite = store.get_by_memory_id("mem-sqlite")
    pg = store.get_by_memory_id("mem-pg")
    cockroach = store.get_by_memory_id("mem-cockroach")
    assert sqlite.superseded_by_memory_id == "mem-cockroach"
    assert pg.superseded_by_memory_id == "mem-cockroach"
    assert cockroach.superseded_by_memory_id is None


def test_no_active_partition_repoints_history_to_inserted_winner(tmp_path) -> None:
    store = make_store(tmp_path)
    _insert_row_raw(
        store,
        memory_id="mem-old",
        value="SQLite",
        key="project.database",
        status=MemoryStatus.SUPERSEDED.value,
        superseded_by="mem-missing",
    )
    result = store.resolve_semantic(
        make_record("mem-new", "PostgreSQL", "project.database")
    )
    assert result.operation is LifecycleOperation.INSERT
    assert result.affected_count == 2
    assert store.get_by_memory_id("mem-old").superseded_by_memory_id == "mem-new"
    assert [t.memory_id for t in result.affected_transitions] == ["mem-old"]


def test_transition_evidence_includes_repoint_and_is_sorted(tmp_path) -> None:
    store = make_store(tmp_path)
    _insert_row_raw(
        store,
        memory_id="mem-z",
        value="legacy",
        key="project.database",
        status=MemoryStatus.SUPERSEDED.value,
        superseded_by="mem-old",
    )
    _insert_row_raw(
        store,
        memory_id="mem-b",
        value="SQLite",
        key="project.database",
    )
    result = store.resolve_semantic(
        make_record("mem-new", "PostgreSQL", "project.database")
    )
    assert [t.memory_id for t in result.affected_transitions] == ["mem-b", "mem-z"]
    assert result.affected_transitions[1].before_status == "SUPERSEDED"
    assert result.affected_transitions[1].after_status == "SUPERSEDED"


# ---------------------------------------------------------------------------
# Atomicity（inject failures → full rollback）
# ---------------------------------------------------------------------------


def test_new_winner_insert_failure_rolls_back_all(tmp_path, monkeypatch) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-old", "SQLite", "project.database"))

    def boom(conn, record):
        raise sqlite3.Error("forced insert failure")

    monkeypatch.setattr(store, "_insert_row", boom)
    with pytest.raises(MemoryDomainError) as exc:
        store.resolve_semantic(
            make_record("mem-new", "PostgreSQL", "project.database")
        )
    assert exc.value.error_code == MemoryErrorCode.PERSISTENCE_FAILED
    monkeypatch.undo()
    # 旧 row 仍是 ACTIVE，没有被部分标记 SUPERSEDED；新 row 不存在。
    assert _row_count(store) == 1
    assert store.get_by_memory_id("mem-old").status is MemoryStatus.ACTIVE


def test_old_status_update_failure_rolls_back_all(tmp_path, monkeypatch) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-old", "SQLite", "project.database"))

    def boom(conn, plan):
        raise sqlite3.Error("forced update failure")

    monkeypatch.setattr(store, "_apply_plan", boom)
    with pytest.raises(MemoryDomainError) as exc:
        store.resolve_semantic(
            make_record("mem-new", "PostgreSQL", "project.database")
        )
    assert exc.value.error_code == MemoryErrorCode.PERSISTENCE_FAILED
    monkeypatch.undo()
    assert _row_count(store) == 1
    assert store.get_by_memory_id("mem-old").status is MemoryStatus.ACTIVE


def test_post_state_validation_failure_rolls_back_all(tmp_path, monkeypatch) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-old", "SQLite", "project.database"))

    def boom(conn, candidate, plan):
        raise MemoryDomainError(
            MemoryErrorCode.PERSISTENCE_FAILED, "forced post-state failure"
        )

    monkeypatch.setattr(store, "_validate_post_state", boom)
    with pytest.raises(MemoryDomainError) as exc:
        store.resolve_semantic(
            make_record("mem-new", "PostgreSQL", "project.database")
        )
    assert exc.value.error_code == MemoryErrorCode.PERSISTENCE_FAILED
    monkeypatch.undo()
    assert _row_count(store) == 1
    assert store.get_by_memory_id("mem-old").status is MemoryStatus.ACTIVE


def test_relation_repair_failure_rolls_back_all(tmp_path, monkeypatch) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-sqlite", "SQLite", "project.database"))
    store.resolve_semantic(make_record("mem-pg", "PostgreSQL", "project.database"))

    def boom(conn, plan):
        raise sqlite3.Error("forced repoint failure")

    monkeypatch.setattr(store, "_apply_plan", boom)
    with pytest.raises(MemoryDomainError):
        store.resolve_semantic(
            make_record("mem-new", "MySQL", "project.database")
        )
    monkeypatch.undo()
    # 全部 rollback：mem-sqlite 仍指向 mem-pg，mem-new 不存在。
    assert _row_count(store) == 2
    assert store.get_by_memory_id("mem-sqlite").superseded_by_memory_id == "mem-pg"


# ---------------------------------------------------------------------------
# Explicit Forget（store level）
# ---------------------------------------------------------------------------


def test_forget_all_versions_redacts_and_retains_key(tmp_path) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-sqlite", "SQLite", "project.database"))
    store.resolve_semantic(make_record("mem-pg", "PostgreSQL", "project.database"))
    result = store.forget_semantic_partition(
        agent_id="core_router",
        memory_scope="direct",
        logical_key="project.database",
    )
    assert result.operation is LifecycleOperation.FORGET
    assert result.outcome == "OK"
    assert result.affected_count == 2
    for memory_id in ("mem-sqlite", "mem-pg"):
        row = store.get_by_memory_id(memory_id)
        assert row.status is MemoryStatus.FORGOTTEN
        assert row.canonical_text == "[FORGOTTEN]"
        assert row.payload == {}
        assert row.superseded_by_memory_id is None
        assert row.logical_key == "project.database"
        assert row.memory_id == memory_id


def test_forget_repeat_is_already_forgotten_no_timestamp_change(tmp_path) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-1", "SQLite", "project.database"))
    first = store.forget_semantic_partition(
        agent_id="core_router", memory_scope="direct", logical_key="project.database"
    )
    assert first.operation is LifecycleOperation.FORGET
    tombstone_time = store.get_by_memory_id("mem-1").updated_at
    second = store.forget_semantic_partition(
        agent_id="core_router", memory_scope="direct", logical_key="project.database"
    )
    assert second.operation is LifecycleOperation.NO_CHANGE
    assert second.outcome == "ALREADY_FORGOTTEN"
    assert store.get_by_memory_id("mem-1").updated_at == tombstone_time


def test_forget_never_existed_returns_not_found_zero_mutation(tmp_path) -> None:
    store = make_store(tmp_path)
    result = store.forget_semantic_partition(
        agent_id="core_router", memory_scope="direct", logical_key="project.nope"
    )
    assert result.operation is LifecycleOperation.FORGET
    assert result.outcome == "NOT_FOUND"
    assert result.affected_count == 0
    assert _row_count(store) == 0


def test_forget_unsafe_forgotten_tombstone_is_repaired(tmp_path) -> None:
    store = make_store(tmp_path)
    # 一个 FORGOTTEN 但正文/payload 未 redact 的“不安全 tombstone”。
    _insert_row_raw(
        store,
        memory_id="mem-bad",
        value="PostgreSQL",
        key="project.database",
        status="FORGOTTEN",
    )
    result = store.forget_semantic_partition(
        agent_id="core_router", memory_scope="direct", logical_key="project.database"
    )
    assert result.operation is LifecycleOperation.FORGET
    assert result.outcome == "OK"
    assert len(result.affected_transitions) == 1
    assert result.affected_transitions[0].before_status == "FORGOTTEN"
    assert result.affected_transitions[0].after_status == "FORGOTTEN"
    row = store.get_by_memory_id("mem-bad")
    assert row.status is MemoryStatus.FORGOTTEN
    assert row.canonical_text == "[FORGOTTEN]"
    assert row.payload == {}
    assert row.superseded_by_memory_id is None


def test_forget_group_failure_rolls_back_all_versions(tmp_path, monkeypatch) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-1", "SQLite", "project.database"))
    store.resolve_semantic(make_record("mem-2", "PostgreSQL", "project.database"))

    def boom(conn, plan):
        raise sqlite3.Error("forced redact failure")

    monkeypatch.setattr(store, "_apply_plan", boom)
    with pytest.raises(MemoryDomainError):
        store.forget_semantic_partition(
            agent_id="core_router", memory_scope="direct", logical_key="project.database"
        )
    monkeypatch.undo()
    # 全事务 rollback：不允许留下 PostgreSQL forgotten 而 SQLite 原正文仍可见。
    rows = store.list_by_agent("core_router", active_only=False)
    assert {r.memory_id for r in rows} == {"mem-1", "mem-2"}
    assert store.get_by_memory_id("mem-2").status is MemoryStatus.ACTIVE
    assert store.get_by_memory_id("mem-2").payload == {"value": "PostgreSQL"}
    assert store.get_by_memory_id("mem-1").status is MemoryStatus.SUPERSEDED
    assert store.get_by_memory_id("mem-1").payload == {"value": "SQLite"}


# ---------------------------------------------------------------------------
# Isolation（partition 隔离）
# ---------------------------------------------------------------------------


def test_agent_isolation(tmp_path) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(
        make_record("mem-a", "SQLite", "project.database", agent_id="agent-a")
    )
    result = store.resolve_semantic(
        make_record("mem-b", "SQLite", "project.database", agent_id="agent-b")
    )
    assert result.operation is LifecycleOperation.INSERT  # 跨 agent 是独立 partition
    assert _active_count(store, key="project.database") == 2
    a = store.get_by_memory_id("mem-a")
    b = store.get_by_memory_id("mem-b")
    assert a.status is MemoryStatus.ACTIVE
    assert b.status is MemoryStatus.ACTIVE


def test_scope_isolation(tmp_path) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(
        make_record("mem-a", "SQLite", "project.database", memory_scope="direct")
    )
    result = store.resolve_semantic(
        make_record("mem-b", "SQLite", "project.database", memory_scope="orchestration")
    )
    assert result.operation is LifecycleOperation.INSERT


def test_logical_key_isolation(tmp_path) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-a", "SQLite", "project.a"))
    result = store.resolve_semantic(make_record("mem-b", "SQLite", "project.b"))
    assert result.operation is LifecycleOperation.INSERT


def test_forget_only_affects_target_partition(tmp_path) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-a", "SQLite", "project.database"))
    store.resolve_semantic(make_record("mem-b", "uv", "project.package_manager"))
    store.forget_semantic_partition(
        agent_id="core_router", memory_scope="direct", logical_key="project.database"
    )
    b = store.get_by_memory_id("mem-b")
    assert b.status is MemoryStatus.ACTIVE
    assert b.payload == {"value": "uv"}


# ---------------------------------------------------------------------------
# Concurrency（真实多个 SQLite 连接）
# ---------------------------------------------------------------------------


def test_concurrent_competing_updates_leave_one_active(tmp_path) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-init", "SQLite", "project.database"))
    results = []

    def run(store_path: str, memory_id: str, value: str) -> None:
        local = AdvancedMemoryStore(store_path)
        try:
            res = local.resolve_semantic(
                make_record(memory_id, value, "project.database")
            )
            results.append((memory_id, res.operation))
        except MemoryDomainError:
            results.append((memory_id, "ERROR"))

    threads = [
        threading.Thread(target=run, args=(store.db_path, "mem-pg", "PostgreSQL")),
        threading.Thread(target=run, args=(store.db_path, "mem-mysql", "MySQL")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    # 最终 invariant：keyed ACTIVE count <= 1。
    assert _active_count(store, key="project.database") == 1
    active = store.list_by_agent("core_router")
    assert len(active) == 1
    # 后提交者按 SQLite commit order 成为 winner，其余全部 SUPERSEDED 指向它。
    for r in store.list_by_agent("core_router", active_only=False):
        if r.memory_id == active[0].memory_id:
            assert r.status is MemoryStatus.ACTIVE
        else:
            assert r.status is MemoryStatus.SUPERSEDED
            assert r.superseded_by_memory_id == active[0].memory_id


def test_concurrent_same_value_updates_leave_one_active(tmp_path) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-init", "PostgreSQL", "project.database"))
    results = []

    def run(store_path: str, memory_id: str) -> None:
        local = AdvancedMemoryStore(store_path)
        try:
            res = local.resolve_semantic(
                make_record(memory_id, "PostgreSQL", "project.database")
            )
            results.append((memory_id, res.operation))
        except MemoryDomainError:
            results.append((memory_id, "ERROR"))

    threads = [
        threading.Thread(target=run, args=(store.db_path, "mem-pg-1")),
        threading.Thread(target=run, args=(store.db_path, "mem-pg-2")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert _active_count(store, key="project.database") == 1
    assert len([r for r in results if r[1] is LifecycleOperation.INSERT]) == 0
    assert len([r for r in results if r[1] is LifecycleOperation.NO_CHANGE]) == 2


def test_concurrent_forget_and_remember_linearize(tmp_path) -> None:
    store = make_store(tmp_path)
    store.resolve_semantic(make_record("mem-1", "SQLite", "project.database"))
    results = []

    def forget_run(store_path: str) -> None:
        local = AdvancedMemoryStore(store_path)
        try:
            res = local.forget_semantic_partition(
                agent_id="core_router",
                memory_scope="direct",
                logical_key="project.database",
            )
            results.append(("forget", res.operation))
        except MemoryDomainError:
            results.append(("forget", "ERROR"))

    def remember_run(store_path: str, memory_id: str, value: str) -> None:
        local = AdvancedMemoryStore(store_path)
        try:
            res = local.resolve_semantic(
                make_record(memory_id, value, "project.database")
            )
            results.append(("remember", res.operation))
        except MemoryDomainError:
            results.append(("remember", "ERROR"))

    threads = [
        threading.Thread(target=forget_run, args=(store.db_path,)),
        threading.Thread(
            target=remember_run, args=(store.db_path, "mem-new", "PostgreSQL")
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    # 按 SQLite commit order linearize：最终至多一个 ACTIVE，且若有 ACTIVE 则
    # 其正文完整；不可能出现 PostgreSQL forgotten 而 SQLite 原正文仍可见。
    active = store.list_by_agent("core_router")
    assert len(active) <= 1
    all_rows = store.list_by_agent("core_router", active_only=False)
    for r in all_rows:
        if r.status is MemoryStatus.FORGOTTEN:
            assert r.canonical_text == "[FORGOTTEN]"
            assert r.payload == {}
    if active:
        assert active[0].status is MemoryStatus.ACTIVE


# ---------------------------------------------------------------------------
# Resolver / store invariant guards
# ---------------------------------------------------------------------------


def test_store_rejects_candidate_with_supersede_relation(tmp_path) -> None:
    store = make_store(tmp_path)
    record = make_record("mem-1", "SQLite", "project.database")
    record = SemanticMemoryRecord(
        memory_id=record.memory_id,
        agent_id=record.agent_id,
        memory_scope=record.memory_scope,
        canonical_text=record.canonical_text,
        payload=record.payload,
        origin=record.origin,
        memory_type=record.memory_type,
        status=record.status,
        logical_key=record.logical_key,
        superseded_by_memory_id="mem-other",
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
    with pytest.raises(MemoryDomainError) as exc:
        store.resolve_semantic(record)
    assert exc.value.error_code == MemoryErrorCode.INVALID_ARGUMENT
    assert _row_count(store) == 0


def test_resolver_pure_function_winner_rule_deterministic(tmp_path) -> None:
    """winner rule = created_at ASC, memory_id ASC（不依赖 SQLite row order）。"""
    import sqlite3 as _sqlite3

    store = make_store(tmp_path)
    _insert_row_raw(
        store,
        memory_id="mem-z",
        value="PostgreSQL",
        key="project.database",
        created_at="2026-01-02T00:00:00+00:00",
    )
    _insert_row_raw(
        store,
        memory_id="mem-a",
        value="PostgreSQL",
        key="project.database",
        created_at="2026-01-01T00:00:00+00:00",
    )
    with _sqlite3.connect(store.db_path) as conn:
        conn.row_factory = _sqlite3.Row
        snapshot = tuple(
            conn.execute(
                "SELECT * FROM long_term_memory WHERE logical_key = ? "
                "ORDER BY created_at DESC, memory_id DESC",
                ("project.database",),
            ).fetchall()
        )
    candidate = make_record("mem-new", "PostgreSQL", "project.database")
    plan = MemoryLifecycleResolver.resolve_remember(
        candidate, snapshot, mutation_time=datetime.now(UTC)
    )
    assert plan.operation is LifecycleOperation.SUPERSEDE
    assert plan.candidate_outcome == "NO_CHANGE"
    # created_at ASC → mem-a 是 winner；即使快照顺序相反也不变。
    assert plan.winner_memory_id == "mem-a"
