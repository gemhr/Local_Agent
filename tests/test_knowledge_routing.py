import pytest
from types import SimpleNamespace

from core.agent_router import (
    AgentRouter,
    KnowledgeBaseUnavailableError,
    KnowledgeSourceNotFoundError,
)


class FakeMemory:
    def __init__(self) -> None:
        self.added_messages = []

    def count_messages(self, *args, **kwargs) -> int:
        return 0

    def get_summary_record(self, agent_id: str) -> dict:
        return {"summary": "", "last_message_id": 0}

    def get_chat_history(self, *args, **kwargs) -> list:
        return []

    def add_message(self, *args, **kwargs) -> None:
        self.added_messages.append((args, kwargs))


class NoCallLLM:
    def generate(self, *args, **kwargs):
        raise AssertionError("LLM must not run when the knowledge base is unavailable")


class RewriteLLM:
    def generate(self, *args, **kwargs):
        yield "CDT"


class EmptyDB:
    def search_with_scores(self, *args, **kwargs) -> list:
        return []

    def search(self, *args, **kwargs) -> list:
        return []


class HybridDB:
    def __init__(self) -> None:
        self.queries = []
        self.keyword_terms = []
        self.target = SimpleNamespace(
            page_content="CDT 是本项目中的字段映射定义，包含源字段和目标字段。",
            metadata={
                "chunk_id": "cdt-1",
                "source": "cdt_field_mapping.md",
                "file_name": "cdt_field_mapping.md",
            },
        )
        self.noise = SimpleNamespace(
            page_content="This unrelated RFC describes a generic network protocol.",
            metadata={"chunk_id": "noise-1", "source": "rfc.txt"},
        )

    def search_with_scores(self, query: str, **kwargs) -> list:
        self.queries.append(query)
        if query == "CDT":
            return [(self.noise, 0.50)]
        return [(self.target, 0.52)]

    def keyword_search(self, terms: list[str], **kwargs) -> list:
        self.keyword_terms = terms
        return [self.target]


class LowScoreDB:
    def search_with_scores(self, *args, **kwargs) -> list:
        return [
            (
                SimpleNamespace(
                    page_content="unrelated material",
                    metadata={"chunk_id": "low-1", "source": "other.txt"},
                ),
                0.10,
            )
        ]


def test_knowledge_expert_fails_closed_without_db_manager() -> None:
    router = AgentRouter(
        llm_engine=NoCallLLM(),
        memory_manager=FakeMemory(),
        db_manager=None,
        knowledge_base_error="embedding import failed",
    )

    with pytest.raises(KnowledgeBaseUnavailableError, match="本地知识库当前不可用"):
        router._build_messages("讲讲 CDT", "knowledge_expert")


def test_knowledge_expert_fails_closed_without_relevant_sources() -> None:
    router = AgentRouter(
        llm_engine=RewriteLLM(),
        memory_manager=FakeMemory(),
        db_manager=EmptyDB(),
    )

    with pytest.raises(KnowledgeSourceNotFoundError, match="已停止回答"):
        router._build_messages("讲讲 CDT", "knowledge_expert")

    prompt = router._build_system_prompt("knowledge_expert")
    assert "不得使用通用知识补写事实" in prompt


@pytest.mark.parametrize(
    ("query", "expected_task"),
    [
        ("调用知识专家，讲讲CDT", "讲讲CDT"),
        ("请使用 knowledge_expert: explain CDT", "explain CDT"),
        ("根据本地知识库查询 CDT 字段", "查询 CDT 字段"),
    ],
)
def test_explicit_knowledge_requests_route_without_llm(query: str, expected_task: str) -> None:
    router = AgentRouter(llm_engine=NoCallLLM(), memory_manager=FakeMemory())

    result = router._plan_orchestration(query)

    assert result["delegates"] == [
        {"agent_id": "knowledge_expert", "task": expected_task}
    ]
    assert result["planning_messages"] == []


def test_rag_uses_original_query_and_keyword_fallback_then_filters_noise() -> None:
    database = HybridDB()
    router = AgentRouter(
        llm_engine=RewriteLLM(),
        memory_manager=FakeMemory(),
        db_manager=database,
        rag_min_score=0.55,
    )

    context = router._build_rag_context("讲讲 CDT")

    assert database.queries == ["CDT", "讲讲 CDT"]
    assert "cdt" in database.keyword_terms
    assert "cdt_field_mapping.md" in context
    assert "源字段和目标字段" in context
    assert "unrelated RFC" not in context


def test_rag_minimum_score_rejects_untrusted_candidates() -> None:
    router = AgentRouter(
        llm_engine=RewriteLLM(),
        memory_manager=FakeMemory(),
        db_manager=LowScoreDB(),
        rag_min_score=0.90,
    )

    with pytest.raises(KnowledgeSourceNotFoundError):
        router._build_messages("讲讲 CDT", "knowledge_expert")


def test_single_knowledge_delegate_bypasses_core_synthesis(monkeypatch) -> None:
    memory = FakeMemory()
    router = AgentRouter(llm_engine=NoCallLLM(), memory_manager=memory)
    grounded_answer = "CDT 的定义来自 cdt_field_mapping.md。"
    monkeypatch.setattr(
        router,
        "_plan_orchestration",
        lambda user_query, run_context=None: {
            "delegates": [{"agent_id": "knowledge_expert", "task": "讲讲 CDT"}]
        },
    )
    monkeypatch.setattr(router, "_run_agent_once", lambda **kwargs: grounded_answer)

    chunks = list(router._stream_core_with_orchestration("调用知识专家，讲讲 CDT"))

    assert grounded_answer in chunks
    assert memory.added_messages[-1][0][2] == grounded_answer


def test_multi_agent_synthesis_prompt_forbids_ungrounded_expansion() -> None:
    router = AgentRouter(llm_engine=NoCallLLM(), memory_manager=FakeMemory())

    prompt = router._build_synthesis_query(
        "解释 CDT",
        [
            {
                "agent_name": "Knowledge Expert",
                "task": "解释 CDT",
                "result": "未找到相关来源。",
            }
        ],
    )

    assert "do not invent, expand" in prompt
    assert "no relevant source was found" in prompt
