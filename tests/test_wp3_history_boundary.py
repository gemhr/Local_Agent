"""WP3 acceptance supplement: specialist/synthesis must not read old Memory.

Tests use a real AgentRouter + real MemoryManager (only the external model is
faked) so the Memory read/write contract is exercised through the real chain.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.agent_router import AgentRouter
from core.memory_manager import MemoryManager
from core.runtime import (
    BudgetLedger,
    CitationBinding,
    ContextTrustLevel,
    CoordinatedRuntimeFactory,
    HistoryPolicy,
    ModelCostProfile,
    ModelProfile,
    ModelProfileId,
    RetrievalExecutionResult,
    RetrievalExecutionStatus,
    RetrievalInvocation,
    RetrievalProvenance,
    RetrievalTransformation,
    RetrievedChunk,
    RunBudget,
    RunStatus,
    SourceMetadata,
    TaskCapabilityRequirements,
    content_digest,
    create_run_context,
)
from core.runtime.retrieval_contract import RetrievalBudgetUsage
from tests._wp3_fixtures import delegated_json, direct_json
from tests.test_step_result_security import (
    make_security_services,
    render_all_channels,
)


OLD_MEMORY_SECRET = "SECRET_OLD_AGENT_MEMORY_MUST_NOT_BE_READ"


def render_messages(messages) -> str:
    return "\n".join(str(message) for message in messages)


class FakeModel:
    def __init__(self, planning_json: str | None = None) -> None:
        self.planning_json = planning_json or direct_json()
        self.calls = 0
        self.all_messages: list[list[dict[str, str]]] = []

    def generate(self, messages, **kwargs):
        self.calls += 1
        self.all_messages.append(list(messages))
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "LocalAgent Planner" in system:
            yield self.planning_json
        elif user.startswith("You are the synthesis agent"):
            yield "FINAL-SYNTHESIS"
        elif "Code Expert" in system:
            yield "result-code_expert"
        elif "Data Analyst" in system:
            yield "result-data_analyst"
        elif "Knowledge Expert" in system:
            yield "result-knowledge_expert"
        elif "Core Router" in system:
            yield "result-core_router"
        else:
            yield "result-generic"


class _DummyDB:
    collection_name = "local_knowledge_base"


def make_real_router(
    memory,
    *,
    model=None,
    retrieval_service=None,
    db_manager=None,
) -> AgentRouter:
    profile = ModelProfile(
        ModelProfileId.LOCAL_FAST,
        context_window=4096,
        max_output_tokens=640,
        supports_tools=True,
        supports_structured_output=True,
        supports_code_reasoning=True,
        supports_long_reasoning=True,
        quality_tier=1,
        latency_tier=1,
        cost_profile=ModelCostProfile(
            ModelProfileId.LOCAL_FAST,
            False,
            fixed_call_cost_units=1,
            estimated_latency_ms=1,
        ),
    )
    return AgentRouter(
        llm_engine=model or FakeModel(),
        memory_manager=memory,
        db_manager=db_manager,
        orchestration_enabled=False,
        model_profiles=(profile,),
        retrieval_execution_service=retrieval_service,
    )


def make_run_context(agent_id: str):
    context, _source = create_run_context(entry_agent_id=agent_id)
    ledger = BudgetLedger(RunBudget(), deadline_remaining=context.remaining_seconds)
    context.attach_budget_ledger(ledger)
    return context


def specialist_capabilities(agent_id: str) -> TaskCapabilityRequirements:
    if agent_id == "code_expert":
        return TaskCapabilityRequirements(
            requires_planning=True,
            requires_multi_agent=True,
            requires_code_reasoning=True,
        )
    if agent_id == "knowledge_expert":
        return TaskCapabilityRequirements(
            requires_planning=True,
            requires_multi_agent=True,
            requires_rag=True,
        )
    return TaskCapabilityRequirements(
        requires_planning=True,
        requires_multi_agent=True,
    )


def make_successful_retrieval_result(text: str) -> RetrievalExecutionResult:
    now = datetime.now(UTC)
    source = SourceMetadata(
        source_id="source-stable",
        source_type="md",
        collection="kb",
        canonical_uri="docs/source.md",
        display_name="source.md",
        document_version="version-1",
        page=3,
        section_path="Citation",
        chunk_id="chunk-1",
        chunk_index=0,
    )
    context_hash = content_digest(text)
    citation = CitationBinding(
        citation_id="R1",
        source_id=source.source_id,
        chunk_id=source.chunk_id,
        context_block_id="context-1",
        display_label="source.md (p.3)",
        page=3,
        section_path="Citation",
        context_content_hash=context_hash,
    )
    provenance = RetrievalProvenance(
        source_id=source.source_id,
        chunk_id=source.chunk_id,
        original_rank=1,
        reranked_rank=1,
        retrieval_score=0.9,
        transformations=(
            RetrievalTransformation.LOADED,
            RetrievalTransformation.RERANKED,
            RetrievalTransformation.CONTEXT_SELECTED,
        ),
        original_content_hash=context_hash,
        context_content_hash=context_hash,
    )
    chunk = RetrievedChunk(
        "context-1",
        text,
        source,
        provenance,
        citation,
        ContextTrustLevel.UNTRUSTED_EXTERNAL,
        0.9,
    )
    return RetrievalExecutionResult(
        retrieval_id="result-1",
        status=RetrievalExecutionStatus.SUCCEEDED,
        rewritten_query_digest=content_digest("query"),
        final_chunks=(chunk,),
        citations=(citation,),
        stage_records=(),
        degraded=False,
        degradation_reasons=(),
        budget_usage=RetrievalBudgetUsage(),
        started_at=now,
        completed_at=now,
        duration_ms=0,
    )


class FakeRetrievalService:
    def __init__(self, chunk_text: str = "RAG_CHUNK_FROM_CURRENT_QUERY") -> None:
        self.chunk_text = chunk_text
        self.queries: list[str] = []
        self._result: RetrievalExecutionResult | None = None

    def execute(
        self,
        invocation: RetrievalInvocation,
        *,
        run_context,
        step_id: str = "retrieval",
        event_emitter=None,
        defer_completed_event: bool = False,
        fault_controller=None,
    ) -> RetrievalExecutionResult:
        self.queries.append(invocation.original_query)
        if self._result is None:
            self._result = make_successful_retrieval_result(self.chunk_text)
        return self._result

    def emit_stage_event(self, record, *, event_emitter, ignore_failure: bool = False):
        return None

    def emit_completed_event(self, result, *, event_emitter):
        return None


@pytest.fixture()
def real_router_environment():
    with tempfile.TemporaryDirectory() as directory:
        memory = MemoryManager(str(Path(directory) / "memory.db"))
        model = FakeModel()
        router = make_real_router(memory, model=model)
        yield memory, model, router


def seed_memory(memory: MemoryManager, *agent_ids: str) -> None:
    for agent_id in agent_ids:
        memory.add_message(
            agent_id,
            "user",
            OLD_MEMORY_SECRET,
            memory_scope="direct",
        )


# 6.1 历史隔离：router 级

def test_history_policy_none_blocks_old_memory_read(real_router_environment) -> None:
    memory, model, router = real_router_environment
    seed_memory(memory, "code_expert")
    context = make_run_context("code_expert")
    router.complete_single_agent(
        "code_expert",
        "CURRENT_INSTRUCTION_XYZ",
        run_context=context,
        capability_requirements=specialist_capabilities("code_expert"),
        persist=False,
        history_policy=HistoryPolicy.NONE,
    )
    rendered = render_messages(model.all_messages)
    assert OLD_MEMORY_SECRET not in rendered
    assert "CURRENT_INSTRUCTION_XYZ" in rendered
    assert memory.count_messages("code_expert") == 1


def test_default_agent_scope_still_reads_history(real_router_environment) -> None:
    memory, model, router = real_router_environment
    seed_memory(memory, "code_expert")
    context = make_run_context("code_expert")
    router.complete_single_agent(
        "code_expert",
        "direct question",
        run_context=context,
        capability_requirements=specialist_capabilities("code_expert"),
        persist=False,
        history_policy=HistoryPolicy.AGENT_SCOPE,
    )
    rendered = render_messages(model.all_messages)
    assert OLD_MEMORY_SECRET in rendered
    assert memory.count_messages("code_expert") == 1


def test_history_policy_none_does_not_write_summary(real_router_environment) -> None:
    memory, model, router = real_router_environment
    seed_memory(memory, "code_expert")
    context = make_run_context("code_expert")
    router.complete_single_agent(
        "code_expert",
        "current",
        run_context=context,
        capability_requirements=specialist_capabilities("code_expert"),
        persist=False,
        history_policy=HistoryPolicy.NONE,
    )
    # NONE 不触发滚动摘要维护，因此不产生额外 Memory 写入。
    assert memory.count_messages("code_expert") == 1
    assert OLD_MEMORY_SECRET not in render_messages(model.all_messages)


# 6.1 历史隔离：真实主链 Shape 3 E2E

@pytest.mark.asyncio
async def test_shape3_real_router_never_reads_old_memory() -> None:
    services = make_security_services()
    with tempfile.TemporaryDirectory() as directory:
        memory = MemoryManager(str(Path(directory) / "memory.db"))
        model = FakeModel(
            planning_json=delegated_json(
                task_ids=("code", "data"),
                synthesis_required=True,
            )
        )
        router = make_real_router(memory, model=model)
        seed_memory(memory, "code_expert", "data_analyst", "synthesis_agent")
        scope = await CoordinatedRuntimeFactory(
            router, services
        ).create_run_scope(
            "core_router",
            "coordinate two reviews",
            persist=False,
        )
        result = await scope.execute()

        assert result.status is RunStatus.FAILED
        assert result.error_code == "FINAL_OUTPUT_PIPELINE_NOT_READY"

        rendered = render_messages(model.all_messages)
        assert OLD_MEMORY_SECRET not in rendered
        # 当前 Binding instruction 正常进入模型输入。
        assert "Inspect the code contract." in rendered
        assert "Inspect the data contract." in rendered
        # synthesis 输入只含依赖结果与当前上下文，不含旧 Memory。
        synthesis_prompt = next(
            message[-1]["content"]
            for message in model.all_messages
            if message[-1]["content"].startswith("You are the synthesis agent")
        )
        assert "result-code_expert" in synthesis_prompt
        assert "result-data_analyst" in synthesis_prompt
        assert OLD_MEMORY_SECRET not in synthesis_prompt

        for agent_id in ("code_expert", "data_analyst", "synthesis_agent"):
            assert memory.count_messages(agent_id) == 1
        assert OLD_MEMORY_SECRET not in render_all_channels(
            services, scope.run_id
        )
        await scope.close()


# 6.2 当前 instruction 正常传递 + knowledge RAG

def test_knowledge_rag_uses_current_instruction_without_history(
    real_router_environment,
) -> None:
    memory, model, router = real_router_environment
    retrieval = FakeRetrievalService(chunk_text="RAG_CHUNK_FROM_CURRENT_QUERY")
    router.retrieval_execution_service = retrieval
    router.db_manager = _DummyDB()
    seed_memory(memory, "knowledge_expert")
    context = make_run_context("knowledge_expert")
    router.complete_single_agent(
        "knowledge_expert",
        "CURRENT_KB_QUESTION_ABC",
        run_context=context,
        capability_requirements=specialist_capabilities("knowledge_expert"),
        persist=False,
        history_policy=HistoryPolicy.NONE,
    )
    assert retrieval.queries == ["CURRENT_KB_QUESTION_ABC"]
    rendered = render_messages(model.all_messages)
    assert "RAG_CHUNK_FROM_CURRENT_QUERY" in rendered
    assert OLD_MEMORY_SECRET not in rendered
    assert "memory_summary" not in rendered
    assert memory.count_messages("knowledge_expert") == 1


# 6.3 兼容回归

def test_legacy_answer_read_path_keeps_history(real_router_environment) -> None:
    memory, _model, router = real_router_environment
    seed_memory(memory, "core_router")
    messages = router._prepare_answer_messages(
        "core_router",
        "legacy question",
    )
    assert OLD_MEMORY_SECRET in render_messages(messages)


@pytest.mark.asyncio
async def test_direct_single_agent_keeps_history_behavior() -> None:
    services = make_security_services()
    with tempfile.TemporaryDirectory() as directory:
        memory = MemoryManager(str(Path(directory) / "memory.db"))
        model = FakeModel(planning_json=direct_json("code_expert"))
        router = make_real_router(memory, model=model)
        seed_memory(memory, "code_expert")
        scope = await CoordinatedRuntimeFactory(
            router, services
        ).create_run_scope(
            "code_expert",
            "direct code question",
        )
        result = await scope.execute()
        assert result.status is RunStatus.SUCCEEDED
        assert OLD_MEMORY_SECRET in render_messages(model.all_messages)
        # 显式单 Agent 保持原写入行为：seed + user + assistant。
        assert memory.count_messages("code_expert") == 3
        await scope.close()


@pytest.mark.asyncio
async def test_core_direct_keeps_history_behavior() -> None:
    services = make_security_services()
    with tempfile.TemporaryDirectory() as directory:
        memory = MemoryManager(str(Path(directory) / "memory.db"))
        model = FakeModel(planning_json=direct_json("core_router"))
        router = make_real_router(memory, model=model)
        seed_memory(memory, "core_router")
        scope = await CoordinatedRuntimeFactory(
            router, services
        ).create_run_scope(
            "core_router",
            "direct core question",
        )
        result = await scope.execute()
        assert result.status is RunStatus.SUCCEEDED
        assert OLD_MEMORY_SECRET in render_messages(model.all_messages)
        assert memory.count_messages("core_router") == 3
        await scope.close()
