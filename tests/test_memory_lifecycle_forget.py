"""WP3-B explicit forget branch + parser deterministic tests。

证明：deterministic cue gate、mutual exclusivity（forget 与 remember 互斥）、
strict parser fail-closed、exact-key membership、assistant-only 不可触发、
invalid/missing/ambiguous key 零 mutation、all-version redaction、forget
幂等、NOT_FOUND 与 observation privacy。

全部使用 fake forget/extraction model + 真实 store，属于 DETERMINISTIC
IMPLEMENTATION TEST，不是真实模型实验。
"""

from __future__ import annotations

import json
from typing import List, Optional, Sequence

import pytest

from core.advanced_memory import (
    AdvancedMemoryStore,
    MemoryStatus,
)
from core.memory_manager import MemoryManager
from core.runtime import (
    CommittedExchangeReceipt,
    ExplicitForgetIntentParser,
    ForgetProposalError,
    ForgetProposalErrorCode,
    ForgetProposalModel,
    FormationExtractionModel,
    InMemoryRunEventJournal,
    RunEventEmitter,
    RuntimeEventChannel,
    RuntimeEventType,
    SemanticFormationStatus,
    SemanticMemoryFormation,
    create_run_context,
    has_explicit_forget_cue,
)


def make_store(tmp_path) -> AdvancedMemoryStore:
    db_path = str(tmp_path / "forget.db")
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


class FakeExtractionModel(FormationExtractionModel):
    def __init__(self, output: str = '{"schema_version": 1, "candidates": []}') -> None:
        self.output = output
        self.calls = 0

    def extract(self, user_query: str, final_answer: str) -> str:
        self.calls += 1
        return self.output


class FakeForgetModel(ForgetProposalModel):
    def __init__(self, output: str | None = None, error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.calls = 0
        self.received_queries: List[str] = []
        self.received_allowlists: List[List[str]] = []

    def propose_key(self, user_query: str, allowlist: Sequence[str]) -> str:
        self.calls += 1
        self.received_queries.append(user_query)
        self.received_allowlists.append(list(allowlist))
        if self.error is not None:
            raise self.error
        assert self.output is not None
        return self.output


class _FakeFinalStore:
    def read_final_content(self, step_id: str) -> str:
        return "final"


def forget_json(key) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "logical_key": key,
            "source_excerpt": "忘记 project.database 这条记忆",
            "safe_reason": "EXPLICIT_FORGET",
        },
        ensure_ascii=False,
    )


FORGET_QUERY = "忘记 project.database 这条记忆，不要再记住。"
REMEMBER_QUERY = "以后这个项目统一用 uv 管包，数据库换成 PostgreSQL。"


def make_forget_formation(
    tmp_path,
    forget: ForgetProposalModel,
    *,
    user_request: str = FORGET_QUERY,
    store: Optional[AdvancedMemoryStore] = None,
    event_emitter=None,
) -> SemanticMemoryFormation:
    return SemanticMemoryFormation(
        entry_agent_id="core_router",
        user_request=user_request,
        memory_store=store or make_store(tmp_path),
        extraction_model=FakeExtractionModel(),
        forget_model=forget,
        run_id="run-1",
        event_emitter=event_emitter,
    )


async def _make_emitter():
    context, source = create_run_context(entry_agent_id="test")
    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(
        8,
        run_id=context.run_id,
        cancellation_token=source.token,
        journal=journal,
    )
    emitter = RunEventEmitter(
        run_id=context.run_id, trace_id=context.trace_id, channel=channel
    )
    return emitter, channel, journal, context.run_id


# ---------------------------------------------------------------------------
# Deterministic cue gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "忘记 project.database 这条记忆。",
        "请不要再记住 project.database。",
        "删除这项记忆：project.database。",
        "把 project.database 的记忆忘掉。",
        "忘掉 project.database。",
    ],
)
def test_forget_cue_matches(query: str) -> None:
    assert has_explicit_forget_cue(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "以后这个项目统一用 uv 管包。",
        "谢谢",
        "数据库换成 PostgreSQL。",
        "今天先临时用 pip 装一下。",
        "你是不是忘记项目数据库是什么？",
        "忘记项目数据库是什么了吗？",
        "你还记得 project.database 吗？",
        "",
    ],
)
def test_no_cue_means_no_forget_branch(query: str) -> None:
    assert has_explicit_forget_cue(query) is False


# ---------------------------------------------------------------------------
# Strict parser
# ---------------------------------------------------------------------------


def test_parser_accepts_exact_valid_key() -> None:
    proposal = ExplicitForgetIntentParser.parse(forget_json("project.database"))
    assert proposal.logical_key == "project.database"


def test_parser_rejects_unknown_field() -> None:
    raw = json.dumps(
        {"schema_version": 1, "logical_key": "project.database", "memory_id": "mem-x"}
    )
    with pytest.raises(ForgetProposalError) as exc:
        ExplicitForgetIntentParser.parse(raw)
    assert exc.value.error_code == ForgetProposalErrorCode.OUTPUT_INVALID


def test_parser_rejects_malformed_json() -> None:
    with pytest.raises(ForgetProposalError) as exc:
        ExplicitForgetIntentParser.parse("{not-json")
    assert exc.value.error_code == ForgetProposalErrorCode.OUTPUT_INVALID


def test_parser_rejects_wrong_schema_version() -> None:
    raw = json.dumps({"schema_version": 2, "logical_key": "project.database"})
    with pytest.raises(ForgetProposalError) as exc:
        ExplicitForgetIntentParser.parse(raw)
    assert exc.value.error_code == ForgetProposalErrorCode.OUTPUT_INVALID


def test_validate_exact_membership() -> None:
    proposal = ExplicitForgetIntentParser.parse(forget_json("project.database"))
    assert (
        ExplicitForgetIntentParser.validate(
            proposal,
            ["project.database"],
            user_query=FORGET_QUERY,
        )
        == "project.database"
    )


def test_validate_requires_source_excerpt_grounded_in_original_query() -> None:
    payload = json.loads(forget_json("project.database"))
    payload["source_excerpt"] = "assistant 建议删除"
    proposal = ExplicitForgetIntentParser.parse(json.dumps(payload, ensure_ascii=False))
    assert (
        ExplicitForgetIntentParser.validate(
            proposal,
            ["project.database"],
            user_query=FORGET_QUERY,
        )
        is None
    )


@pytest.mark.parametrize(
    ("key", "allowlist"),
    [
        ("project.database", []),  # 无精确成员
        ("project.invented", ["project.database"]),  # Model 发明 key
        ("Project.Database", ["project.database"]),  # 大小写不符
        ("project/database", ["project/database"]),  # 语法不合法
    ],
)
def test_validate_fails_closed_on_non_exact_member(key, allowlist) -> None:
    proposal = ExplicitForgetIntentParser.parse(forget_json(key))
    assert (
        ExplicitForgetIntentParser.validate(
            proposal, allowlist, user_query=FORGET_QUERY
        )
        is None
    )


# ---------------------------------------------------------------------------
# Formation forget branch（mutual exclusivity + all-version redaction）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_user_forget_with_valid_key_forgets_all_versions(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    # 预置 keyed 历史：SQLite SUPERSEDED + PostgreSQL ACTIVE（同一 partition）。
    from tests.test_memory_lifecycle import make_record, _insert_row_raw

    _insert_row_raw(store, memory_id="mem-sqlite", value="SQLite", key="project.database")
    store.resolve_semantic(make_record("mem-pg", "PostgreSQL", "project.database"))
    forget = FakeForgetModel(output=forget_json("project.database"))
    formation = make_forget_formation(tmp_path, forget, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.status is SemanticFormationStatus.SUCCEEDED
    assert result.lifecycle_operation == "FORGET"
    assert result.lifecycle_outcome == "OK"
    assert result.lifecycle_affected_count == 2
    assert result.persisted_count == 0
    assert result.accepted_count == 0
    # forget 与 remember 互斥：本 exchange 不再形成任何新 Semantic Memory。
    rows = store.list_by_agent("core_router", active_only=False)
    assert {r.memory_id for r in rows} == {"mem-sqlite", "mem-pg"}
    assert all(r.status is MemoryStatus.FORGOTTEN for r in rows)
    assert all(r.canonical_text == "[FORGOTTEN]" for r in rows)
    assert all(r.payload == {} for r in rows)
    assert all(r.logical_key == "project.database" for r in rows)


@pytest.mark.asyncio
async def test_no_cue_does_not_invoke_destructive_parser(tmp_path) -> None:
    store = make_store(tmp_path)
    forget = FakeForgetModel(output=forget_json("project.database"))
    formation = make_forget_formation(
        tmp_path, forget, user_request=REMEMBER_QUERY, store=store
    )
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    # 没有 deterministic cue → 绝不调用 destructive forget parser。
    assert forget.calls == 0
    assert result.lifecycle_operation is None
    assert result.status is SemanticFormationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_forget_without_forget_model_fails_closed_zero_mutation(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    from tests.test_memory_lifecycle import make_record

    store.resolve_semantic(make_record("mem-1", "PostgreSQL", "project.database"))
    formation = SemanticMemoryFormation(
        entry_agent_id="core_router",
        user_request=FORGET_QUERY,
        memory_store=store,
        extraction_model=FakeExtractionModel(),
        forget_model=None,
        run_id="run-1",
    )
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.lifecycle_outcome == "FAILED_CLOSED"
    row = store.get_by_memory_id("mem-1")
    assert row.status is MemoryStatus.ACTIVE
    assert row.payload == {"value": "PostgreSQL"}


@pytest.mark.asyncio
async def test_forget_invented_key_fails_closed_zero_mutation(tmp_path) -> None:
    store = make_store(tmp_path)
    from tests.test_memory_lifecycle import make_record

    store.resolve_semantic(make_record("mem-1", "PostgreSQL", "project.database"))
    forget = FakeForgetModel(output=forget_json("project.invented"))
    formation = make_forget_formation(tmp_path, forget, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.lifecycle_outcome == "FAILED_CLOSED"
    assert result.lifecycle_affected_count == 0
    row = store.get_by_memory_id("mem-1")
    assert row.status is MemoryStatus.ACTIVE
    assert row.payload == {"value": "PostgreSQL"}


@pytest.mark.asyncio
async def test_forget_only_targets_registry_backed_existing_keys(tmp_path) -> None:
    """WP3-R1：new canonical forget path 只允许 registry-backed existing keys。

    历史 free-form key（project_database）不进入 allowlist → chat forget 无法
    命中 → NOT_FOUND 零 mutation；registry key 仍可正常 all-version FORGET。
    """
    store = make_store(tmp_path)
    from tests.test_memory_lifecycle import _insert_row_raw

    _insert_row_raw(
        store, memory_id="mem-legacy", value="SQLite", key="project_database"
    )
    _insert_row_raw(
        store, memory_id="mem-reg", value="PostgreSQL", key="project.database"
    )
    forget = FakeForgetModel(output=forget_json("project.database"))
    formation = make_forget_formation(tmp_path, forget, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    # registry key 被 forget，全版本 FORGOTTEN。
    assert result.lifecycle_operation == "FORGET"
    assert result.lifecycle_outcome == "OK"
    reg_row = store.get_by_memory_id("mem-reg")
    assert reg_row.status is MemoryStatus.FORGOTTEN
    # 历史 free-form key 不在 registry allowlist，不被 chat forget 误处理。
    legacy_row = store.get_by_memory_id("mem-legacy")
    assert legacy_row.status is MemoryStatus.ACTIVE
    assert legacy_row.payload == {"value": "SQLite"}


@pytest.mark.asyncio
async def test_forget_never_existed_is_not_found_zero_mutation(tmp_path) -> None:
    store = make_store(tmp_path)
    forget = FakeForgetModel(output=forget_json("project.database"))
    formation = make_forget_formation(tmp_path, forget, store=store)
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert result.lifecycle_operation == "FORGET"
    assert result.lifecycle_outcome == "NOT_FOUND"
    assert result.lifecycle_affected_count == 0
    assert store.list_by_agent("core_router", active_only=False) == []


@pytest.mark.asyncio
async def test_forget_repeat_is_already_forgotten(tmp_path) -> None:
    store = make_store(tmp_path)
    from tests.test_memory_lifecycle import make_record

    store.resolve_semantic(make_record("mem-1", "PostgreSQL", "project.database"))
    first_model = FakeForgetModel(output=forget_json("project.database"))
    first = await make_forget_formation(
        tmp_path, first_model, store=store
    ).run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert first.lifecycle_outcome == "OK"
    second_model = FakeForgetModel(output=forget_json("project.database"))
    second = await make_forget_formation(
        tmp_path, second_model, store=store
    ).run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert second.lifecycle_outcome == "ALREADY_FORGOTTEN"


@pytest.mark.asyncio
async def test_assistant_only_cannot_forget(tmp_path) -> None:
    """assistant-only source：cue 必须来自 original user query；formation 只
    消费 original user query，assistant 内容无法触发 forget。"""
    store = make_store(tmp_path)
    from tests.test_memory_lifecycle import make_record

    store.resolve_semantic(make_record("mem-1", "PostgreSQL", "project.database"))
    # 用户 query 没有 forget cue；assistant 生成内容不进入 forget 判定。
    forget = FakeForgetModel(output=forget_json("project.database"))
    formation = make_forget_formation(
        tmp_path,
        forget,
        user_request="数据库换成 PostgreSQL 了。",
        store=store,
    )
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    assert forget.calls == 0
    assert result.lifecycle_operation is None
    assert store.get_by_memory_id("mem-1").status is MemoryStatus.ACTIVE


# ---------------------------------------------------------------------------
# Observation privacy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forget_event_never_leaks_key_text_or_query(tmp_path) -> None:
    emitter, channel, journal, run_id = await _make_emitter()
    try:
        store = make_store(tmp_path)
        from tests.test_memory_lifecycle import make_record

        store.resolve_semantic(
            make_record("mem-1", "PostgreSQL", "project.database")
        )
        forget = FakeForgetModel(output=forget_json("project.database"))
        formation = make_forget_formation(
            tmp_path, forget, store=store, event_emitter=emitter
        )
        await formation.run_formation(
            receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
        )
        records = journal.read_after(run_id, 0, 1000)
        lifecycle_events = [
            item
            for item in records
            if item.event_type.value == "MEMORY_LIFECYCLE_RESOLVED"
        ]
        assert len(lifecycle_events) == 1
        rendered = json.dumps(lifecycle_events[0].safe_payload, ensure_ascii=False)
        assert "project.database" not in rendered
        assert "PostgreSQL" not in rendered
        assert "忘记" not in rendered
        assert "FORGOTTEN" in rendered  # 只有固定 tombstone 词
        assert lifecycle_events[0].safe_payload["operation"] == "FORGET"
        assert lifecycle_events[0].safe_payload["outcome"] == "OK"
        assert lifecycle_events[0].safe_payload["affected_count"] == 1
        assert lifecycle_events[0].safe_payload["affected_transitions"] != "NONE"
    finally:
        await channel.abort()


@pytest.mark.asyncio
async def test_forget_not_found_emits_safe_lifecycle_outcome(tmp_path) -> None:
    emitter, channel, journal, run_id = await _make_emitter()
    try:
        forget = FakeForgetModel(output=forget_json("project.database"))
        formation = make_forget_formation(
            tmp_path,
            forget,
            store=make_store(tmp_path),
            event_emitter=emitter,
        )
        result = await formation.run_formation(
            receipt=receipt(),
            final_step_id="synthesis",
            store=_FakeFinalStore(),
        )
        assert result.lifecycle_outcome == "NOT_FOUND"
        lifecycle_events = [
            row
            for row in journal.read_after(run_id, 0, 100)
            if row.event_type is RuntimeEventType.MEMORY_LIFECYCLE_RESOLVED
        ]
        assert len(lifecycle_events) == 1
        payload = lifecycle_events[0].safe_payload
        assert payload["operation"] == "FORGET"
        assert payload["outcome"] == "NOT_FOUND"
        assert payload["affected_count"] == 0
        assert "project.database" not in repr(payload)
        assert FORGET_QUERY not in repr(payload)
    finally:
        await channel.abort()


@pytest.mark.asyncio
async def test_lifecycle_event_is_journal_first_and_best_effort(tmp_path) -> None:
    """event emit failure 不改变 committed lifecycle state。"""
    store = make_store(tmp_path)
    from tests.test_memory_lifecycle import make_record

    store.resolve_semantic(make_record("mem-1", "PostgreSQL", "project.database"))

    class BrokenEmitter:
        async def emit(self, *args, **kwargs):
            raise RuntimeError("journal down")

    forget = FakeForgetModel(output=forget_json("project.database"))
    formation = make_forget_formation(
        tmp_path,
        forget,
        store=store,
        event_emitter=BrokenEmitter(),
    )
    result = await formation.run_formation(
        receipt=receipt(), final_step_id="synthesis", store=_FakeFinalStore()
    )
    # event 失败 best-effort：不改变已提交的 FORGET。
    assert result.lifecycle_outcome == "OK"
    assert store.get_by_memory_id("mem-1").status is MemoryStatus.FORGOTTEN
