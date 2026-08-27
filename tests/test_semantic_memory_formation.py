"""WP2-B Semantic Memory Formation component deterministic tests。

证明：strict parser fail-closed、code-owned Should-Remember policy、source
grounding、authoritative record mapping、multi-candidate semantics、
same-execution persistence retry idempotency、WP3 boundary（无 dedup/supersede）、
failure/cancellation/timeout 隔离与 content-minimized observation。

全部使用 fake extraction model + 真实 AdvancedMemoryStore（MemoryManager 初始化
的 v2 DB），属于 DETERMINISTIC IMPLEMENTATION TEST，不是真实 Formation 实验。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import List, Optional

import pytest

from core.advanced_memory import (
    AdvancedMemoryStore,
    MemoryDomainError,
    MemoryErrorCode,
    MemoryStatus,
    MemoryType,
)
from core.memory_manager import MemoryManager
from core.runtime import (
    CommittedExchangeReceipt,
    FormationCandidateOutcomeCode,
    FormationExtractionModel,
    InMemoryMetricsRecorder,
    InMemorySpanRecorder,
    RuntimeMetricsProjector,
    SemanticFormationErrorCode,
    SemanticFormationStatus,
    SemanticMemoryFormation,
    StrictFormationProposalParser,
)
from core.runtime.events import MemoryFormationCompletedPayload

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

USER_QUERY = "以后这个项目统一用 uv 管包，数据库换成 PostgreSQL。"
FINAL_ANSWER = "好的，已了解你的偏好。"


class FakeExtractionModel(FormationExtractionModel):
    """Deterministic fake：记录输入，返回预设 JSON。"""

    def __init__(self, output: Optional[str] = None, error: Optional[Exception] = None) -> None:
        self.output = output
        self.error = error
        self.calls = 0
        self.received_queries: List[str] = []
        self.received_answers: List[str] = []

    def extract(self, user_query: str, final_answer: str) -> str:
        self.calls += 1
        self.received_queries.append(user_query)
        self.received_answers.append(final_answer)
        if self.error is not None:
            raise self.error
        assert self.output is not None
        return self.output


def make_store(tmp_path) -> AdvancedMemoryStore:
    db_path = str(tmp_path / "formation.db")
    MemoryManager(db_path=db_path)
    return AdvancedMemoryStore(db_path)


def receipt(**kw) -> CommittedExchangeReceipt:
    base = dict(
        run_id="run-1",
        exchange_id="run-1",
        entry_agent_id="core_router",
        memory_scope="direct",
    )
    base.update(kw)
    return CommittedExchangeReceipt(**base)


def make_formation(
    tmp_path,
    extraction: FormationExtractionModel,
    *,
    store: Optional[AdvancedMemoryStore] = None,
    event_emitter=None,
    span_recorder=None,
    run_id: Optional[str] = "run-1",
) -> SemanticMemoryFormation:
    return SemanticMemoryFormation(
        entry_agent_id="core_router",
        user_request=USER_QUERY,
        memory_store=store or make_store(tmp_path),
        extraction_model=extraction,
        run_id=run_id,
        event_emitter=event_emitter,
        span_recorder=span_recorder,
    )


class FakeStore:
    """包装真实 store：可注入失败次数，记录收到的 record 对象。"""

    def __init__(self, real: AdvancedMemoryStore, fail_times: int = 0) -> None:
        self._real = real
        self.fail_times = fail_times
        self.received: List[object] = []

    def create(self, record):
        self.received.append(record)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise MemoryDomainError(MemoryErrorCode.PERSISTENCE_FAILED)
        return self._real.create(record)


def candidates_json(*candidates: dict) -> str:
    return json.dumps(
        {"schema_version": 1, "candidates": list(candidates)}, ensure_ascii=False
    )


def remember(
    *,
    category="PROJECT_STABLE_FACT",
    text="项目数据库使用 PostgreSQL。",
    value="PostgreSQL",
    key=None,
    excerpt="数据库换成 PostgreSQL",
    reason="EXPLICIT_PROJECT_FACT",
    **extra,
) -> dict:
    item = {
        "disposition": "REMEMBER",
        "category": category,
        "canonical_text": text,
        "value": value,
        "source_excerpt": excerpt,
        "reason_code": reason,
    }
    if key is not None:
        item["logical_key"] = key
    item.update(extra)
    return item


def ignore(excerpt="谢谢", reason="SMALL_TALK") -> dict:
    return {
        "disposition": "IGNORE",
        "category": "STABLE_USER_PREFERENCE",
        "canonical_text": "闲聊内容",
        "value": "small talk",
        "source_excerpt": excerpt,
        "reason_code": reason,
    }


# ---------------------------------------------------------------------------
# Parser（fail closed）
# ---------------------------------------------------------------------------


def test_parser_malformed_json_is_typed_failure() -> None:
    from core.runtime import SemanticFormationError

    with pytest.raises(SemanticFormationError) as excinfo:
        StrictFormationProposalParser.parse("not-json{")
    assert (
        excinfo.value.error_code is SemanticFormationErrorCode.OUTPUT_INVALID
    )


def test_parser_unknown_key_fails_closed() -> None:
    from core.runtime import SemanticFormationError

    raw = candidates_json(remember(confidence=0.9))
    with pytest.raises(SemanticFormationError) as excinfo:
        StrictFormationProposalParser.parse(raw)
    assert (
        excinfo.value.error_code
        is SemanticFormationErrorCode.OUTPUT_UNKNOWN_FIELD
    )


def test_parser_forbidden_authoritative_field_fails_closed() -> None:
    from core.runtime import SemanticFormationError

    raw = candidates_json(remember(memory_id="mem-evil"))
    with pytest.raises(SemanticFormationError) as excinfo:
        StrictFormationProposalParser.parse(raw)
    assert (
        excinfo.value.error_code
        is SemanticFormationErrorCode.OUTPUT_FORBIDDEN_FIELD
    )


def test_parser_oversized_batch_is_bounded_reject() -> None:
    from core.runtime import SemanticFormationError
    from core.runtime.semantic_memory_formation import FORMATION_MAX_CANDIDATES

    raw = candidates_json(
        *(remember(text=f"事实 {index}", excerpt="数据库换成 PostgreSQL", value=str(index)) for index in range(FORMATION_MAX_CANDIDATES + 1))
    )
    with pytest.raises(SemanticFormationError) as excinfo:
        StrictFormationProposalParser.parse(raw)
    assert (
        excinfo.value.error_code is SemanticFormationErrorCode.BATCH_TOO_LARGE
    )


def test_parser_rejects_wrong_schema_version() -> None:
    from core.runtime import SemanticFormationError

    raw = json.dumps({"schema_version": 2, "candidates": []})
    with pytest.raises(SemanticFormationError):
        StrictFormationProposalParser.parse(raw)


def test_parser_rejects_top_level_extra_field() -> None:
    from core.runtime import SemanticFormationError

    raw = json.dumps(
        {"schema_version": 1, "candidates": [], "memory_id": "x"}
    )
    with pytest.raises(SemanticFormationError):
        StrictFormationProposalParser.parse(raw)


# ---------------------------------------------------------------------------
# Policy：ACCEPT / IGNORE（deterministic pipeline with fake model proposal）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "text", "value", "key"),
    [
        ("STABLE_USER_PREFERENCE", "用户偏好使用 uv 管理依赖。", "uv", "project.package_manager"),
        ("PROJECT_STABLE_FACT", "项目数据库使用 PostgreSQL。", "PostgreSQL", "project.database"),
        ("ENGINEERING_CONSTRAINT", "项目禁止直接提交 main 分支。", True, "project.branch_protection"),
        ("LONG_TERM_DECISION", "项目长期使用 SQLite 作为本地存储。", "SQLite", None),
    ],
)
async def test_explicit_stable_statement_is_accepted(
    tmp_path, category, text, value, key
) -> None:
    extraction = FakeExtractionModel(
        candidates_json(
            remember(category=category, text=text, value=value, key=key)
        )
    )
    store = make_store(tmp_path)
    formation = make_formation(tmp_path, extraction, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.SUCCEEDED
    assert result.accepted_count == 1
    assert result.persisted_count == 1
    records = store.list_by_agent("core_router")
    assert len(records) == 1
    assert records[0].canonical_text == text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "excerpt"),
    [
        ("TRANSIENT_STATE", "今天"),
        ("ONE_OFF_OPERATION", "这次"),
        ("SMALL_TALK", "谢谢"),
        ("UNCERTAIN_WORDING", "可能"),
    ],
)
async def test_transient_or_smalltalk_proposal_is_ignored(
    tmp_path, reason, excerpt
) -> None:
    extraction = FakeExtractionModel(candidates_json(ignore(excerpt=excerpt, reason=reason)))
    store = make_store(tmp_path)
    formation = make_formation(tmp_path, extraction, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.SUCCEEDED
    assert result.accepted_count == 0
    assert result.ignored_count == 1
    assert store.list_by_agent("core_router") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("user_query", ["谢谢", "今天先临时用 pip 装一下。"])
async def test_obvious_non_persistent_source_is_rejected_before_model(
    tmp_path, user_query
) -> None:
    extraction = FakeExtractionModel(
        candidates_json(
            remember(
                category="LONG_TERM_DECISION",
                text="项目使用 pip。",
                value="pip",
                excerpt=user_query.rstrip("。"),
            )
        )
    )
    store = make_store(tmp_path)
    formation = SemanticMemoryFormation(
        entry_agent_id="core_router",
        user_request=user_query,
        memory_store=store,
        extraction_model=extraction,
        run_id="run-1",
    )
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.SUCCEEDED
    assert result.accepted_count == 0
    assert extraction.calls == 0
    assert store.list_by_agent("core_router") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_query",
    [
        "这个项目可能使用 PostgreSQL。",
        "工具返回显示数据库是 PostgreSQL。",
    ],
)
async def test_grounded_but_ineligible_source_cannot_be_upgraded_by_model(
    tmp_path, user_query
) -> None:
    extraction = FakeExtractionModel(
        candidates_json(
            remember(
                text="项目数据库使用 PostgreSQL。",
                value="PostgreSQL",
                excerpt=user_query.rstrip("。"),
            )
        )
    )
    store = make_store(tmp_path)
    formation = SemanticMemoryFormation(
        entry_agent_id="core_router",
        user_request=user_query,
        memory_store=store,
        extraction_model=extraction,
        run_id="run-1",
    )
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert extraction.calls == 1
    assert result.accepted_count == 0
    assert result.candidate_outcomes[0].outcome is (
        FormationCandidateOutcomeCode.IGNORED_INVALID
    )
    assert store.list_by_agent("core_router") == []


# ---------------------------------------------------------------------------
# Source safety
# ---------------------------------------------------------------------------


class _FakeFinalStore:
    """只提供 read_final_content 的最小 store fake。"""

    def read_final_content(self, step_id: str) -> str:
        return FINAL_ANSWER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "excerpt",
    [
        "根据知识库文档 PostgreSQL 更快",  # RAG content
        "工具返回显示磁盘已满",  # tool output
        "鲁迅说过时间就像海绵",  # third-party quote
    ],
)
async def test_ungrounded_candidate_is_invalid_and_not_persisted(
    tmp_path, excerpt
) -> None:
    extraction = FakeExtractionModel(
        candidates_json(remember(text="外部事实。", value="x", excerpt=excerpt))
    )
    store = make_store(tmp_path)
    formation = make_formation(tmp_path, extraction, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.SUCCEEDED
    assert result.accepted_count == 0
    assert result.ignored_count == 1
    assert result.candidate_outcomes[0].outcome is (
        FormationCandidateOutcomeCode.IGNORED_INVALID
    )
    assert store.list_by_agent("core_router") == []


@pytest.mark.asyncio
async def test_assistant_speculation_never_becomes_memory(tmp_path) -> None:
    # Model 试图把 assistant 建议升格为事实，但 source_excerpt 无法 ground
    # 到 original user query，被 code-owned validation 拒绝。
    extraction = FakeExtractionModel(
        candidates_json(
            remember(
                text="建议用户切换到 PostgreSQL。",
                value="PostgreSQL",
                excerpt="建议你切换到 PostgreSQL",
            )
        )
    )
    store = make_store(tmp_path)
    formation = make_formation(tmp_path, extraction, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.accepted_count == 0
    assert store.list_by_agent("core_router") == []


@pytest.mark.asyncio
async def test_private_reasoning_never_enters_formation_input(tmp_path) -> None:
    # Input allowlist：extraction 只收到 user query 与 delivered final answer。
    extraction = FakeExtractionModel(candidates_json())
    formation = make_formation(tmp_path, extraction, store=make_store(tmp_path))
    await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert extraction.calls == 1
    assert extraction.received_queries == [USER_QUERY]
    assert extraction.received_answers == [FINAL_ANSWER]


# ---------------------------------------------------------------------------
# Candidate-level validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_logical_key_makes_candidate_invalid(tmp_path) -> None:
    extraction = FakeExtractionModel(
        candidates_json(remember(key="Project.DataBase", value="PostgreSQL"))
    )
    store = make_store(tmp_path)
    formation = make_formation(tmp_path, extraction, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.ignored_count == 1
    assert result.candidate_outcomes[0].outcome is (
        FormationCandidateOutcomeCode.IGNORED_INVALID
    )
    assert store.list_by_agent("core_router") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, ["a"], {"value": "x"}])
async def test_invalid_payload_makes_candidate_invalid(tmp_path, value) -> None:
    extraction = FakeExtractionModel(
        candidates_json(remember(value=value))
    )
    store = make_store(tmp_path)
    formation = make_formation(tmp_path, extraction, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.ignored_count == 1
    assert store.list_by_agent("core_router") == []


@pytest.mark.asyncio
async def test_ungrounded_candidate_is_invalid(tmp_path) -> None:
    extraction = FakeExtractionModel(
        candidates_json(
            remember(text="用户住在上海。", value="上海", excerpt="住在上海")
        )
    )
    store = make_store(tmp_path)
    formation = make_formation(tmp_path, extraction, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.ignored_count == 1
    assert store.list_by_agent("core_router") == []


# ---------------------------------------------------------------------------
# Record mapping（authoritative fields）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accepted_candidate_maps_to_active_semantic_hybrid_record(
    tmp_path,
) -> None:
    extraction = FakeExtractionModel(
        candidates_json(
            remember(
                category="PROJECT_STABLE_FACT",
                text="项目数据库使用 PostgreSQL。",
                value="PostgreSQL",
                key="project.database",
            )
        )
    )
    store = make_store(tmp_path)
    formation = make_formation(tmp_path, extraction, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.persisted_count == 1
    memory_id = result.candidate_outcomes[0].memory_id
    assert memory_id and memory_id.startswith("mem-")
    record = store.get_by_memory_id(memory_id)
    assert record.memory_type is MemoryType.SEMANTIC
    assert record.status is MemoryStatus.ACTIVE
    assert record.payload == {"value": "PostgreSQL"}
    assert record.logical_key == "project.database"
    assert record.agent_id == "core_router"
    assert record.memory_scope == "direct"
    assert record.origin.origin_type == "DELIVERED_EXCHANGE"
    assert record.origin.formation_method == "HYBRID"
    assert record.origin.origin_run_id == "run-1"
    assert record.origin.origin_exchange_id == "run-1"
    assert record.origin.origin_agent_id == "core_router"
    assert record.origin.origin_memory_scope == "direct"
    assert record.superseded_by_memory_id is None


@pytest.mark.asyncio
async def test_model_cannot_specify_authoritative_fields(tmp_path) -> None:
    # Parser 对 forbidden 字段整体 fail closed（零写入），Model 无法注入
    # ID/status/type/origin/timestamp。
    extraction = FakeExtractionModel(
        candidates_json(
            remember(status="SUPERSEDED"),
        )
    )
    store = make_store(tmp_path)
    formation = make_formation(tmp_path, extraction, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.FAILED
    assert (
        result.safe_error_code
        == SemanticFormationErrorCode.OUTPUT_FORBIDDEN_FIELD.value
    )
    assert result.proposed_count == 0
    assert store.list_by_agent("core_router") == []


# ---------------------------------------------------------------------------
# Multi-candidate semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_exchange_can_form_multiple_atomic_memories(tmp_path) -> None:
    extraction = FakeExtractionModel(
        candidates_json(
            remember(
                text="项目数据库使用 PostgreSQL。",
                value="PostgreSQL",
                key="project.database",
                excerpt="数据库换成 PostgreSQL",
            ),
            remember(
                category="STABLE_USER_PREFERENCE",
                text="项目统一使用 uv 管理依赖。",
                value="uv",
                key="project.package_manager",
                excerpt="统一用 uv",
            ),
        )
    )
    store = make_store(tmp_path)
    formation = make_formation(tmp_path, extraction, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.proposed_count == 2
    assert result.persisted_count == 2
    records = store.list_by_agent("core_router")
    assert len(records) == 2
    assert {record.payload["value"] for record in records} == {
        "PostgreSQL",
        "uv",
    }


@pytest.mark.asyncio
async def test_invalid_candidate_does_not_block_valid_candidates(tmp_path) -> None:
    extraction = FakeExtractionModel(
        candidates_json(
            remember(text="无根据事实。", value="x", excerpt="不存在的原话"),
            remember(
                text="项目数据库使用 PostgreSQL。",
                value="PostgreSQL",
                excerpt="数据库换成 PostgreSQL",
            ),
        )
    )
    store = make_store(tmp_path)
    formation = make_formation(tmp_path, extraction, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.SUCCEEDED
    assert result.ignored_count == 1
    assert result.persisted_count == 1


@pytest.mark.asyncio
async def test_persistence_failure_yields_partial_without_rollback(tmp_path) -> None:
    real = make_store(tmp_path)
    # 第 1 条成功、第 2 条持续失败（非 retryable conflict → 不重试）。
    failing = _DuplicateConflictOnSecondStore(real)
    extraction = FakeExtractionModel(
        candidates_json(
            remember(
                text="项目数据库使用 PostgreSQL。",
                value="PostgreSQL",
                excerpt="数据库换成 PostgreSQL",
            ),
            remember(
                category="STABLE_USER_PREFERENCE",
                text="项目统一使用 uv 管理依赖。",
                value="uv",
                excerpt="统一用 uv",
            ),
        )
    )
    formation = SemanticMemoryFormation(
        entry_agent_id="core_router",
        user_request=USER_QUERY,
        memory_store=failing,
        extraction_model=extraction,
        run_id="run-1",
    )
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.PARTIAL
    assert result.persisted_count == 1
    assert result.failed_count == 1
    # 已成功 candidate 不因后续失败回滚。
    persisted = real.list_by_agent("core_router")
    assert len(persisted) == 1
    assert persisted[0].payload == {"value": "PostgreSQL"}


class _DuplicateConflictOnSecondStore:
    def __init__(self, real: AdvancedMemoryStore) -> None:
        self._real = real
        self.count = 0

    def create(self, record):
        self.count += 1
        if self.count == 2:
            raise MemoryDomainError(MemoryErrorCode.DUPLICATE_CONFLICT)
        return self._real.create(record)


# ---------------------------------------------------------------------------
# Same-execution persistence retry idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retryable_persistence_failure_reuses_same_record(tmp_path) -> None:
    real = make_store(tmp_path)
    # 第一次 create 抛 retryable PERSISTENCE_FAILED（未提交），重试必须复用
    # 同一 record 对象且不重调 extractor、不重生成 ID/timestamp。
    store = FakeStore(real, fail_times=1)
    extraction = FakeExtractionModel(candidates_json(remember()))
    formation = SemanticMemoryFormation(
        entry_agent_id="core_router",
        user_request=USER_QUERY,
        memory_store=store,
        extraction_model=extraction,
        run_id="run-1",
    )
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.SUCCEEDED
    assert result.persisted_count == 1
    assert extraction.calls == 1
    # 同一 record 对象被提交两次（失败一次 + 重试一次），ID/timestamp 只生成一次。
    assert len(store.received) == 2
    first, second = store.received
    assert first is second
    assert first.memory_id == second.memory_id
    assert first.created_at == second.created_at
    assert len(real.list_by_agent("core_router")) == 1


@pytest.mark.asyncio
async def test_uncertain_commit_retry_returns_reused_without_second_row(
    tmp_path,
) -> None:
    # 第一次 create 实际已提交但向 caller 报 retryable failure；重试同一
    # record 时 store 的 complete-record idempotency 返回 existing row。
    real = make_store(tmp_path)
    store = _CommitThenFailStore(real, fail_times=1)
    extraction = FakeExtractionModel(candidates_json(remember()))
    formation = SemanticMemoryFormation(
        entry_agent_id="core_router",
        user_request=USER_QUERY,
        memory_store=store,
        extraction_model=extraction,
        run_id="run-1",
    )
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.SUCCEEDED
    assert result.reused_count == 1
    assert result.persisted_count == 0
    assert len(real.list_by_agent("core_router")) == 1
    memory_id = result.candidate_outcomes[0].memory_id
    assert real.get_by_memory_id(memory_id).payload == {"value": "PostgreSQL"}


class _CommitThenFailStore:
    """先真实提交再抛 retryable failure，模拟“已提交但 caller 未确认”。"""

    def __init__(self, real: AdvancedMemoryStore, *, fail_times: int) -> None:
        self._real = real
        self.fail_times = fail_times

    def create(self, record):
        resulting = self._real.create(record)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise MemoryDomainError(MemoryErrorCode.PERSISTENCE_FAILED)
        return resulting


# ---------------------------------------------------------------------------
# Write-once guard / eligibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_execution_guard_rejects_duplicate_formation(tmp_path) -> None:
    extraction = FakeExtractionModel(candidates_json(remember()))
    formation = make_formation(tmp_path, extraction, store=make_store(tmp_path))
    first = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    second = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert extraction.calls == 1
    assert second.status is first.status
    assert second == first  # memoized 同一 typed result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_receipt",
    [
        receipt(entry_agent_id="code_expert"),
        receipt(memory_scope="orchestration"),
        receipt(run_id="other-run"),
        receipt(run_id=None),
    ],
)
async def test_invalid_identity_or_scope_fails_closed_without_writes(
    tmp_path, bad_receipt
) -> None:
    extraction = FakeExtractionModel(candidates_json(remember()))
    store = make_store(tmp_path)
    formation = make_formation(tmp_path, extraction, store=store)
    result = await formation.run_formation(
        receipt=bad_receipt, final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.FAILED
    assert (
        result.safe_error_code
        == SemanticFormationErrorCode.IDENTITY_INVALID.value
    )
    assert extraction.calls == 0
    assert store.list_by_agent("core_router") == []


# ---------------------------------------------------------------------------
# Failure semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_extraction_failure_is_typed_failed_zero_writes(
    tmp_path,
) -> None:
    extraction = FakeExtractionModel(error=RuntimeError("provider down"))
    store = make_store(tmp_path)
    formation = make_formation(tmp_path, extraction, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.FAILED
    assert (
        result.safe_error_code == SemanticFormationErrorCode.MODEL_FAILED.value
    )
    assert result.persisted_count == 0
    assert store.list_by_agent("core_router") == []


@pytest.mark.asyncio
async def test_malformed_output_is_typed_failed_zero_writes(tmp_path) -> None:
    extraction = FakeExtractionModel("{broken json")
    store = make_store(tmp_path)
    formation = make_formation(tmp_path, extraction, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.FAILED
    assert (
        result.safe_error_code
        == SemanticFormationErrorCode.OUTPUT_INVALID.value
    )
    assert store.list_by_agent("core_router") == []


@pytest.mark.asyncio
async def test_zero_memory_small_talk_is_normal_success(tmp_path) -> None:
    extraction = FakeExtractionModel(
        json.dumps(
            {
                "schema_version": 1,
                "candidates": [
                    {
                        "disposition": "IGNORE",
                        "category": "STABLE_USER_PREFERENCE",
                        "canonical_text": "用户表达感谢",
                        "value": "thanks",
                        "source_excerpt": "谢谢",
                        "reason_code": "SMALL_TALK",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    formation_component = SemanticMemoryFormation(
        entry_agent_id="core_router",
        user_request="谢谢",
        memory_store=make_store(tmp_path),
        extraction_model=extraction,
        run_id="run-1",
    )
    result = await formation_component.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.SUCCEEDED
    assert result.accepted_count == 0
    assert result.ignored_count == 0
    assert result.persisted_count == 0
    assert extraction.calls == 0


# ---------------------------------------------------------------------------
# WP3 boundary：无 cross-run dedup / supersede / NO_CHANGE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_runs_same_fact_form_two_active_records(tmp_path) -> None:
    store = make_store(tmp_path)
    for run_id in ("run-a", "run-b"):
        extraction = FakeExtractionModel(candidates_json(remember()))
        formation = SemanticMemoryFormation(
            entry_agent_id="core_router",
            user_request=USER_QUERY,
            memory_store=store,
            extraction_model=extraction,
            run_id=run_id,
        )
        result = await formation.run_formation(
            receipt=receipt(run_id=run_id, exchange_id=run_id),
            final_step_id="synthesis",
            store=_FakeFinalStore(),
        )
        assert result.status is SemanticFormationStatus.SUCCEEDED
    records = store.list_by_agent("core_router")
    assert len(records) == 2
    assert all(record.status is MemoryStatus.ACTIVE for record in records)
    assert len({record.memory_id for record in records}) == 2
    # 没有 semantic NO_CHANGE / supersede：两条都是 ACTIVE，无 lifecycle 变化。


@pytest.mark.asyncio
async def test_user_correction_only_creates_new_active_record(tmp_path) -> None:
    store = make_store(tmp_path)
    # Run A：SQLite 事实
    first_extraction = FakeExtractionModel(
        candidates_json(
            remember(
                text="项目数据库使用 SQLite。",
                value="SQLite",
                key="project.database",
                excerpt="数据库换成 PostgreSQL",
            )
        )
    )
    formation_a = SemanticMemoryFormation(
        entry_agent_id="core_router",
        user_request=USER_QUERY,
        memory_store=store,
        extraction_model=first_extraction,
        run_id="run-a",
    )
    await formation_a.run_formation(
        receipt=receipt(run_id="run-a", exchange_id="run-a"),
        final_step_id="synthesis",
        store=_FakeFinalStore(),
    )
    sqlite_record = store.list_by_agent("core_router")[0]
    # Run B：用户更正为 PostgreSQL —— 只创建新 ACTIVE，不改旧 record。
    second_extraction = FakeExtractionModel(
        candidates_json(
            remember(
                text="项目数据库使用 PostgreSQL。",
                value="PostgreSQL",
                key="project.database",
                excerpt="数据库换成 PostgreSQL",
            )
        )
    )
    formation_b = SemanticMemoryFormation(
        entry_agent_id="core_router",
        user_request=USER_QUERY,
        memory_store=store,
        extraction_model=second_extraction,
        run_id="run-b",
    )
    await formation_b.run_formation(
        receipt=receipt(run_id="run-b", exchange_id="run-b"),
        final_step_id="synthesis",
        store=_FakeFinalStore(),
    )
    records = store.list_by_agent("core_router", active_only=False)
    assert len(records) == 2
    assert all(record.status is MemoryStatus.ACTIVE for record in records)
    # 旧 record 完全不变（identity/正文/status/timestamps）。
    unchanged = store.get_by_memory_id(sqlite_record.memory_id)
    assert unchanged.status is MemoryStatus.ACTIVE
    assert unchanged.payload == {"value": "SQLite"}
    assert unchanged.updated_at == sqlite_record.updated_at
    assert unchanged.superseded_by_memory_id is None


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


async def _make_emitter():
    from core.runtime import (
        InMemoryRunEventJournal,
        RunEventEmitter,
        RuntimeEventChannel,
        create_run_context,
    )
    from tests._runtime_assembly_fixtures import FakeDispatcher

    context, source = create_run_context(entry_agent_id="test")
    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(
        16,
        run_id=context.run_id,
        cancellation_token=source.token,
        journal=journal,
        observability_dispatcher=FakeDispatcher(),
    )
    emitter = RunEventEmitter(
        run_id=context.run_id, trace_id=context.trace_id, channel=channel
    )
    return emitter, journal, channel


def _formation_events(journal, run_id: str) -> list:
    return [
        record
        for record in journal.read_after(run_id, 0, 100)
        if record.event_type.value == "MEMORY_FORMATION_COMPLETED"
    ]


@pytest.mark.asyncio
async def test_success_observation_has_counts_ids_status_latency(tmp_path) -> None:
    emitter, journal, channel = await _make_emitter()
    try:
        extraction = FakeExtractionModel(candidates_json(remember()))
        store = make_store(tmp_path)
        spans = InMemorySpanRecorder()
        formation = make_formation(
            tmp_path,
            extraction,
            store=store,
            event_emitter=emitter,
            span_recorder=spans,
        )
        result = await formation.run_formation(
            receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
        )
        events = _formation_events(journal, emitter.run_id)
        assert len(events) == 1
        payload = events[0].safe_payload
        assert payload["status"] == "SUCCEEDED"
        assert payload["formation_method"] == "HYBRID"
        assert payload["agent_id"] == "core_router"
        assert payload["memory_scope"] == "direct"
        assert payload["exchange_id"] == "run-1"
        assert payload["proposed_count"] == 1
        assert payload["persisted_count"] == 1
        assert payload["failed_count"] == 0
        assert payload["schema_version"] == 1
        assert payload["formation_total_duration_ms"] >= 0
        assert payload["model_extraction_duration_ms"] >= 0
        assert payload["persistence_duration_ms"] >= 0
        assert result.candidate_outcomes[0].memory_id in payload["candidate_outcomes"]

        metrics = InMemoryMetricsRecorder()
        RuntimeMetricsProjector(metrics).project(events[0])
        snapshot = metrics.snapshot()
        assert snapshot.counter(
            "runtime_memory_formation_total",
            {"status": "SUCCEEDED", "error_code": "OK"},
        ) == 1
        assert snapshot.histogram(
            "runtime_memory_formation_duration_seconds",
            {"status": "SUCCEEDED"},
        ) == (payload["formation_total_duration_ms"] / 1000.0,)

        formation_spans = [
            item for item in spans.snapshot() if item.operation == "memory.formation"
        ]
        assert len(formation_spans) == 1
        assert formation_spans[0].attributes["formation_status"] == "SUCCEEDED"
        assert USER_QUERY not in repr(formation_spans[0])
        assert FINAL_ANSWER not in repr(formation_spans[0])
    finally:
        await channel.abort()


@pytest.mark.asyncio
async def test_failure_observation_is_correct_and_content_minimized(tmp_path) -> None:
    emitter, journal, channel = await _make_emitter()
    try:
        extraction = FakeExtractionModel(error=RuntimeError("provider secret path C:\\x"))
        formation = make_formation(
            tmp_path, extraction, store=make_store(tmp_path), event_emitter=emitter
        )
        result = await formation.run_formation(
            receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
        )
        assert result.status is SemanticFormationStatus.FAILED
        events = _formation_events(journal, emitter.run_id)
        assert len(events) == 1
        payload = events[0].safe_payload
        assert payload["status"] == "FAILED"
        assert payload["safe_error_code"] == "FORMATION_MODEL_FAILED"
        assert payload["persisted_count"] == 0
        rendered = str(payload) + repr(result)
        for forbidden in (
            USER_QUERY,
            FINAL_ANSWER,
            "provider secret path",
            "PostgreSQL",
        ):
            assert forbidden not in rendered
    finally:
        await channel.abort()


@pytest.mark.asyncio
async def test_observation_excludes_memory_payload_and_source_quote(tmp_path) -> None:
    emitter, journal, channel = await _make_emitter()
    try:
        extraction = FakeExtractionModel(candidates_json(remember()))
        formation = make_formation(
            tmp_path, extraction, store=make_store(tmp_path), event_emitter=emitter
        )
        result = await formation.run_formation(
            receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
        )
        events = _formation_events(journal, emitter.run_id)
        assert len(events) == 1
        rendered = str(events[0].safe_payload) + repr(result)
        for forbidden in (
            USER_QUERY,
            FINAL_ANSWER,
            "项目数据库使用 PostgreSQL",  # Memory 正文
            "数据库换成 PostgreSQL",  # source quote
        ):
            assert forbidden not in rendered
    finally:
        await channel.abort()


@pytest.mark.asyncio
async def test_observation_failure_does_not_change_business_result(tmp_path) -> None:
    class _BrokenEmitter:
        async def emit(self, *args, **kwargs):
            raise RuntimeError("observability down")

    extraction = FakeExtractionModel(candidates_json(remember()))
    store = make_store(tmp_path)
    formation = SemanticMemoryFormation(
        entry_agent_id="core_router",
        user_request=USER_QUERY,
        memory_store=store,
        extraction_model=extraction,
        run_id="run-1",
        event_emitter=_BrokenEmitter(),
    )
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    # Observation failure 是 best-effort：Memory business fact 已提交。
    assert result.status is SemanticFormationStatus.SUCCEEDED
    assert result.persisted_count == 1
    assert len(store.list_by_agent("core_router")) == 1


def test_payload_dataclass_validates_status_and_method() -> None:
    with pytest.raises(ValueError):
        MemoryFormationCompletedPayload(
            exchange_id="e",
            agent_id="a",
            memory_scope="direct",
            formation_method="HYBRID",
            status="WEIRD",
            schema_version=1,
            proposed_count=0,
            accepted_count=0,
            ignored_count=0,
            persisted_count=0,
            reused_count=0,
            failed_count=0,
            formation_total_duration_ms=0,
            model_extraction_duration_ms=0,
            persistence_duration_ms=0,
        )
    with pytest.raises(ValueError):
        MemoryFormationCompletedPayload(
            exchange_id="e",
            agent_id="a",
            memory_scope="direct",
            formation_method="MANUAL",
            status="SUCCEEDED",
            schema_version=1,
            proposed_count=0,
            accepted_count=0,
            ignored_count=0,
            persisted_count=0,
            reused_count=0,
            failed_count=0,
            formation_total_duration_ms=0,
            model_extraction_duration_ms=0,
            persistence_duration_ms=0,
        )


# ---------------------------------------------------------------------------
# Cancellation / timeout isolation（business 层面）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_returns_typed_timed_out_zero_writes(
    tmp_path, monkeypatch
) -> None:
    import core.runtime.semantic_memory_formation as formation_module

    monkeypatch.setattr(formation_module, "FORMATION_TIMEOUT_SECONDS", 0.1)
    store = make_store(tmp_path)
    formation = SemanticMemoryFormation(
        entry_agent_id="core_router",
        user_request=USER_QUERY,
        memory_store=store,
        extraction_model=_SlowExtractionModel(0.5),
        run_id="run-1",
    )
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.TIMED_OUT
    assert result.safe_error_code == SemanticFormationErrorCode.TIMED_OUT.value
    assert result.persisted_count == 0
    assert store.list_by_agent("core_router") == []


@pytest.mark.asyncio
async def test_extraction_cancellation_waits_safe_boundary_and_writes_nothing(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    formation = SemanticMemoryFormation(
        entry_agent_id="core_router",
        user_request=USER_QUERY,
        memory_store=store,
        extraction_model=_SlowExtractionModel(0.2),
        run_id="run-1",
    )
    task = asyncio.create_task(
        formation.run_formation(
            receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    result = await task
    assert result.status is SemanticFormationStatus.CANCELLED
    assert result.safe_error_code == SemanticFormationErrorCode.CANCELLED.value
    assert result.persisted_count == 0
    assert store.list_by_agent("core_router") == []


@pytest.mark.asyncio
async def test_timeout_during_persistence_stops_at_safe_boundary(
    tmp_path, monkeypatch
) -> None:
    import core.runtime.semantic_memory_formation as formation_module

    monkeypatch.setattr(formation_module, "FORMATION_TIMEOUT_SECONDS", 0.1)
    real = make_store(tmp_path)
    slow_store = _SlowCreateStore(real, delay=0.3)
    extraction = FakeExtractionModel(candidates_json(remember()))
    formation = SemanticMemoryFormation(
        entry_agent_id="core_router",
        user_request=USER_QUERY,
        memory_store=slow_store,
        extraction_model=extraction,
        run_id="run-1",
    )
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.PARTIAL
    assert result.persisted_count == 1
    assert result.candidate_outcomes[0].outcome is (
        FormationCandidateOutcomeCode.PERSISTED
    )
    assert result.candidate_outcomes[0].safe_reason_code == "OK"
    # 返回前已等待单条 transaction 到 commit/rollback 安全边界。
    assert slow_store.committed_count == 1
    assert len(real.list_by_agent("core_router")) == 1


class _SlowExtractionModel(FormationExtractionModel):
    def __init__(self, delay: float) -> None:
        self.delay = delay

    def extract(self, user_query: str, final_answer: str) -> str:
        import time as _time

        _time.sleep(self.delay)
        return candidates_json(remember())


class _SlowCreateStore:
    def __init__(self, real: AdvancedMemoryStore, *, delay: float) -> None:
        self._real = real
        self.delay = delay
        self.committed_count = 0

    def create(self, record):
        import time as _time

        _time.sleep(self.delay)
        self.committed_count += 1
        return self._real.create(record)
