"""仅保留仍有效的纯合同/本地测试与共享 fixture；废弃 agent-only 节点见 .ai/archive/tests/scoped_memory_lifecycle/。"""

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














# ---------------------------------------------------------------------------
# Observation privacy
# ---------------------------------------------------------------------------
