from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import re
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from core.agent_router import (
    AgentRouter,
    KnowledgeRetrievalFailedError,
    KnowledgeSourceNotFoundError,
)
from core.runtime import (
    BudgetLedger,
    BlockingTaskKind,
    BoundedBlockingExecutor,
    CancellationReason,
    InMemorySpanRecorder,
    ModelAdapterInvocationError,
    ModelAdapterResolver,
    ModelAdapterResponse,
    ModelCircuitBreakerConfig,
    ModelCircuitBreakerRegistry,
    ModelCostProfile,
    ModelFailureCategory,
    ModelInvocationRouter,
    ModelProfile,
    ModelProfileId,
    QueryEmbedding,
    QueryRewriteStrategy,
    RetrievalCandidate,
    RetrievalExecutionService,
    RetrievalExecutionSpec,
    RetrievalExecutionStatus,
    RetrievalInvocation,
    RuntimeKnowledgeRetrievalAdapter,
    RuntimeEventChannel,
    RuntimeEventType,
    RetrievalStageStatus,
    RetrievalStage,
    RunBudget,
    RunEventEmitter,
    RetryExecutor,
    RetryPolicy,
    SourceMetadata,
    VectorScoreSemantics,
    content_digest,
    create_run_context,
)
from core.knowledge_base.vector_db_manager import VectorDBManager
from core.runtime.retrieval_contract import MaterializedDocument


class FakeMemory:
    def __init__(self, summary: str = "") -> None:
        self.summary = summary
        self.search_calls = 0

    def count_messages(self, *args, **kwargs):
        return 0

    def get_summary_record(self, agent_id):
        return {"summary": self.summary, "last_message_id": 0}

    def get_chat_history(self, *args, **kwargs):
        return []

    def add_message(self, *args, **kwargs):
        return None

    def search_messages(self, *args, **kwargs):
        self.search_calls += 1
        raise AssertionError("query rewrite must not trigger memory retrieval")


class RewriteLLM:
    def generate(self, *args, **kwargs):
        yield "CDT"


class CountingAnswerLLM:
    def __init__(self, *, fail_final: bool = False):
        self.calls = 0
        self.fail_final = fail_final

    def generate(self, *args, **kwargs):
        self.calls += 1
        if self.fail_final and kwargs.get("max_tokens") == 640:
            raise RuntimeError("provider failure after empty retrieval")
        yield "没有找到可用的本地检索证据。"


class TrapLegacyLLM:
    def __init__(self):
        self.calls = 0

    def generate(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("legacy generate path must not run")
        yield  # pragma: no cover


class RewriteModelAdapter:
    def __init__(self, outcomes, *, sleep_seconds=0.0):
        self.outcomes = list(outcomes)
        self.sleep_seconds = sleep_seconds
        self.calls = 0
        self.messages = []
        self.thread_names = []

    def invoke(self, messages, *, max_tokens):
        self.calls += 1
        self.messages.append([dict(message) for message in messages])
        self.thread_names.append(threading.current_thread().name)
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return ModelAdapterResponse(str(outcome))


class RecordingInvocationRouter(ModelInvocationRouter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.results = []

    def invoke(self, **kwargs):
        result = super().invoke(**kwargs)
        self.results.append(result)
        return result


def make_unified_rewrite_router(
    model_adapter,
    *,
    registry=None,
    invocation_router=None,
    database=None,
    memory=None,
):
    profile = ModelProfile(
        ModelProfileId.LOCAL_FAST,
        4096,
        128,
        False,
        False,
        False,
        False,
        1,
        1,
        ModelCostProfile(
            ModelProfileId.LOCAL_FAST,
            False,
            fixed_call_cost_units=1,
        ),
        False,
        "rewrite-local",
    )
    legacy = TrapLegacyLLM()
    router = AgentRouter(
        legacy,
        memory or FakeMemory(),
        database or ExplicitVectorDB(),
        model_profiles=(profile,),
        model_adapter_resolver=ModelAdapterResolver(
            {ModelProfileId.LOCAL_FAST: model_adapter}
        ),
        model_invocation_router=invocation_router,
        circuit_breaker_registry=registry,
    )
    return router, legacy


class ExplicitVectorDB:
    collection_name = "local-kb"
    embedding_model_id = "fake-qwen3-embedding-0.6b"

    def __init__(self, *, embedding_failure: bool = False, empty: bool = False):
        self.embedding_failure = embedding_failure
        self.empty = empty
        self.embedded_queries: list[str] = []
        self.vector_queries = 0

    def embed_query(self, query):
        self.embedded_queries.append(query)
        if self.embedding_failure:
            raise RuntimeError("secret embedding failure")
        return [0.1, 0.2]

    def search_by_vector_with_scores(self, embedding, **kwargs):
        self.vector_queries += 1
        if self.empty:
            return []
        return [
            (
                SimpleNamespace(
                    page_content="CDT 是字段映射定义。",
                    metadata={
                        "doc_id": "doc-cdt",
                        "chunk_id": "chunk-cdt",
                        "chunk_index": 0,
                        "source": "docs/cdt.md",
                        "file_name": "cdt.md",
                        "file_hash": "version-1",
                        "source_type": "md",
                        "section_path": "CDT",
                    },
                ),
                0.9,
            )
        ]

    def keyword_search(self, *args, **kwargs):
        return []


class SemanticVectorDB:
    embedding_model_id = "fake"

    def __init__(self, semantics, scores):
        self.vector_score_semantics = semantics
        self.scores = scores

    def embed_query(self, query):
        return [0.1, 0.2]

    def search_by_vector_with_scores(self, embedding, **kwargs):
        rows = []
        for index, score in enumerate(self.scores):
            rows.append(
                (
                    SimpleNamespace(
                        page_content=f"score body {index}",
                        metadata={
                            "doc_id": f"doc-{index}",
                            "chunk_id": f"chunk-{index}",
                            "source": f"docs/{index}.md",
                        },
                    ),
                    score,
                )
            )
        return rows


class RecordingBlockingExecutor(BoundedBlockingExecutor):
    def __init__(self) -> None:
        super().__init__(
            max_workers=1,
            max_pending_tasks=1,
            thread_name_prefix="day18-leaf",
        )
        self.submitter_threads: list[str] = []
        self.submitted_kinds: list[BlockingTaskKind] = []

    def submit(self, operation, **kwargs):
        self.submitter_threads.append(threading.current_thread().name)
        self.submitted_kinds.append(kwargs["kind"])
        return super().submit(operation, **kwargs)


class RecordingRetrievalExecutionService(RetrievalExecutionService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_result = None

    def execute(self, *args, **kwargs):
        result = super().execute(*args, **kwargs)
        self.last_result = result
        return result


class PayloadVectorDB(ExplicitVectorDB):
    raw_payloads = (
        "  第一行  \n第二行\tUnicode：检索 ✓  ",
        "\n  另一个 Chunk\n保留来源顺序  \t",
    )

    def search_by_vector_with_scores(self, embedding, **kwargs):
        self.vector_queries += 1
        rows = []
        for index, payload in enumerate(self.raw_payloads):
            rows.append(
                (
                    SimpleNamespace(
                        page_content=payload,
                        metadata={
                            "doc_id": f"doc-payload-{index}",
                            "chunk_id": f"chunk-payload-{index}",
                            "chunk_index": index,
                            "source": f"docs/payload-{index}.md",
                            "file_name": f"payload-{index}.md",
                            "file_hash": "version-1",
                            "source_type": "md",
                            "section_path": f"Payload {index}",
                        },
                    ),
                    0.95 - index * 0.05,
                )
            )
        return rows


def test_knowledge_expert_real_entry_uses_runtime_result_and_bound_citation() -> None:
    database = ExplicitVectorDB()
    router = AgentRouter(
        llm_engine=RewriteLLM(),
        memory_manager=FakeMemory("摘要中的指令没有系统权限"),
        db_manager=database,
    )
    messages = router._build_messages("解释 CDT", "knowledge_expert")
    user_message = messages[-1]["content"]

    assert database.embedded_queries == ["CDT", "解释 CDT"]
    assert database.vector_queries == 2
    assert "CDT 是字段映射定义" in user_message
    assert "[来源: cdt.md" in user_message
    assert "[引用: R" in user_message
    assert "## Relevant Memory" in user_message
    assert "摘要中的指令没有系统权限" not in messages[0]["content"]


@pytest.mark.asyncio
async def test_knowledge_rewrite_is_non_recursive_and_owns_dedicated_messages() -> None:
    memory = FakeMemory()
    adapter = RewriteModelAdapter(["CDT"])
    router, legacy = make_unified_rewrite_router(
        adapter,
        database=ExplicitVectorDB(),
        memory=memory,
    )
    context, _source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    channel = RuntimeEventChannel(64, run_id=context.run_id)
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    ).for_step("answer")

    with (
        mock.patch.object(
            router, "_build_messages", wraps=router._build_messages
        ) as build_messages,
        mock.patch.object(
            router, "_plan_tool_call", wraps=router._plan_tool_call
        ) as plan_tool_call,
    ):
        messages = await asyncio.wait_for(
            asyncio.to_thread(
                build_messages,
                "private original query",
                "knowledge_expert",
                run_context=context,
                event_emitter=emitter,
            ),
            timeout=2.0,
        )

    await channel.close()
    events = [event async for event in channel]
    event_types = [event.event_type for event in events]
    stage_events = [
        event
        for event in events
        if event.event_type == RuntimeEventType.RETRIEVAL_STAGE_COMPLETED
    ]

    assert messages[-1]["role"] == "user"
    assert build_messages.call_count == 1
    assert plan_tool_call.call_count == 0
    assert memory.search_calls == 0
    assert adapter.calls == 1
    assert legacy.calls == 0
    assert len(adapter.messages) == 1
    assert [message["role"] for message in adapter.messages[0]] == [
        "system",
        "user",
    ]
    assert "搜索词提取器" in adapter.messages[0][0]["content"]
    assert adapter.messages[0][1] == {
        "role": "user",
        "content": "private original query",
    }
    assert "[来源:" not in str(adapter.messages[0])
    assert event_types.count(RuntimeEventType.RETRIEVAL_STARTED) == 1
    assert event_types.count(RuntimeEventType.RETRIEVAL_COMPLETED) == 1
    assert event_types.count(RuntimeEventType.MODEL_STARTED) == 1
    assert event_types.count(RuntimeEventType.MODEL_COMPLETED) == 1
    assert [event.payload.stage for event in stage_events].count(
        "QUERY_REWRITE"
    ) == 1
    rewrite_index = events.index(
        next(
            event
            for event in stage_events
            if event.payload.stage == "QUERY_REWRITE"
        )
    )
    embedding_index = events.index(
        next(
            event
            for event in stage_events
            if event.payload.stage == "EMBEDDING"
        )
    )
    assert event_types.index(RuntimeEventType.RETRIEVAL_STARTED) < (
        event_types.index(RuntimeEventType.MODEL_STARTED)
    )
    assert event_types.index(RuntimeEventType.MODEL_STARTED) < (
        event_types.index(RuntimeEventType.MODEL_COMPLETED)
    )
    assert event_types.index(RuntimeEventType.MODEL_COMPLETED) < rewrite_index
    assert rewrite_index < embedding_index
    assert embedding_index < event_types.index(
        RuntimeEventType.RETRIEVAL_COMPLETED
    )


def test_knowledge_expert_continues_empty_but_embedding_failure_remains_failure() -> None:
    empty_llm = CountingAnswerLLM()
    empty_router = AgentRouter(
        empty_llm, FakeMemory(), ExplicitVectorDB(empty=True)
    )
    messages = empty_router._build_messages("解释 CDT", "knowledge_expert")
    combined = "\n".join(message["content"] for message in messages)
    assert "knowledge_retrieval_status=EMPTY" in combined
    assert len([message for message in messages if message["role"] == "user"]) == 1
    assert "Retrieved Documents" not in combined
    assert empty_llm.calls == 1

    assert empty_router._complete_final_response("knowledge_expert", "解释 CDT")
    assert empty_llm.calls == 3

    failed_router = AgentRouter(
        RewriteLLM(),
        FakeMemory(),
        ExplicitVectorDB(embedding_failure=True),
    )
    with pytest.raises(KnowledgeRetrievalFailedError) as raised:
        failed_router._build_messages("解释 CDT", "knowledge_expert")
    assert raised.value.result.status == RetrievalExecutionStatus.FAILED
    assert raised.value.result.error.safe_error_code == "EMBEDDING_FAILED"
    assert not isinstance(raised.value, KnowledgeSourceNotFoundError)


def test_empty_followed_by_model_provider_failure_remains_failure() -> None:
    provider = CountingAnswerLLM(fail_final=True)
    router = AgentRouter(provider, FakeMemory(), ExplicitVectorDB(empty=True))

    with pytest.raises(RuntimeError, match="provider failure after empty retrieval"):
        router._complete_final_response("knowledge_expert", "解释 CDT")
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_mandatory_retrieval_context_overflow_is_typed_failure() -> None:
    router = AgentRouter(
        RewriteLLM(),
        FakeMemory(),
        ExplicitVectorDB(),
        max_tokens=100,
        model_context_window=128,
    )
    router.knowledge_rewrite_max_tokens = 8
    context, _source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    channel = RuntimeEventChannel(64, run_id=context.run_id)
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    ).for_step("answer")
    with pytest.raises(KnowledgeRetrievalFailedError) as raised:
        await asyncio.to_thread(
            router._build_messages,
            "解释 CDT",
            "knowledge_expert",
            run_context=context,
            event_emitter=emitter,
        )
    await channel.close()
    events = [event async for event in channel]
    result = raised.value.result
    assert result.status == RetrievalExecutionStatus.FAILED
    assert result.error.category.value == "CONTEXT_BUILD_FAILED"
    assert result.error.safe_error_code == "CONTEXT_BUILD_FAILED"
    assert result.final_chunks == ()
    assert result.citations == ()
    assert result.stage_records[-1].status == RetrievalStageStatus.FAILED
    completed = [
        event
        for event in events
        if event.event_type == RuntimeEventType.RETRIEVAL_COMPLETED
    ]
    assert len(completed) == 1
    assert completed[0].payload.status == "FAILED"
    assert completed[0].payload.safe_error_code == "CONTEXT_BUILD_FAILED"
    context_stage = [
        event
        for event in events
        if event.event_type == RuntimeEventType.RETRIEVAL_STAGE_COMPLETED
        and event.payload.stage == "CONTEXT_BUILD"
    ]
    assert len(context_stage) == 1
    assert context_stage[0].payload.status == "FAILED"


class EventAdapter:
    query_rewrite_strategy = QueryRewriteStrategy.NONE
    has_explicit_embedding = True
    has_reranker = False

    def rewrite_query(self, query):
        return query

    def embed_query(self, query):
        return QueryEmbedding.create(query, [0.1, 0.2], "fake")

    def retrieve(self, query, embedding, invocation, *, max_candidates):
        source = SourceMetadata(
            "source",
            "md",
            "kb",
            "docs/source.md",
            "source.md",
            "v1",
            None,
            "",
            "chunk",
            0,
        )
        return [
            RetrievalCandidate(
                "candidate",
                source,
                0.9,
                1,
                {"chunk_id": "chunk"},
                "fake:chunk",
                "private chunk body",
            )
        ]

    def keyword_retrieve(self, terms, invocation, *, max_candidates):
        return []

    def rerank(self, rewritten_query, original_query, candidates):
        return list(candidates)

    def materialize(self, candidate):
        return MaterializedDocument(
            candidate,
            candidate.text,
            content_digest(candidate.text),
        )


@pytest.mark.asyncio
async def test_retrieval_runtime_events_are_typed_and_content_safe() -> None:
    context, _source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    channel = RuntimeEventChannel(32, run_id=context.run_id)
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    ).for_step("answer")
    recorder = InMemorySpanRecorder()
    service = RetrievalExecutionService(EventAdapter(), span_recorder=recorder)
    invocation = RetrievalInvocation.create(
        "private query body",
        collection_names=("kb",),
        top_k=2,
        rerank_top_k=1,
    )

    result = await asyncio.to_thread(
        service.execute,
        invocation,
        run_context=context,
        step_id="answer",
        event_emitter=emitter,
    )
    await channel.close()
    events = [event async for event in channel]

    assert result.status == RetrievalExecutionStatus.SUCCEEDED
    assert events[0].event_type == RuntimeEventType.RETRIEVAL_STARTED
    assert events[-1].event_type == RuntimeEventType.RETRIEVAL_COMPLETED
    retrieval_span = next(
        record for record in recorder.snapshot() if record.component == "retrieval"
    )
    assert events[0].span_id == retrieval_span.span_id
    assert events[-1].span_id == retrieval_span.span_id
    stage_spans = {
        record.attributes["retrieval_stage"]: record
        for record in recorder.snapshot()
        if record.component == "retrieval_stage"
    }
    for event in events:
        if event.event_type is RuntimeEventType.RETRIEVAL_STAGE_COMPLETED:
            span = stage_spans[event.payload.stage]
            assert event.span_id == span.span_id
            assert event.parent_span_id == span.parent_span_id
    assert len(
        [
            event
            for event in events
            if event.event_type == RuntimeEventType.RETRIEVAL_STAGE_COMPLETED
        ]
    ) == len(
        [
            record
            for record in result.stage_records
            if record.status is not RetrievalStageStatus.SKIPPED
        ]
    )
    safe = str([event.to_safe_dict() for event in events])
    assert "private query body" not in safe
    assert "private chunk body" not in safe
    assert "[0.1, 0.2]" not in safe


@pytest.mark.asyncio
async def test_query_rewrite_uses_model_contract_budget_events_and_order() -> None:
    adapter = RewriteModelAdapter(["CDT"])
    database = ExplicitVectorDB()
    router, legacy = make_unified_rewrite_router(
        adapter, database=database
    )
    context, _source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    channel = RuntimeEventChannel(64, run_id=context.run_id)
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    ).for_step("answer")

    result = await asyncio.to_thread(
        router._execute_knowledge_retrieval,
        "private original query",
        run_context=context,
        event_emitter=emitter,
    )
    await channel.close()
    events = [event async for event in channel]
    event_types = [event.event_type for event in events]

    assert result.status == RetrievalExecutionStatus.SUCCEEDED
    assert adapter.calls == 1
    assert legacy.calls == 0
    usage = context.budget_ledger.snapshot().committed_usage
    assert usage.model_calls == 1
    assert usage.input_tokens > 0
    assert usage.output_tokens == router.knowledge_rewrite_max_tokens
    assert usage.cost_units == 1
    assert event_types.count(RuntimeEventType.MODEL_STARTED) == 1
    assert event_types.count(RuntimeEventType.MODEL_COMPLETED) == 1
    rewrite_stage_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type == RuntimeEventType.RETRIEVAL_STAGE_COMPLETED
        and event.payload.stage == "QUERY_REWRITE"
    )
    assert event_types.index(RuntimeEventType.RETRIEVAL_STARTED) < (
        event_types.index(RuntimeEventType.MODEL_STARTED)
    )
    assert event_types.index(RuntimeEventType.MODEL_STARTED) < (
        event_types.index(RuntimeEventType.MODEL_COMPLETED)
    )
    assert event_types.index(RuntimeEventType.MODEL_COMPLETED) < (
        rewrite_stage_index
    )
    safe = str([event.to_safe_dict() for event in events])
    assert "private original query" not in safe


@pytest.mark.asyncio
async def test_degraded_rewrite_event_order_continues_only_current_retrieval() -> None:
    adapter = RewriteModelAdapter(
        [
            ModelAdapterInvocationError(
                ModelFailureCategory.UNKNOWN_FAILURE,
                provider_started=True,
                provider_responded=False,
            )
        ]
    )
    database = ExplicitVectorDB()
    router, legacy = make_unified_rewrite_router(adapter, database=database)
    context, _source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    channel = RuntimeEventChannel(64, run_id=context.run_id)
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    ).for_step("answer")

    result = await asyncio.wait_for(
        asyncio.to_thread(
            router._execute_knowledge_retrieval,
            "original query",
            run_context=context,
            event_emitter=emitter,
        ),
        timeout=2.0,
    )
    await channel.close()
    events = [event async for event in channel]
    event_types = [event.event_type for event in events]
    rewrite_event = next(
        event
        for event in events
        if event.event_type == RuntimeEventType.RETRIEVAL_STAGE_COMPLETED
        and event.payload.stage == "QUERY_REWRITE"
    )
    embedding_event = next(
        event
        for event in events
        if event.event_type == RuntimeEventType.RETRIEVAL_STAGE_COMPLETED
        and event.payload.stage == "EMBEDDING"
    )
    model_completed = next(
        event
        for event in events
        if event.event_type == RuntimeEventType.MODEL_COMPLETED
    )

    assert result.status == RetrievalExecutionStatus.DEGRADED
    assert model_completed.payload.succeeded is False
    assert rewrite_event.payload.status == "FAILED"
    assert rewrite_event.payload.degraded is True
    assert events.index(model_completed) < events.index(rewrite_event)
    assert events.index(rewrite_event) < events.index(embedding_event)
    assert event_types.count(RuntimeEventType.RETRIEVAL_STARTED) == 1
    assert event_types.count(RuntimeEventType.RETRIEVAL_COMPLETED) == 1
    assert database.embedded_queries == ["original query"]
    assert database.vector_queries == 1
    assert adapter.calls == 1
    assert legacy.calls == 0


def test_query_rewrite_circuit_degrades_but_budget_blocks_without_adapter() -> None:
    registry = ModelCircuitBreakerRegistry(
        ModelCircuitBreakerConfig(failure_threshold=1)
    )
    registry.get("rewrite-local").acquire_permission().record_failure()
    circuit_adapter = RewriteModelAdapter(["must not run"])
    circuit_router, legacy = make_unified_rewrite_router(
        circuit_adapter, registry=registry
    )
    context, _source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    circuit_result = circuit_router._execute_knowledge_retrieval(
        "original query", run_context=context
    )
    assert circuit_result.status == RetrievalExecutionStatus.DEGRADED
    assert circuit_adapter.calls == 0
    assert legacy.calls == 0
    assert context.budget_ledger.snapshot().committed_usage.model_calls == 0

    ordinary_adapter = RewriteModelAdapter(
        [
            ModelAdapterInvocationError(
                ModelFailureCategory.UNKNOWN_FAILURE,
                provider_started=True,
                provider_responded=False,
            )
        ]
    )
    ordinary_db = ExplicitVectorDB()
    ordinary_router, legacy = make_unified_rewrite_router(
        ordinary_adapter, database=ordinary_db
    )
    context, _source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    ordinary_result = ordinary_router._execute_knowledge_retrieval(
        "original query", run_context=context
    )
    assert ordinary_result.status == RetrievalExecutionStatus.DEGRADED
    assert ordinary_db.embedded_queries == ["original query"]
    assert context.budget_ledger.snapshot().committed_usage.model_calls == 1
    assert legacy.calls == 0

    budget_adapter = RewriteModelAdapter(["must not run"])
    database = ExplicitVectorDB()
    budget_router, legacy = make_unified_rewrite_router(
        budget_adapter, database=database
    )
    context, _source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(
        BudgetLedger(RunBudget(max_model_calls=0))
    )
    budget_result = budget_router._execute_knowledge_retrieval(
        "original query", run_context=context
    )
    assert budget_result.status == RetrievalExecutionStatus.FAILED
    assert budget_result.error.category.value == "BUDGET_EXHAUSTED"
    assert budget_adapter.calls == 0
    assert database.embedded_queries == []
    assert legacy.calls == 0


def test_query_rewrite_retry_is_owned_by_existing_retry_executor() -> None:
    transient = ModelAdapterInvocationError(
        ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE,
        provider_started=True,
        provider_responded=False,
    )
    adapter = RewriteModelAdapter([transient, "CDT"])
    invocation_router = RecordingInvocationRouter(
        retry_executor=RetryExecutor(
            RetryPolicy(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
            )
        )
    )
    router, legacy = make_unified_rewrite_router(
        adapter, invocation_router=invocation_router
    )
    context, _source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    result = router._execute_knowledge_retrieval(
        "original query", run_context=context
    )
    usage = context.budget_ledger.snapshot().committed_usage
    assert result.status == RetrievalExecutionStatus.SUCCEEDED
    assert adapter.calls == 2
    assert usage.model_calls == 2
    assert usage.retries == 1
    assert legacy.calls == 0
    attempts = invocation_router.results[0].attempts
    assert [item.candidate_index for item in attempts] == [0, 0]
    assert [item.retry_index for item in attempts] == [0, 1]
    assert len({item.profile_id for item in attempts}) == 1
    assert len({item.breaker_key for item in attempts}) == 1


def test_query_rewrite_cancel_timeout_and_safety_do_not_degrade_continue() -> None:
    cancelled_adapter = RewriteModelAdapter(["must not run"])
    cancelled_router, _legacy = make_unified_rewrite_router(
        cancelled_adapter
    )
    context, source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    source.cancel(CancellationReason.USER_CANCELLED)
    cancelled = cancelled_router._execute_knowledge_retrieval(
        "original query", run_context=context
    )
    assert cancelled.status == RetrievalExecutionStatus.CANCELLED
    assert cancelled_adapter.calls == 0

    safety_adapter = RewriteModelAdapter(
        [
            ModelAdapterInvocationError(
                ModelFailureCategory.SAFETY_REFUSAL,
                provider_started=True,
                provider_responded=True,
            )
        ]
    )
    safety_db = ExplicitVectorDB()
    safety_router, _legacy = make_unified_rewrite_router(
        safety_adapter, database=safety_db
    )
    context, _source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    safety = safety_router._execute_knowledge_retrieval(
        "original query", run_context=context
    )
    assert safety.status == RetrievalExecutionStatus.FAILED
    assert safety.error.category.value == "VALIDATION"
    assert safety_db.embedded_queries == []

    timeout_adapter = RewriteModelAdapter(["late"], sleep_seconds=0.1)
    timeout_db = ExplicitVectorDB()
    timeout_router, _legacy = make_unified_rewrite_router(
        timeout_adapter, database=timeout_db
    )
    timeouts = dict(RetrievalExecutionSpec().stage_timeouts)
    timeouts[RetrievalStage.QUERY_REWRITE] = 0.01
    timeout_router.retrieval_execution_service.spec = RetrievalExecutionSpec(
        stage_timeouts=timeouts
    )
    context, _source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    timed_out = timeout_router._execute_knowledge_retrieval(
        "original query", run_context=context
    )
    assert timed_out.status == RetrievalExecutionStatus.TIMED_OUT
    assert timed_out.execution_detached is True
    assert timeout_db.embedded_queries == []
    assert timeout_router.retrieval_execution_service.blocking_executor.wait_until_idle(
        1.0
    )


def test_single_worker_full_retrieval_and_two_concurrent_runs_do_not_deadlock() -> None:
    adapter = RewriteModelAdapter(["CDT", "CDT", "CDT"])
    router, legacy = make_unified_rewrite_router(
        adapter,
        database=ExplicitVectorDB(),
    )
    executor = RecordingBlockingExecutor()
    router.retrieval_execution_service.blocking_executor = executor

    def run_one(query):
        context, _source = create_run_context(
            entry_agent_id="knowledge_expert",
            timeout_seconds=1.0,
        )
        context.attach_budget_ledger(
            BudgetLedger(
                RunBudget(),
                deadline_remaining=context.remaining_seconds,
            )
        )
        return router._execute_knowledge_retrieval(
            query, run_context=context
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="day18-orchestrator",
        ) as pool:
            single = pool.submit(run_one, "single retrieval").result(timeout=2.0)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="day18-orchestrator",
        ) as pool:
            concurrent_results = [
                future.result(timeout=2.0)
                for future in (
                    pool.submit(run_one, "concurrent retrieval A"),
                    pool.submit(run_one, "concurrent retrieval B"),
                )
            ]
        assert single.status == RetrievalExecutionStatus.SUCCEEDED
        assert all(
            result.status == RetrievalExecutionStatus.SUCCEEDED
            for result in concurrent_results
        )
        assert adapter.calls == 3
        assert legacy.calls == 0
        assert all(
            name.startswith("day18-leaf")
            for name in adapter.thread_names
        )
        assert executor.submitter_threads
        assert all(
            name.startswith("day18-orchestrator")
            for name in executor.submitter_threads
        )
        assert BlockingTaskKind.QUERY_REWRITE in executor.submitted_kinds
        assert BlockingTaskKind.EMBEDDING in executor.submitted_kinds
        assert BlockingTaskKind.VECTOR_QUERY in executor.submitted_kinds
        assert executor.wait_until_idle(0.5)
    finally:
        assert executor.shutdown(wait=True, timeout=0.5)


@pytest.mark.asyncio
async def test_detached_timeout_emits_one_terminal_stage_and_only_tracker_cleans_up() -> None:
    adapter = RewriteModelAdapter(["late"], sleep_seconds=0.1)
    router, _legacy = make_unified_rewrite_router(adapter)
    timeouts = dict(RetrievalExecutionSpec().stage_timeouts)
    timeouts[RetrievalStage.QUERY_REWRITE] = 0.01
    router.retrieval_execution_service.spec = RetrievalExecutionSpec(
        stage_timeouts=timeouts
    )
    context, _source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    channel = RuntimeEventChannel(64, run_id=context.run_id)
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    ).for_step("answer")
    result = await asyncio.to_thread(
        router._execute_knowledge_retrieval,
        "private original query",
        run_context=context,
        event_emitter=emitter,
    )
    await asyncio.sleep(0.15)
    await channel.close()
    events = [event async for event in channel]
    rewrite_terminal = [
        event
        for event in events
        if event.event_type == RuntimeEventType.RETRIEVAL_STAGE_COMPLETED
        and event.payload.stage == "QUERY_REWRITE"
    ]
    retrieval_completed = [
        event
        for event in events
        if event.event_type == RuntimeEventType.RETRIEVAL_COMPLETED
    ]

    assert result.status == RetrievalExecutionStatus.TIMED_OUT
    assert len(rewrite_terminal) == 1
    assert rewrite_terminal[0].payload.worker_terminated is False
    assert rewrite_terminal[0].payload.execution_detached is True
    assert rewrite_terminal[0].payload.background_work_pending is True
    assert len(retrieval_completed) == 1
    assert retrieval_completed[0].payload.background_work_pending is True
    assert router.retrieval_execution_service.blocking_executor.wait_until_idle(
        1.0
    )


def test_vector_score_semantics_are_declared_and_converted_exactly_once() -> None:
    invocation = RetrievalInvocation.create(
        "query",
        collection_names=("kb",),
        top_k=2,
        rerank_top_k=1,
    )
    raw_adapter = RuntimeKnowledgeRetrievalAdapter(
        SemanticVectorDB(VectorScoreSemantics.RAW_DISTANCE, [0.0, 3.0]),
        query_rewriter=None,
        query_term_extractor=None,
        candidate_scorer=None,
    )
    raw = raw_adapter.retrieve(
        "query",
        QueryEmbedding.create("query", [0.1, 0.2], "fake"),
        invocation,
        max_candidates=2,
    )
    assert [item.score for item in raw] == [1.0, 0.25]
    raw_filtered = RetrievalExecutionService(
        raw_adapter, minimum_score=0.0
    )._filter_candidates(raw)
    assert [item.score for item in raw_filtered] == [1.0]

    relevance_adapter = RuntimeKnowledgeRetrievalAdapter(
        SemanticVectorDB(
            VectorScoreSemantics.NORMALIZED_RELEVANCE,
            [0.8, 0.2],
        ),
        query_rewriter=None,
        query_term_extractor=None,
        candidate_scorer=None,
    )
    relevance = relevance_adapter.retrieve(
        "query",
        QueryEmbedding.create("query", [0.1, 0.2], "fake"),
        invocation,
        max_candidates=2,
    )
    assert [item.score for item in relevance] == [0.8, 0.2]
    assert VectorDBManager.chroma_by_vector_score_semantics is (
        VectorScoreSemantics.RAW_DISTANCE
    )
    assert VectorDBManager.vector_score_semantics is (
        VectorScoreSemantics.NORMALIZED_RELEVANCE
    )
    filtered = RetrievalExecutionService(
        relevance_adapter, minimum_score=0.0
    )._filter_candidates(relevance)
    assert [item.score for item in filtered] == [0.8]

    undeclared = RuntimeKnowledgeRetrievalAdapter(
        SemanticVectorDB("RAW_DISTANCE", [0.0]),
        query_rewriter=None,
        query_term_extractor=None,
        candidate_scorer=None,
    )
    with pytest.raises(
        ValueError,
        match="semantics 必须显式声明为枚举值",
    ):
        undeclared.retrieve(
            "query",
            QueryEmbedding.create("query", [0.1, 0.2], "fake"),
            invocation,
            max_candidates=1,
        )


def test_final_model_context_payload_hash_and_citation_order_are_exact() -> None:
    database = PayloadVectorDB()
    adapter = RewriteModelAdapter(["payload query"])
    router, _legacy = make_unified_rewrite_router(adapter, database=database)
    service = RecordingRetrievalExecutionService(
        router.retrieval_execution_service.adapter,
        blocking_executor=router.retrieval_execution_service.blocking_executor,
    )
    router.retrieval_execution_service = service
    context, _source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))

    messages = router._build_messages(
        "检查 Payload",
        "knowledge_expert",
        run_context=context,
    )
    result = service.last_result
    assert result is not None
    assert result.status == RetrievalExecutionStatus.SUCCEEDED
    assert len(result.final_chunks) == len(result.citations) == 2
    model_context = messages[-1]["content"]
    positions = []
    for chunk in result.final_chunks:
        payload_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
        assert chunk.citation.context_content_hash == payload_hash
        assert chunk.provenance.context_content_hash == payload_hash
        wrapped = (
            f"[来源: {chunk.citation.display_label}]\n"
            f"{chunk.text}\n"
            f"[引用: {chunk.citation.citation_id}]"
        )
        assert wrapped in model_context
        assert hashlib.sha256(wrapped.encode("utf-8")).hexdigest() != payload_hash
        positions.append(model_context.index(wrapped))

    assert positions == sorted(positions)
    assert tuple(chunk.citation for chunk in result.final_chunks) == result.citations
    assert re.findall(r"\[引用: (R[^\]]+)\]", model_context) == [
        citation.citation_id for citation in result.citations
    ]
    assert any("\n" in payload for payload in database.raw_payloads)
    assert any(payload != payload.strip() for payload in database.raw_payloads)
    assert all(
        chunk.text == " ".join(raw_payload.split())
        for chunk, raw_payload in zip(
            result.final_chunks, database.raw_payloads, strict=True
        )
    )
