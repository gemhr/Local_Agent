"""WP4-B Memory Retrieval / Ranking / Selection deterministic tests。

全部使用真实 SQLite（MemoryManager 初始化 v2 DB）+ 真实
AdvancedMemoryStore + MemoryRetrievalService。属于 DETERMINISTIC
IMPLEMENTATION TEST，不是真实模型实验，也不是 vector / semantic retrieval。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from core.advanced_memory import (
    AdvancedMemoryStore,
    MemoryOrigin,
    MemoryStatus,
    MemoryType,
    SemanticMemoryRecord,
)
from core.memory_manager import MemoryManager
from core.runtime.memory_retrieval import (
    MEMORY_DIRECT_SCOPE,
    MemoryContextBundle,
    MemoryRetrievalError,
    MemoryRetrievalErrorCode,
    MemoryRetrievalService,
)
from core.runtime.memory_authorization import MemoryAccessPrincipal
from core.runtime.model_context import ContextSourceType, ContextTrustLevel


def origin(rid="run-1", eid="exchange-1") -> MemoryOrigin:
    return MemoryOrigin(
        origin_type="DELIVERED_EXCHANGE",
        origin_run_id=rid,
        origin_exchange_id=eid,
        origin_agent_id="core_router",
        origin_memory_scope="direct",
        formation_method="HYBRID",
    )


def make_store(tmp_path, name="memory.db") -> AdvancedMemoryStore:
    db_path = str(tmp_path / name)
    MemoryManager(db_path=db_path)
    return AdvancedMemoryStore(db_path)


def make_record(
    memory_id: str,
    *,
    canonical_text: str,
    value=None,
    key: str | None = None,
    agent_id: str = "core_router",
    memory_scope: str = "direct",
    created_at: str = "2026-01-01T00:00:00+00:00",
    payload: dict | None = None,
) -> SemanticMemoryRecord:
    ts = datetime.fromisoformat(created_at)
    if payload is None:
        payload = {"value": value}
    return SemanticMemoryRecord(
        memory_id=memory_id,
        agent_id=agent_id,
        memory_scope=memory_scope,
        canonical_text=canonical_text,
        payload=payload,
        origin=origin(),
        memory_type=MemoryType.SEMANTIC,
        status=MemoryStatus.ACTIVE,
        logical_key=key,
        created_at=ts,
        updated_at=ts,
    )


def service(store: AdvancedMemoryStore, **kw) -> MemoryRetrievalService:
    return MemoryRetrievalService(store, **kw)


def retrieve(store: AdvancedMemoryStore, query: str, **kw) -> MemoryContextBundle:
    return service(store, **kw).retrieve(
        requester=MemoryAccessPrincipal("core_router"),
        target_owner_agent_id="core_router",
        memory_scope=MEMORY_DIRECT_SCOPE,
        query=query,
    )


# ---------------------------------------------------------------------------
# Lexical relevance / registered / OPEN
# ---------------------------------------------------------------------------


def test_registered_active_semantic_fact_is_selected_for_related_query(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(
        make_record(
            "mem-pg",
            canonical_text="项目数据库使用 PostgreSQL",
            value="PostgreSQL",
            key="project.database",
        )
    )
    bundle = retrieve(store, "我们项目用什么数据库？")
    assert bundle.record_count == 1
    assert bundle.selected_count == 1
    assert bundle.registered_selected_count == 1
    assert bundle.open_selected_count == 0
    assert bundle.records[0].content == "项目数据库使用 PostgreSQL"
    assert bundle.records[0].source_type is ContextSourceType.MEMORY_RETRIEVAL
    assert bundle.records[0].trust_level is ContextTrustLevel.USER_CONTENT
    assert bundle.budget_used_chars == len("项目数据库使用 PostgreSQL")


def test_zero_lexical_relevance_is_never_selected(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(
        make_record(
            "mem-db",
            canonical_text="项目数据库使用 PostgreSQL",
            value="PostgreSQL",
            key="project.database",
        )
    )
    bundle = retrieve(store, "今天天气怎么样？")
    assert bundle.selected_count == 0
    assert bundle.record_count == 0
    assert bundle.evidence[0].drop_reason == "NO_LEXICAL_MATCH"


def test_registered_exact_logical_key_match_gets_structural_signal(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(
        make_record(
            "mem-key",
            canonical_text="fact-key-body",
            value="SQLite",
            key="project.database",
        )
    )
    bundle = retrieve(store, "project database 是什么配置")
    assert bundle.record_count == 1
    evidence = bundle.evidence[0]
    assert evidence.registered is True
    assert evidence.registered_exact_logical_key_match is True
    assert evidence.lexical_match_score >= 1


def test_open_unkeyed_fact_is_retrievable_under_same_policy(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(
        make_record(
            "mem-open",
            canonical_text="用户偏好使用 Python 编写脚本",
            value="Python",
            key=None,
        )
    )
    bundle = retrieve(store, "用户写脚本用什么语言？偏好 Python 吗")
    assert bundle.record_count == 1
    assert bundle.open_selected_count == 1
    assert bundle.registered_selected_count == 0
    assert bundle.evidence[0].registered is False


def test_open_fact_payload_is_matching_only_and_never_model_visible(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(
        make_record(
            "mem-open-payload",
            canonical_text="项目部署在内部集群",
            payload={"value": "kubernetes-secret-value"},
        )
    )
    bundle = retrieve(store, "项目部署在哪里？")
    assert bundle.record_count == 1
    # model-visible content 只有 canonical_text；payload value 不进入 context。
    rendered = "\n".join(record.content for record in bundle.records)
    assert "kubernetes-secret-value" not in rendered
    assert "项目部署在内部集群" in rendered


# ---------------------------------------------------------------------------
# Scope isolation
# ---------------------------------------------------------------------------


def test_scope_isolation_agent_and_partition(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(
        make_record(
            "mem-a",
            canonical_text="项目数据库使用 PostgreSQL",
            value="PostgreSQL",
            key="project.database",
        )
    )
    store.create(
        make_record(
            "mem-b",
            canonical_text="另一个 Agent 的数据库事实",
            value="Oracle",
            key="project.database",
            agent_id="data_analyst",
        )
    )
    store.create(
        make_record(
            "mem-shared-scope",
            canonical_text="共享 scope 的数据库事实",
            value="MySQL",
            key="project.database",
            memory_scope="shared",
        )
    )
    bundle = retrieve(store, "项目数据库用什么？")
    assert [record.content for record in bundle.records] == [
        "项目数据库使用 PostgreSQL"
    ]
    assert bundle.candidate_count == 1


# ---------------------------------------------------------------------------
# ACTIVE only / lifecycle integration
# ---------------------------------------------------------------------------


def test_superseded_row_excluded_active_winner_injected(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(
        make_record(
            "mem-sqlite",
            canonical_text="项目数据库使用 SQLite",
            value="SQLite",
            key="project.database",
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    # keyed lifecycle：PostgreSQL SUPERSEDE SQLite，partition 只剩一个 ACTIVE。
    result = store.resolve_semantic(
        make_record(
            "mem-pg",
            canonical_text="项目数据库使用 PostgreSQL",
            value="PostgreSQL",
            key="project.database",
            created_at="2026-01-02T00:00:00+00:00",
        )
    )
    assert result.operation.value == "SUPERSEDE"
    bundle = retrieve(store, "项目数据库用什么？")
    assert [record.content for record in bundle.records] == [
        "项目数据库使用 PostgreSQL"
    ]
    assert "SQLite" not in "\n".join(r.content for r in bundle.records)


def test_forgotten_rows_never_enter_context(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(
        make_record(
            "mem-pg",
            canonical_text="PostgreSQL",
            value="PostgreSQL",
            key="project.database",
        )
    )
    store.forget_semantic_partition(
        agent_id="core_router",
        memory_scope=MEMORY_DIRECT_SCOPE,
        logical_key="project.database",
    )
    bundle = retrieve(store, "PostgreSQL database")
    assert bundle.selected_count == 0
    assert bundle.record_count == 0
    assert bundle.candidate_count == 0
    # 正文永不出现：tombstone canonical_text 也不进 Model Context。
    assert "PostgreSQL" not in "\n".join(r.content for r in bundle.records)
    assert "[FORGOTTEN]" not in "\n".join(r.content for r in bundle.records)


# ---------------------------------------------------------------------------
# Deterministic ranking / tie-break / top-K / budget
# ---------------------------------------------------------------------------


def test_ranking_priority_registered_exact_after_lexical_score(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(
        make_record(
            "mem-open",
            canonical_text="database fact two-token overlap database db",
            value="x",
            key=None,
        )
    )
    store.create(
        make_record(
            "mem-key",
            canonical_text="key fact",
            value="y",
            key="project.database",
        )
    )
    bundle = retrieve(store, "project database")
    # 两者 lexical score 相同（project/database 各 1），registered exact 优先。
    assert [r.content for r in bundle.records] == ["key fact", "database fact two-token overlap database db"]
    assert bundle.evidence[0].registered_exact_logical_key_match is True
    assert bundle.evidence[0].rank == 1
    assert bundle.evidence[1].rank == 2


def test_tie_break_created_at_desc_then_memory_id_asc(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(
        make_record(
            "mem-2",
            canonical_text="database alpha",
            value="a",
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    store.create(
        make_record(
            "mem-3",
            canonical_text="database beta",
            value="b",
            created_at="2026-01-03T00:00:00+00:00",
        )
    )
    store.create(
        make_record(
            "mem-1",
            canonical_text="database gamma",
            value="c",
            created_at="2026-01-03T00:00:00+00:00",
        )
    )
    bundle = retrieve(store, "database", top_k=3)
    # created_at 相同 → memory_id ASC：mem-1(gamma) 在 mem-3(beta) 之前。
    assert [r.content for r in bundle.records] == [
        "database gamma",
        "database beta",
        "database alpha",
    ]
    assert [e.rank for e in bundle.evidence if e.selected] == [1, 2, 3]


def test_top_k_caps_selected_count_deterministically(tmp_path) -> None:
    store = make_store(tmp_path)
    for index in range(4):
        store.create(
            make_record(
                f"mem-{index}",
                canonical_text=f"database item {index}",
                value=str(index),
                created_at=f"2026-01-0{index + 1}T00:00:00+00:00",
            )
        )
    bundle = retrieve(store, "database item", top_k=2)
    assert bundle.selected_count == 2
    assert bundle.record_count == 2
    assert bundle.omitted_count == 2
    drop_reasons = [e.drop_reason for e in bundle.evidence if not e.selected]
    assert drop_reasons.count("TOP_K_EXCEEDED") == 2


def test_per_record_char_budget_drops_oversized_record(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(
        make_record(
            "mem-small",
            canonical_text="database small fact",
            value="s",
        )
    )
    store.create(
        make_record(
            "mem-large",
            canonical_text="database " + "x" * 100,
            value="l",
        )
    )
    bundle = retrieve(store, "database", max_memory_record_chars=50, top_k=5)
    assert [r.content for r in bundle.records] == ["database small fact"]
    reasons = {e.memory_id: e.drop_reason for e in bundle.evidence}
    assert reasons["mem-large"] == "RECORD_CHAR_BUDGET_EXCEEDED"


def test_total_context_char_budget_prefers_complete_records(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(
        make_record(
            "mem-first",
            canonical_text="database " + "a" * 30,
            value="1",
            created_at="2026-01-02T00:00:00+00:00",
        )
    )
    store.create(
        make_record(
            "mem-second",
            canonical_text="database " + "b" * 30,
            value="2",
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    bundle = retrieve(store, "database", max_memory_context_chars=50, top_k=5)
    # 两条各 39 chars：第二条完整放不下 → drop，不截断、不乱序、不抛异常。
    assert bundle.selected_count == 1
    assert bundle.record_count == 1
    assert bundle.budget_used_chars == 39
    reasons = {e.memory_id: e.drop_reason for e in bundle.evidence}
    assert reasons["mem-second"] == "CONTEXT_CHAR_BUDGET_EXCEEDED"


# ---------------------------------------------------------------------------
# Malformed rows / failure semantics
# ---------------------------------------------------------------------------


def test_malformed_historical_row_is_dropped_and_counted(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(
        make_record(
            "mem-good",
            canonical_text="database good row",
            value="g",
        )
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO long_term_memory (
                memory_id, memory_type, status, agent_id, memory_scope,
                canonical_text, payload, logical_key,
                origin_type, origin_run_id, origin_exchange_id,
                origin_agent_id, origin_memory_scope, formation_method,
                created_at, updated_at, superseded_by_memory_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "mem-bad",
                "SEMANTIC",
                "ACTIVE",
                "core_router",
                "direct",
                "database bad row",
                "not-valid-json",
                None,
                "DELIVERED_EXCHANGE",
                "run-1",
                "exchange-1",
                "core_router",
                "direct",
                "HYBRID",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                None,
            ),
        )
        conn.commit()
    bundle = retrieve(store, "database row")
    assert bundle.malformed_count == 1
    assert bundle.candidate_count == 2
    assert bundle.eligible_count == 1
    assert [r.content for r in bundle.records] == ["database good row"]


def test_store_failure_maps_to_typed_retrieval_error(tmp_path) -> None:
    # 没有 MemoryManager 初始化 schema → SQLite authority read 失败。
    broken_store = AdvancedMemoryStore(str(tmp_path / "missing-schema.db"))
    with pytest.raises(MemoryRetrievalError) as exc_info:
        retrieve(broken_store, "database")
    assert (
        exc_info.value.error_code == MemoryRetrievalErrorCode.UNAVAILABLE
    )


def test_bundle_is_immutable_and_projection_is_safe(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(
        make_record(
            "mem-pg",
            canonical_text="项目数据库使用 PostgreSQL",
            value="PostgreSQL",
            key="project.database",
        )
    )
    bundle = retrieve(store, "项目数据库")
    assert isinstance(bundle, MemoryContextBundle)
    with pytest.raises(Exception):
        bundle.records = ()  # type: ignore[misc]
    record = bundle.records[0]
    # 模型可见对象不暴露 memory_id / DB status / logical_key / score / payload。
    assert record.content == "项目数据库使用 PostgreSQL"
    assert record.source_type is ContextSourceType.MEMORY_RETRIEVAL
    assert record.trust_level is ContextTrustLevel.USER_CONTENT
    evidence = bundle.evidence[0]
    assert evidence.memory_id == "mem-pg"
    assert evidence.lexical_match_score >= 1
    assert evidence.selected is True


def test_invalid_configuration_fails_closed(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(MemoryRetrievalError):
        MemoryRetrievalService(store, top_k=0)
    with pytest.raises(MemoryRetrievalError):
        MemoryRetrievalService(store, candidate_limit=-1)
    with pytest.raises(MemoryRetrievalError):
        MemoryRetrievalService(store, max_memory_context_chars=0)
    with pytest.raises(MemoryRetrievalError):
        MemoryRetrievalService(object())  # type: ignore[arg-type]
