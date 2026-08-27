"""WP1-B Advanced Memory Domain typed record / validation contract tests。

证明：SEMANTIC 合法 record 可构造；非法 type/status typed reject；payload
必须是可序列化 JSON object；provenance 字段非空；logical_key optional；
self-supersede relation 拒绝；时间戳必须是 UTC。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from core.advanced_memory import (
    MemoryDomainError,
    MemoryErrorCode,
    MemoryOrigin,
    MemoryStatus,
    MemoryType,
    SemanticMemoryRecord,
)


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


# ---------------------------------------------------------------------------
# valid record
# ---------------------------------------------------------------------------


def test_valid_semantic_record_constructs() -> None:
    mem = record()
    assert mem.memory_id == "mem-1"
    assert mem.memory_type is MemoryType.SEMANTIC
    assert mem.status is MemoryStatus.ACTIVE
    assert mem.logical_key is None
    assert mem.superseded_by_memory_id is None
    assert mem.origin.origin_run_id == "run-1"


def test_enum_vocabulary_is_frozen_to_semantic_and_triple_status() -> None:
    assert [member.value for member in MemoryType] == ["SEMANTIC"]
    assert [member.value for member in MemoryStatus] == [
        "ACTIVE",
        "SUPERSEDED",
        "FORGOTTEN",
    ]


def test_superseded_status_vocabulary_is_persistable_value() -> None:
    """SUPERSEDED / FORGOTTEN 是 lifecycle-capable persistence vocabulary，
    合法构造（不是 public create 入口）。"""
    mem = record(status="SUPERSEDED", superseded_by_memory_id="mem-2")
    assert mem.status is MemoryStatus.SUPERSEDED
    assert mem.superseded_by_memory_id == "mem-2"
    forgotten = record(memory_id="mem-3", status="FORGOTTEN")
    assert forgotten.status is MemoryStatus.FORGOTTEN


# ---------------------------------------------------------------------------
# invalid type / status
# ---------------------------------------------------------------------------


def test_invalid_memory_type_reject() -> None:
    with pytest.raises(MemoryDomainError) as exc:
        record(memory_type="EPISODIC")
    assert exc.value.error_code == MemoryErrorCode.UNSUPPORTED_TYPE
    with pytest.raises(MemoryDomainError) as exc:
        record(memory_type="PROCEDURAL")
    assert exc.value.error_code == MemoryErrorCode.UNSUPPORTED_TYPE
    with pytest.raises(MemoryDomainError) as exc:
        record(memory_type="UNKNOWN")
    assert exc.value.error_code == MemoryErrorCode.UNSUPPORTED_TYPE


def test_invalid_status_reject() -> None:
    with pytest.raises(MemoryDomainError) as exc:
        record(status="DELETED")
    assert exc.value.error_code == MemoryErrorCode.UNSUPPORTED_STATUS
    with pytest.raises(MemoryDomainError) as exc:
        record(status="UNKNOWN")
    assert exc.value.error_code == MemoryErrorCode.UNSUPPORTED_STATUS


# ---------------------------------------------------------------------------
# stable opaque identity
# ---------------------------------------------------------------------------


def test_identity_is_not_derived_from_content() -> None:
    """相同正文 + 不同 memory_id → 两条不同 identity 的合法 record。"""
    first = record(memory_id="mem-a")
    second = record(memory_id="mem-b")
    assert first.memory_id != second.memory_id
    assert first.canonical_text == second.canonical_text
    assert first.payload == second.payload


def test_identity_not_derived_from_logical_key() -> None:
    mem = record(logical_key="project_database")
    assert mem.memory_id == "mem-1"
    assert mem.logical_key == "project_database"
    assert mem.logical_key != mem.memory_id


# ---------------------------------------------------------------------------
# logical key
# ---------------------------------------------------------------------------


def test_logical_key_optional_and_preserved() -> None:
    assert record().logical_key is None
    mem = record(logical_key="profile.preferred_language")
    assert mem.logical_key == "profile.preferred_language"


def test_empty_logical_key_reject() -> None:
    with pytest.raises(MemoryDomainError) as exc:
        record(logical_key="   ")
    assert exc.value.error_code == MemoryErrorCode.INVALID_ARGUMENT


# ---------------------------------------------------------------------------
# canonical payload validation
# ---------------------------------------------------------------------------


def test_payload_must_be_json_object() -> None:
    with pytest.raises(MemoryDomainError) as exc:
        record(payload="not-an-object")
    assert exc.value.error_code == MemoryErrorCode.INVALID_ARGUMENT
    with pytest.raises(MemoryDomainError) as exc:
        record(payload=["array", "not", "object"])
    assert exc.value.error_code == MemoryErrorCode.INVALID_ARGUMENT


def test_payload_must_be_json_serializable() -> None:
    with pytest.raises(MemoryDomainError) as exc:
        record(payload={"bad": object()})
    assert exc.value.error_code == MemoryErrorCode.INVALID_ARGUMENT


def test_payload_nested_object_allowed() -> None:
    mem = record(payload={"project": {"name": "db", "engine": "sqlite"}})
    assert mem.payload["project"]["engine"] == "sqlite"


# ---------------------------------------------------------------------------
# provenance validation
# ---------------------------------------------------------------------------


def test_provenance_required_fields_non_empty() -> None:
    for field_name in (
        "origin_type",
        "origin_run_id",
        "origin_exchange_id",
        "origin_agent_id",
        "origin_memory_scope",
    ):
        with pytest.raises(MemoryDomainError) as exc:
            record(origin=origin(**{field_name: ""}))
        assert exc.value.error_code == MemoryErrorCode.INVALID_ARGUMENT, field_name


def test_origin_must_be_memory_origin() -> None:
    with pytest.raises(MemoryDomainError) as exc:
        record(origin={"origin_type": "x"})
    assert exc.value.error_code == MemoryErrorCode.INVALID_ARGUMENT


def test_empty_formation_method_reject() -> None:
    with pytest.raises(MemoryDomainError) as exc:
        record(origin=origin(formation_method=" "))
    assert exc.value.error_code == MemoryErrorCode.INVALID_ARGUMENT


def test_formation_method_round_trips_as_optional_field() -> None:
    mem = record(origin=origin(formation_method="rule_based"))
    assert mem.origin.formation_method == "rule_based"


# ---------------------------------------------------------------------------
# supersede relation
# ---------------------------------------------------------------------------


def test_self_supersede_relation_reject() -> None:
    with pytest.raises(MemoryDomainError) as exc:
        record(superseded_by_memory_id="mem-1")
    assert exc.value.error_code == MemoryErrorCode.INVALID_SUPERSEDE_SELF


def test_valid_supersede_relation_reference_allowed() -> None:
    mem = record(memory_id="mem-old", superseded_by_memory_id="mem-new")
    assert mem.superseded_by_memory_id == "mem-new"


def test_empty_supersede_reference_reject() -> None:
    with pytest.raises(MemoryDomainError) as exc:
        record(superseded_by_memory_id="")
    assert exc.value.error_code == MemoryErrorCode.INVALID_ARGUMENT


# ---------------------------------------------------------------------------
# timestamps
# ---------------------------------------------------------------------------


def test_timestamp_must_be_utc_aware() -> None:
    with pytest.raises(MemoryDomainError) as exc:
        record(created_at=datetime(2026, 1, 1, 12, 0, 0))
    assert exc.value.error_code == MemoryErrorCode.INVALID_ARGUMENT
    with pytest.raises(MemoryDomainError) as exc:
        record(updated_at=datetime.now(timezone(timedelta(hours=8))))
    assert exc.value.error_code == MemoryErrorCode.INVALID_ARGUMENT


def test_utc_timestamps_default_and_accepted() -> None:
    mem = record(created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
    assert mem.created_at == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert mem.updated_at.tzinfo is not None
    assert mem.updated_at.utcoffset() == timedelta(0)