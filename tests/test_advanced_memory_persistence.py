"""WP1-B Advanced Memory SQLite persistence foundation tests。

证明：create/read/provenance/payload/timestamp/logical-key round-trip；
agent partition 与 scope 过滤；active-only 默认读取；status-inclusive 读取；
stable identity；DB 唯一约束与幂等 contract；同 id 冲突拒绝且原 row 不变；
失败 create 无 partial row；public create 拒绝非 ACTIVE。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from core.advanced_memory import (
    AdvancedMemoryStore,
    MemoryDomainError,
    MemoryErrorCode,
    MemoryOrigin,
    MemoryStatus,
    SemanticMemoryRecord,
)
from core.memory_manager import MemoryManager


def origin(**kw) -> MemoryOrigin:
    base = dict(
        origin_type="delivered_exchange",
        origin_run_id="run-1",
        origin_exchange_id="exchange-1",
        origin_agent_id="core_router",
        origin_memory_scope="direct",
    )
    base.update(kw)
    return MemoryOrigin(**base)


def record(**kw) -> SemanticMemoryRecord:
    base = dict(
        memory_id="mem-1",
        agent_id="core_router",
        memory_scope="direct",
        canonical_text="数据库使用 SQLite",
        payload={"key": "database", "value": "SQLite"},
        origin=origin(),
    )
    base.update(kw)
    return SemanticMemoryRecord(**base)


def make_store(tmp_path) -> AdvancedMemoryStore:
    """构造与生产同构的 store：先由 MemoryManager 初始化 v2 schema（schema
    truth owner 是 memory_manager），再让窄 persistence boundary 使用同一 DB。"""
    db_path = str(tmp_path / "advanced_memory.db")
    MemoryManager(db_path=db_path)
    return AdvancedMemoryStore(db_path)


def _insert_lifecycle_row(
    store: AdvancedMemoryStore,
    *,
    memory_id: str,
    status: str,
    agent_id: str = "core_router",
    memory_scope: str = "direct",
    superseded_by: str | None = None,
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> None:
    """persistence-level controlled setup：直接插入非 ACTIVE record。

    仅用于 deterministic lifecycle serialization 测试；不通过 public create。
    """
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO long_term_memory (
                memory_id, memory_type, status, agent_id, memory_scope,
                canonical_text, payload, logical_key,
                origin_type, origin_run_id, origin_exchange_id,
                origin_agent_id, origin_memory_scope, formation_method,
                created_at, updated_at, superseded_by_memory_id
            ) VALUES (?, 'SEMANTIC', ?, ?, ?, ?, ?, NULL,
                      'delivered_exchange', 'run-x', 'exchange-x', ?, ?, NULL,
                      ?, ?, ?)
            """,
            (
                memory_id,
                status,
                agent_id,
                memory_scope,
                f"canonical-{memory_id}",
                "{}",
                agent_id,
                memory_scope,
                created_at,
                created_at,
                superseded_by,
            ),
        )


def _row_count(store: AdvancedMemoryStore) -> int:
    with sqlite3.connect(store.db_path) as conn:
        return int(
            conn.execute("SELECT COUNT(1) FROM long_term_memory").fetchone()[0]
        )


# ---------------------------------------------------------------------------
# round-trip
# ---------------------------------------------------------------------------


def test_create_and_read_round_trip(tmp_path) -> None:
    store = make_store(tmp_path)
    mem = record()
    created = store.create(mem)
    assert created.memory_id == mem.memory_id
    loaded = store.get_by_memory_id(mem.memory_id)
    assert loaded.memory_id == mem.memory_id
    assert loaded.canonical_text == mem.canonical_text
    assert loaded.agent_id == mem.agent_id
    assert loaded.memory_scope == mem.memory_scope
    assert loaded.memory_type is mem.memory_type
    assert loaded.status is mem.status


def test_provenance_round_trip(tmp_path) -> None:
    store = make_store(tmp_path)
    mem = record(
        origin=origin(
            origin_run_id="run-abc",
            origin_exchange_id="exchange-xyz",
            origin_agent_id="core_router",
            origin_memory_scope="direct",
            formation_method="rule_based",
        )
    )
    store.create(mem)
    loaded = store.get_by_memory_id(mem.memory_id)
    assert loaded.origin.origin_type == "delivered_exchange"
    assert loaded.origin.origin_run_id == "run-abc"
    assert loaded.origin.origin_exchange_id == "exchange-xyz"
    assert loaded.origin.origin_agent_id == "core_router"
    assert loaded.origin.origin_memory_scope == "direct"
    assert loaded.origin.formation_method == "rule_based"


def test_payload_round_trip(tmp_path) -> None:
    store = make_store(tmp_path)
    nested = {
        "project": {"name": "db", "engine": "sqlite", "tags": ["a", "b"]},
        "enabled": True,
        "count": 3,
    }
    mem = record(payload=nested)
    store.create(mem)
    assert store.get_by_memory_id(mem.memory_id).payload == nested


def test_timestamp_round_trip(tmp_path) -> None:
    store = make_store(tmp_path)
    ts = datetime(2026, 2, 3, 4, 5, 6, 789000, tzinfo=UTC)
    mem = record(created_at=ts, updated_at=ts)
    store.create(mem)
    loaded = store.get_by_memory_id(mem.memory_id)
    assert loaded.created_at == ts
    assert loaded.updated_at == ts


def test_logical_key_round_trip(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(record(logical_key="profile.preferred_language"))
    assert (
        store.get_by_memory_id("mem-1").logical_key
        == "profile.preferred_language"
    )
    store.create(record(memory_id="mem-no-key"))
    assert store.get_by_memory_id("mem-no-key").logical_key is None


def test_stable_identity_after_persistence(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(record())
    loaded = store.get_by_memory_id("mem-1")
    assert loaded.memory_id == "mem-1"
    # lifecycle metadata（status）变化不改变 identity：fixture 直接改 status
    _insert_lifecycle_row(store, memory_id="mem-2", status="SUPERSEDED", superseded_by="mem-9")
    changed = store.get_by_memory_id("mem-2")
    assert changed.memory_id == "mem-2"
    assert changed.status is MemoryStatus.SUPERSEDED


def test_get_missing_raises_not_found(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(MemoryDomainError) as exc:
        store.get_by_memory_id("does-not-exist")
    assert exc.value.error_code == MemoryErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# partition / scope / lifecycle reads
# ---------------------------------------------------------------------------


def test_agent_partition_filtering(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(record(memory_id="mem-a", agent_id="agent-a", canonical_text="fact A"))
    store.create(record(memory_id="mem-b", agent_id="agent-b", canonical_text="fact B"))
    a = store.list_by_agent("agent-a")
    b = store.list_by_agent("agent-b")
    assert [r.memory_id for r in a] == ["mem-a"]
    assert [r.memory_id for r in b] == ["mem-b"]
    assert store.list_by_agent("agent-none") == []


def test_memory_scope_filtering(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(
        record(memory_id="mem-direct", memory_scope="direct")
    )
    store.create(
        record(
            memory_id="mem-orch",
            memory_scope="orchestration",
            origin=origin(origin_memory_scope="orchestration"),
        )
    )
    direct = store.list_by_agent("core_router", memory_scope="direct")
    orch = store.list_by_agent("core_router", memory_scope="orchestration")
    assert [r.memory_id for r in direct] == ["mem-direct"]
    assert [r.memory_id for r in orch] == ["mem-orch"]
    all_scope = store.list_by_agent("core_router", memory_scope=None)
    assert {r.memory_id for r in all_scope} == {"mem-direct", "mem-orch"}


def test_active_only_read_is_default_and_suppresses_non_active(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(record(memory_id="mem-active"))
    _insert_lifecycle_row(store, memory_id="mem-superseded", status="SUPERSEDED")
    _insert_lifecycle_row(store, memory_id="mem-forgotten", status="FORGOTTEN")
    active = store.list_by_agent("core_router")
    assert [r.memory_id for r in active] == ["mem-active"]
    assert all(r.status is MemoryStatus.ACTIVE for r in active)


def test_status_inclusive_read(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(record(memory_id="mem-active"))
    _insert_lifecycle_row(store, memory_id="mem-superseded", status="SUPERSEDED", superseded_by="mem-active")
    _insert_lifecycle_row(store, memory_id="mem-forgotten", status="FORGOTTEN")
    all_records = store.list_by_agent("core_router", active_only=False)
    statuses = {r.memory_id: r.status for r in all_records}
    assert statuses["mem-active"] is MemoryStatus.ACTIVE
    assert statuses["mem-superseded"] is MemoryStatus.SUPERSEDED
    assert statuses["mem-forgotten"] is MemoryStatus.FORGOTTEN
    # 显式 status-inclusive 读取能读取 lifecycle state
    superseded = store.get_by_memory_id("mem-superseded")
    assert superseded.status is MemoryStatus.SUPERSEDED
    assert superseded.superseded_by_memory_id == "mem-active"


def test_supersede_relation_persistence_round_trip(tmp_path) -> None:
    """optional supersede relation 经 persistence-level controlled setup
    可稳定 round-trip（WP1 不提供 mutation API）。"""
    store = make_store(tmp_path)
    store.create(record(memory_id="mem-new"))
    _insert_lifecycle_row(
        store,
        memory_id="mem-old",
        status="SUPERSEDED",
        superseded_by="mem-new",
    )
    old = store.get_by_memory_id("mem-old")
    assert old.superseded_by_memory_id == "mem-new"
    assert old.status is MemoryStatus.SUPERSEDED


# ---------------------------------------------------------------------------
# public create rejects non-ACTIVE / unsupported type
# ---------------------------------------------------------------------------


def test_public_create_rejects_non_active(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(MemoryDomainError) as exc:
        store.create(record(status="SUPERSEDED"))
    assert exc.value.error_code == MemoryErrorCode.PUBLIC_CREATE_ACTIVE_ONLY
    with pytest.raises(MemoryDomainError) as exc:
        store.create(record(status="FORGOTTEN"))
    assert exc.value.error_code == MemoryErrorCode.PUBLIC_CREATE_ACTIVE_ONLY
    assert _row_count(store) == 0


def test_public_create_rejects_active_supersede_relation(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(MemoryDomainError) as exc:
        store.create(record(superseded_by_memory_id="mem-new"))
    assert exc.value.error_code == MemoryErrorCode.INVALID_ARGUMENT
    assert _row_count(store) == 0


def test_public_create_rejects_unsupported_type(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(MemoryDomainError) as exc:
        store.create(record(memory_type="EPISODIC"))
    assert exc.value.error_code == MemoryErrorCode.UNSUPPORTED_TYPE
    assert _row_count(store) == 0


# ---------------------------------------------------------------------------
# idempotency / unique identity
# ---------------------------------------------------------------------------


def test_unique_db_identity_prevents_second_row(tmp_path) -> None:
    store = make_store(tmp_path)
    original = record()
    store.create(original)
    assert _row_count(store) == 1
    # 同 id + 同 canonical record → 幂等成功，不产生第二 row
    result = store.create(original)
    assert _row_count(store) == 1
    assert result.memory_id == "mem-1"
    assert result == original


def test_same_id_different_record_is_rejected_and_original_unchanged(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(record(canonical_text="original-fact"))
    with pytest.raises(MemoryDomainError) as exc:
        store.create(record(canonical_text="different-fact"))
    assert exc.value.error_code == MemoryErrorCode.DUPLICATE_CONFLICT
    # 原 row 不变
    assert _row_count(store) == 1
    assert store.get_by_memory_id("mem-1").canonical_text == "original-fact"


def test_same_id_different_provenance_is_rejected(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(record())
    with pytest.raises(MemoryDomainError) as exc:
        store.create(record(origin=origin(origin_run_id="run-other")))
    assert exc.value.error_code == MemoryErrorCode.DUPLICATE_CONFLICT
    assert _row_count(store) == 1


def test_same_id_different_logical_key_is_rejected(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(record())
    with pytest.raises(MemoryDomainError) as exc:
        store.create(record(logical_key="project_database"))
    assert exc.value.error_code == MemoryErrorCode.DUPLICATE_CONFLICT
    assert _row_count(store) == 1


@pytest.mark.parametrize("timestamp_field", ["created_at", "updated_at"])
def test_same_id_different_timestamp_is_rejected(
    tmp_path, timestamp_field: str
) -> None:
    store = make_store(tmp_path)
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    original = record(created_at=ts, updated_at=ts)
    store.create(original)
    changed_timestamps = {"created_at": ts, "updated_at": ts}
    changed_timestamps[timestamp_field] = datetime(2026, 1, 2, tzinfo=UTC)
    changed = record(**changed_timestamps)
    with pytest.raises(MemoryDomainError) as exc:
        store.create(changed)
    assert exc.value.error_code == MemoryErrorCode.DUPLICATE_CONFLICT
    assert store.get_by_memory_id("mem-1") == original


def test_no_content_or_logical_key_dedup(tmp_path) -> None:
    """相同 content / logical_key 不自动 dedup：不同 memory_id 可共存。"""
    store = make_store(tmp_path)
    store.create(record(memory_id="mem-1", logical_key="project_database", canonical_text="same"))
    store.create(record(memory_id="mem-2", logical_key="project_database", canonical_text="same"))
    assert _row_count(store) == 2
    assert {r.memory_id for r in store.list_by_agent("core_router")} == {
        "mem-1",
        "mem-2",
    }


def test_multiple_records_same_origin_run_allowed(tmp_path) -> None:
    """origin_run_id 不设全局唯一：同一 Run 可形成多条 atomic fact。"""
    store = make_store(tmp_path)
    store.create(record(memory_id="mem-a", origin=origin(origin_run_id="run-1")))
    store.create(record(memory_id="mem-b", origin=origin(origin_run_id="run-1")))
    assert _row_count(store) == 2


# ---------------------------------------------------------------------------
# atomicity
# ---------------------------------------------------------------------------


def test_failed_create_leaves_no_partial_row(tmp_path, monkeypatch) -> None:
    store = make_store(tmp_path)
    mem = record()

    def boom(conn, rec):
        raise sqlite3.Error("forced insert failure")

    monkeypatch.setattr(store, "_insert_row", boom)
    with pytest.raises(MemoryDomainError) as exc:
        store.create(mem)
    assert exc.value.error_code == MemoryErrorCode.PERSISTENCE_FAILED
    monkeypatch.undo()
    # 无 partial row / 无 partial provenance
    assert _row_count(store) == 0
    with pytest.raises(MemoryDomainError) as not_found:
        store.get_by_memory_id(mem.memory_id)
    assert not_found.value.error_code == MemoryErrorCode.NOT_FOUND
    assert store.list_by_agent("core_router", active_only=False) == []


def test_conflicting_duplicate_is_fully_rejected_no_partial(tmp_path) -> None:
    """冲突 duplicate 不能留下 partial/inconsistent provenance。"""
    store = make_store(tmp_path)
    store.create(record(memory_id="mem-1"))
    with pytest.raises(MemoryDomainError):
        store.create(
            record(
                memory_id="mem-1",
                origin=origin(origin_run_id="run-2"),
            )
        )
    assert _row_count(store) == 1
    loaded = store.get_by_memory_id("mem-1")
    assert loaded.origin.origin_run_id == "run-1"
