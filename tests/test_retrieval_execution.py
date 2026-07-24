from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import replace

import pytest

from core.runtime import (
    BudgetLedger,
    BlockingExecutorNestedSubmissionError,
    BlockingTaskKind,
    BoundedBlockingExecutor,
    CancellationReason,
    CancellationSource,
    QueryEmbedding,
    QueryRewriteStrategy,
    RetrievalAdapterError,
    RetrievalCandidate,
    RetrievalErrorCategory,
    RetrievalExecutionService,
    RetrievalExecutionSpec,
    RetrievalExecutionStatus,
    RetrievalInvocation,
    RetrievalStage,
    RetrievalStageStatus,
    RunBudget,
    RunCancelledError,
    RunRegistry,
    SourceMetadata,
    content_digest,
    create_run_context,
)
from core.runtime.retrieval_contract import MaterializedDocument


class FakeRetrievalAdapter:
    query_rewrite_strategy = QueryRewriteStrategy.EXISTING_MODEL
    has_explicit_embedding = True
    has_reranker = True

    def __init__(self, *, document_count: int = 2) -> None:
        self.document_count = document_count
        self.rewrite_failure = False
        self.embedding_failure = False
        self.vector_failure = False
        self.rerank_failure = False
        self.failed_load_ids: set[str] = set()
        self.sleep_rewrite_seconds = 0.0
        self.rewritten_query = "rewritten query"
        self.calls: list[str] = []

    def rewrite_query(
        self, query: str, *, run_context, event_emitter
    ) -> str:
        self.calls.append("rewrite")
        if self.sleep_rewrite_seconds:
            time.sleep(self.sleep_rewrite_seconds)
        if self.rewrite_failure:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.QUERY_REWRITE_FAILED,
                "QUERY_REWRITE_FAILED",
                "改写失败。",
            )
        return self.rewritten_query

    def embed_query(self, query: str) -> QueryEmbedding:
        self.calls.append("embed")
        if self.embedding_failure:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.EMBEDDING_FAILED,
                "EMBEDDING_FAILED",
                "Embedding 失败。",
            )
        return QueryEmbedding.create(query, [0.1, 0.2, 0.3], "fake-embedding")

    def retrieve(self, query, embedding, invocation, *, max_candidates):
        self.calls.append("retrieve")
        if self.vector_failure:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.VECTOR_STORE_FAILED,
                "VECTOR_STORE_FAILED",
                "Vector Store 失败。",
            )
        candidates = []
        for index in range(self.document_count):
            chunk_id = f"chunk-{index}"
            source = SourceMetadata(
                source_id=f"source-{index}",
                source_type="md",
                collection="kb",
                canonical_uri=f"doc-{index}.md",
                display_name=f"doc-{index}.md",
                document_version="v1",
                page=index + 1,
                section_path="Section",
                chunk_id=chunk_id,
                chunk_index=index,
            )
            candidates.append(
                RetrievalCandidate(
                    candidate_id=chunk_id,
                    source=source,
                    score=0.9 - index * 0.05,
                    original_rank=index + 1,
                    metadata={"chunk_id": chunk_id},
                    content_locator=f"fake:{chunk_id}",
                    text=f"Document {index} says retrieval facts.",
                )
            )
        return candidates[:max_candidates]

    def keyword_retrieve(self, terms, invocation, *, max_candidates):
        self.calls.append("keyword")
        return []

    def should_keyword_retrieve(self, terms, invocation):
        return bool(terms and not invocation.filters)

    def rerank(self, rewritten_query, original_query, candidates):
        self.calls.append("rerank")
        if self.rerank_failure:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.RERANK_FAILED,
                "RERANK_FAILED",
                "Rerank 失败。",
            )
        return [
            replace(
                candidate,
                reranked_score=candidate.score,
                reranked_rank=index,
            )
            for index, candidate in enumerate(candidates, start=1)
        ]

    def materialize(self, candidate):
        self.calls.append(f"load:{candidate.chunk_id}")
        if candidate.chunk_id in self.failed_load_ids:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.DOCUMENT_LOAD_FAILED,
                "DOCUMENT_LOAD_FAILED",
                "文档读取失败。",
            )
        assert candidate.text is not None
        return MaterializedDocument(
            candidate,
            candidate.text,
            content_digest(candidate.text),
        )


def make_context(**budget_limits):
    context, source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(
        BudgetLedger(
            RunBudget(**budget_limits),
            deadline_remaining=context.remaining_seconds,
        )
    )
    return context, source


def make_invocation() -> RetrievalInvocation:
    return RetrievalInvocation.create(
        "original query",
        collection_names=("kb",),
        top_k=4,
        rerank_top_k=2,
        requested_timeout_seconds=2.0,
        retrieval_id="retrieval-test",
    )


def test_successful_pipeline_records_every_stage_and_budget() -> None:
    adapter = FakeRetrievalAdapter()
    context, _source = make_context()
    result = RetrievalExecutionService(adapter).execute(
        make_invocation(), run_context=context
    )

    assert result.status == RetrievalExecutionStatus.SUCCEEDED
    assert [record.stage for record in result.stage_records] == list(RetrievalStage)
    assert all(
        record.status == RetrievalStageStatus.SUCCEEDED
        for record in result.stage_records
    )
    assert len(result.final_chunks) == len(result.citations) == 2
    assert result.budget_usage.retrieval_calls == 1
    assert result.budget_usage.embedding_calls == 2
    assert result.budget_usage.vector_queries == 2
    assert result.budget_usage.keyword_queries == 1
    assert result.budget_usage.document_reads == len(result.final_chunks) == 2
    assert result.budget_usage.context_chars == sum(
        len(chunk.text) for chunk in result.final_chunks
    )
    ledger_usage = context.budget_ledger.snapshot().committed_usage
    assert result.budget_usage.to_safe_dict() == {
        "retrieval_calls": ledger_usage.retrieval_calls,
        "embedding_calls": ledger_usage.embedding_calls,
        "vector_queries": ledger_usage.vector_queries,
        "keyword_queries": ledger_usage.keyword_queries,
        "document_reads": ledger_usage.document_reads,
        "context_chars": ledger_usage.context_chars,
    }
    assert "original query" not in str(result.to_safe_dict())


def test_empty_is_only_returned_after_valid_zero_candidate_query() -> None:
    adapter = FakeRetrievalAdapter(document_count=0)
    context, _source = make_context()
    result = RetrievalExecutionService(adapter).execute(
        make_invocation(), run_context=context
    )

    assert result.status == RetrievalExecutionStatus.EMPTY
    assert result.error is None
    assert result.stage_records[2].stage == RetrievalStage.RETRIEVE
    assert result.stage_records[2].status == RetrievalStageStatus.SUCCEEDED
    assert all(
        record.status == RetrievalStageStatus.SKIPPED
        for record in result.stage_records[3:]
    )


def test_embedding_and_vector_failures_are_not_reported_as_empty() -> None:
    for flag, category in (
        ("embedding_failure", RetrievalErrorCategory.EMBEDDING_FAILED),
        ("vector_failure", RetrievalErrorCategory.VECTOR_STORE_FAILED),
    ):
        adapter = FakeRetrievalAdapter()
        setattr(adapter, flag, True)
        context, _source = make_context()
        result = RetrievalExecutionService(adapter).execute(
            make_invocation(), run_context=context
        )
        assert result.status == RetrievalExecutionStatus.FAILED
        assert result.error is not None
        assert result.error.category == category
        assert result.status != RetrievalExecutionStatus.EMPTY


def test_query_rewrite_and_rerank_failures_are_controlled_degradation() -> None:
    for flag, expected_reason in (
        ("rewrite_failure", "QUERY_REWRITE_FAILED"),
        ("rerank_failure", "RERANK_FAILED"),
    ):
        adapter = FakeRetrievalAdapter()
        setattr(adapter, flag, True)
        context, _source = make_context()
        result = RetrievalExecutionService(adapter).execute(
            make_invocation(), run_context=context
        )
        assert result.status == RetrievalExecutionStatus.DEGRADED
        assert result.degraded is True
        assert expected_reason in result.degradation_reasons
        failed = next(
            record
            for record in result.stage_records
            if record.safe_error_code == expected_reason
        )
        assert failed.status == RetrievalStageStatus.FAILED
        assert failed.degraded is True
        assert result.final_chunks


def test_identical_rewritten_and_original_query_is_charged_once() -> None:
    adapter = FakeRetrievalAdapter()
    adapter.rewritten_query = "original query"
    context, _source = make_context()
    result = RetrievalExecutionService(adapter).execute(
        make_invocation(), run_context=context
    )
    assert result.status == RetrievalExecutionStatus.SUCCEEDED
    assert result.budget_usage.embedding_calls == 1
    assert result.budget_usage.vector_queries == 1
    assert result.budget_usage.keyword_queries == 1
    assert adapter.calls.count("embed") == 1
    assert adapter.calls.count("retrieve") == 1
    assert adapter.calls.count("keyword") == 1


def test_filter_skips_keyword_without_reserving_or_charging_budget() -> None:
    adapter = FakeRetrievalAdapter()
    context, _source = make_context()
    invocation = RetrievalInvocation.create(
        "original query",
        collection_names=("kb",),
        filters={"tenant": "allowed"},
        top_k=4,
        rerank_top_k=2,
        requested_timeout_seconds=2.0,
    )
    result = RetrievalExecutionService(adapter).execute(
        invocation, run_context=context
    )

    assert result.status == RetrievalExecutionStatus.SUCCEEDED
    assert result.budget_usage.keyword_queries == 0
    assert context.budget_ledger.snapshot().committed_usage.keyword_queries == 0
    assert "keyword" not in adapter.calls


def test_partial_document_failure_degrades_but_all_failed_is_fatal() -> None:
    partial = FakeRetrievalAdapter()
    partial.failed_load_ids.add("chunk-1")
    context, _source = make_context()
    partial_result = RetrievalExecutionService(partial).execute(
        make_invocation(), run_context=context
    )
    assert partial_result.status == RetrievalExecutionStatus.DEGRADED
    assert partial_result.degradation_reasons == (
        "DOCUMENT_LOAD_PARTIAL_FAILED:1",
    )
    assert len(partial_result.final_chunks) == 1

    failed = FakeRetrievalAdapter()
    failed.failed_load_ids.update({"chunk-0", "chunk-1"})
    context, _source = make_context()
    failed_result = RetrievalExecutionService(failed).execute(
        make_invocation(), run_context=context
    )
    assert failed_result.status == RetrievalExecutionStatus.FAILED
    assert failed_result.error.category == RetrievalErrorCategory.DOCUMENT_LOAD_FAILED


def test_budget_cancellation_and_timeout_have_distinct_statuses() -> None:
    budget_adapter = FakeRetrievalAdapter()
    context, _source = make_context(max_embedding_calls=0)
    budget_result = RetrievalExecutionService(budget_adapter).execute(
        make_invocation(), run_context=context
    )
    assert budget_result.status == RetrievalExecutionStatus.FAILED
    assert budget_result.error.category == RetrievalErrorCategory.BUDGET_EXHAUSTED
    assert "embed" not in budget_adapter.calls

    keyword_adapter = FakeRetrievalAdapter()
    context, _source = make_context(max_keyword_queries=0)
    keyword_budget = RetrievalExecutionService(keyword_adapter).execute(
        make_invocation(), run_context=context
    )
    assert keyword_budget.status == RetrievalExecutionStatus.FAILED
    assert keyword_budget.error.category == RetrievalErrorCategory.BUDGET_EXHAUSTED
    assert keyword_budget.budget_usage.embedding_calls == 2
    assert keyword_budget.budget_usage.vector_queries == 2
    assert keyword_budget.budget_usage.keyword_queries == 0
    assert "keyword" not in keyword_adapter.calls

    context_adapter = FakeRetrievalAdapter()
    context, _source = make_context(max_context_chars=1)
    context_budget = RetrievalExecutionService(context_adapter).execute(
        make_invocation(), run_context=context
    )
    assert context_budget.status == RetrievalExecutionStatus.FAILED
    assert context_budget.error.category == RetrievalErrorCategory.BUDGET_EXHAUSTED
    assert context_budget.final_chunks == ()
    assert context.budget_ledger.snapshot().committed_usage.context_chars == 0

    cancel_adapter = FakeRetrievalAdapter()
    context, source = make_context()
    source.cancel(CancellationReason.USER_CANCELLED)
    cancelled = RetrievalExecutionService(cancel_adapter).execute(
        make_invocation(), run_context=context
    )
    assert cancelled.status == RetrievalExecutionStatus.CANCELLED
    assert cancel_adapter.calls == []

    timeout_adapter = FakeRetrievalAdapter()
    timeout_adapter.sleep_rewrite_seconds = 0.1
    timeouts = dict(RetrievalExecutionSpec().stage_timeouts)
    timeouts[RetrievalStage.QUERY_REWRITE] = 0.01
    service = RetrievalExecutionService(
        timeout_adapter,
        spec=RetrievalExecutionSpec(stage_timeouts=timeouts),
    )
    context, _source = make_context()
    timed_out = service.execute(make_invocation(), run_context=context)
    assert timed_out.status == RetrievalExecutionStatus.TIMED_OUT
    assert timed_out.error.category == RetrievalErrorCategory.TIMEOUT
    assert "embed" not in timeout_adapter.calls
    timeout_record = timed_out.stage_records[0]
    assert timeout_record.worker_terminated is False
    assert timeout_record.execution_detached is True
    assert timeout_record.background_work_pending is True
    assert timed_out.background_work_pending is True
    assert service.blocking_executor.wait_until_idle(1.0)


def test_bounded_executor_admission_queue_cancellation_and_idle_tracking() -> None:
    executor = BoundedBlockingExecutor(max_workers=1, max_pending_tasks=1)
    first_started = threading.Event()
    release_first = threading.Event()
    queued_ran = threading.Event()
    source = CancellationSource()

    first = executor.submit(
        lambda: (first_started.set(), release_first.wait(1.0)),
        kind=BlockingTaskKind.EMBEDDING,
        run_id="run-safe",
        operation_id="embedding-1",
        cancellation_check=lambda: None,
        remaining_seconds=lambda: 2.0,
    )
    assert first_started.wait(1.0)
    queued = executor.submit(
        lambda: queued_ran.set(),
        kind=BlockingTaskKind.VECTOR_QUERY,
        run_id="run-safe",
        operation_id="vector-1",
        cancellation_check=lambda: None,
        remaining_seconds=lambda: 2.0,
    )
    assert executor.snapshot().admitted_count == 2
    assert executor.snapshot().pending_count == 1
    assert executor.wait_until_idle(0.01) is False
    assert RunRegistry().snapshot() == {}
    assert executor.snapshot().active_count == 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as waiter:
        third = waiter.submit(
            executor.submit,
            lambda: None,
            kind=BlockingTaskKind.KEYWORD_QUERY,
            run_id="run-safe",
            operation_id="keyword-1",
            cancellation_check=source.token.raise_if_cancelled,
            remaining_seconds=lambda: 2.0,
        )
        time.sleep(0.05)
        assert third.done() is False
        source.cancel(CancellationReason.USER_CANCELLED)
        with pytest.raises(RunCancelledError):
            third.result(timeout=1.0)

    queued_state = queued.cancel_or_detach()
    assert queued_state.worker_terminated is True
    assert queued_state.execution_detached is False
    release_first.set()
    first.result(timeout=1.0)
    assert executor.wait_until_idle(1.0)
    assert queued_ran.is_set() is False
    assert executor.snapshot().admitted_count == 0
    assert executor.shutdown(wait=True, timeout=1.0)


def test_running_worker_detaches_until_true_completion_and_shutdown_waits() -> None:
    executor = BoundedBlockingExecutor(max_workers=1, max_pending_tasks=0)
    started = threading.Event()
    release = threading.Event()
    handle = executor.submit(
        lambda: (started.set(), release.wait(1.0)),
        kind=BlockingTaskKind.QUERY_REWRITE,
        run_id="run-safe",
        operation_id="rewrite-1",
        cancellation_check=lambda: None,
        remaining_seconds=lambda: 1.0,
    )
    assert started.wait(1.0)
    state = handle.cancel_or_detach()
    assert state.worker_terminated is False
    assert state.execution_detached is True
    assert state.background_work_pending is True
    assert executor.snapshot().detached_count == 1
    assert executor.wait_until_idle(0.01) is False

    timer = threading.Timer(0.05, release.set)
    timer.start()
    try:
        assert executor.shutdown(wait=True, timeout=1.0)
    finally:
        timer.cancel()
    assert executor.snapshot().detached_count == 0


def test_owner_worker_nested_submission_fails_fast_without_deadlock() -> None:
    executor = BoundedBlockingExecutor(
        max_workers=1,
        max_pending_tasks=1,
        thread_name_prefix="day18-nested-guard",
    )

    def submit_nested():
        return executor.submit(
            lambda: None,
            kind=BlockingTaskKind.VECTOR_QUERY,
            run_id="run-safe",
            operation_id="nested-vector",
            cancellation_check=lambda: None,
            remaining_seconds=lambda: 1.0,
        )

    outer = executor.submit(
        submit_nested,
        kind=BlockingTaskKind.EMBEDDING,
        run_id="run-safe",
        operation_id="outer-embedding",
        cancellation_check=lambda: None,
        remaining_seconds=lambda: 1.0,
    )
    with pytest.raises(
        BlockingExecutorNestedSubmissionError,
        match="BLOCKING_EXECUTOR_NESTED_SUBMISSION",
    ):
        outer.result(timeout=0.5)
    assert executor.wait_until_idle(0.5)
    assert executor.shutdown(wait=True, timeout=0.5)


def test_queued_provider_call_cancel_releases_budget_and_never_executes() -> None:
    executor = BoundedBlockingExecutor(max_workers=1, max_pending_tasks=1)
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    blocker = executor.submit(
        lambda: (blocker_started.set(), release_blocker.wait(1.0)),
        kind=BlockingTaskKind.CONTEXT_BUILD,
        run_id="other-run",
        operation_id="blocker",
        cancellation_check=lambda: None,
        remaining_seconds=lambda: 2.0,
    )
    assert blocker_started.wait(1.0)
    adapter = FakeRetrievalAdapter()
    adapter.query_rewrite_strategy = QueryRewriteStrategy.NONE
    context, source = make_context()
    service = RetrievalExecutionService(
        adapter, blocking_executor=executor
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as runner:
        pending_result = runner.submit(
            service.execute, make_invocation(), run_context=context
        )
        deadline = time.monotonic() + 1.0
        while executor.snapshot().pending_count != 1:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        source.cancel(CancellationReason.USER_CANCELLED)
        result = pending_result.result(timeout=1.0)
    release_blocker.set()
    blocker.result(timeout=1.0)
    assert result.status == RetrievalExecutionStatus.CANCELLED
    assert context.budget_ledger.snapshot().committed_usage.embedding_calls == 0
    assert "embed" not in adapter.calls
    assert executor.wait_until_idle(1.0)
    assert executor.shutdown(wait=True, timeout=1.0)


def test_queued_provider_call_deadline_releases_budget_and_never_executes() -> None:
    executor = BoundedBlockingExecutor(max_workers=1, max_pending_tasks=1)
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    blocker = executor.submit(
        lambda: (blocker_started.set(), release_blocker.wait(1.0)),
        kind=BlockingTaskKind.CONTEXT_BUILD,
        run_id="other-run",
        operation_id="blocker",
        cancellation_check=lambda: None,
        remaining_seconds=lambda: 2.0,
    )
    assert blocker_started.wait(0.5)
    adapter = FakeRetrievalAdapter()
    adapter.query_rewrite_strategy = QueryRewriteStrategy.NONE
    context, _source = create_run_context(
        entry_agent_id="knowledge_expert",
        timeout_seconds=0.08,
    )
    context.attach_budget_ledger(
        BudgetLedger(
            RunBudget(),
            deadline_remaining=context.remaining_seconds,
        )
    )
    service = RetrievalExecutionService(adapter, blocking_executor=executor)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as runner:
            pending_result = runner.submit(
                service.execute, make_invocation(), run_context=context
            )
            deadline = time.monotonic() + 0.5
            while executor.snapshot().pending_count != 1:
                assert time.monotonic() < deadline
                time.sleep(0.005)
            result = pending_result.result(timeout=0.5)
    finally:
        release_blocker.set()
        blocker.result(timeout=0.5)

    assert result.status == RetrievalExecutionStatus.TIMED_OUT
    assert context.budget_ledger.snapshot().committed_usage.embedding_calls == 0
    assert "embed" not in adapter.calls
    assert executor.wait_until_idle(0.5)
    assert executor.shutdown(wait=True, timeout=0.5)
