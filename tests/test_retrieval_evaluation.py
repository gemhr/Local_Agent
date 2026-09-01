from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import uuid
from dataclasses import fields, replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import server
from core.runtime import (
    BlockingTaskKind,
    BoundedBlockingExecutor,
    BudgetLedger,
    CancellationReason,
    ChatRuntimeMode,
    CitationBinding,
    ContextTrustLevel,
    RetrievalCandidate,
    RetrievalExecutionResult,
    RetrievalExecutionService,
    RetrievalExecutionSpec,
    RetrievalExecutionStatus,
    RetrievalInvocation,
    RetrievalProvenance,
    RetrievalStage,
    RetrievalTransformation,
    RetrievedChunk,
    RunBudget,
    RunStatus,
    SourceMetadata,
    StopReason,
    content_digest,
    create_run_context,
)
from core.runtime.retrieval_contract import RetrievalBudgetUsage
from core.runtime.retrieval_evaluation import (
    MAX_ARTIFACTS_PER_RUN,
    MAX_SCALAR_CHARS,
    RetrievalEvaluationCaptureBuilder,
    RetrievalEvaluationCaptureError,
    RetrievalEvaluationCaptureStatus,
    RetrievalEvaluationChannel,
    RetrievalEvaluationCollector,
    current_retrieval_evaluation_collector,
    install_retrieval_evaluation_collector,
    reset_retrieval_evaluation_collector,
)
from tests.test_retrieval_execution import (
    FakeRetrievalAdapter,
    make_context,
    make_invocation,
)
from tests.test_runtime_execute_endpoint import _result

RETRIEVAL_RESULT_FIELDS = (
    "retrieval_id",
    "status",
    "rewritten_query_digest",
    "final_chunks",
    "citations",
    "stage_records",
    "degraded",
    "degradation_reasons",
    "budget_usage",
    "started_at",
    "completed_at",
    "duration_ms",
    "error",
)


class ProvenanceAdapter(FakeRetrievalAdapter):
    def retrieve(self, query, embedding, invocation, *, max_candidates):
        candidates = super().retrieve(query, embedding, invocation, max_candidates=2)
        shared = replace(candidates[0], candidate_id="shared", score=0.50)
        if query == self.rewritten_query:
            vector_only = replace(candidates[1], candidate_id="vector-055", score=0.55)
            return [shared, vector_only]
        return [replace(shared, score=0.45)]

    def keyword_retrieve(self, terms, invocation, *, max_candidates):
        candidate = super().retrieve(
            self.rewritten_query, None, invocation, max_candidates=1
        )[0]
        return [replace(candidate, candidate_id="shared", score=0.55)]


def _run_with_collector(adapter=None, *, invocation=None):
    context, _source = make_context()
    collector = RetrievalEvaluationCollector(context.run_id)
    token = install_retrieval_evaluation_collector(collector)
    try:
        result = RetrievalExecutionService(adapter or FakeRetrievalAdapter()).execute(
            invocation or make_invocation(), run_context=context
        )
    finally:
        reset_retrieval_evaluation_collector(token)
    return result, collector


def test_retrieval_execution_result_frozen_field_signature_is_exact() -> None:
    assert (
        tuple(field.name for field in fields(RetrievalExecutionResult))
        == RETRIEVAL_RESULT_FIELDS
    )


def test_same_execution_captures_queries_channels_winner_scores_and_selection() -> None:
    result, collector = _run_with_collector(ProvenanceAdapter())

    status, error_code, snapshots = collector.envelope()
    assert result.status is RetrievalExecutionStatus.SUCCEEDED
    assert status is RetrievalEvaluationCaptureStatus.COMPLETE
    assert error_code is None
    snapshot = snapshots[0]
    assert snapshot.query == "original query"
    assert snapshot.rewritten_query == "rewritten query"
    shared = next(
        item for item in snapshot.retrieved_items if item.chunk_id == "chunk-0"
    )
    assert shared.retrieval_score == 0.55
    assert shared.retrieval_score_kind == "KEYWORD_FIXED_HEURISTIC"
    assert shared.retrieval_channels == (
        "VECTOR_REWRITTEN_QUERY",
        "VECTOR_ORIGINAL_QUERY",
        "KEYWORD",
    )
    vector_only = next(
        item for item in snapshot.retrieved_items if item.chunk_id == "chunk-1"
    )
    assert vector_only.retrieval_score == 0.55
    assert vector_only.retrieval_score_kind == "VECTOR_NORMALIZED_RELEVANCE"
    assert [item.chunk_id for item in snapshot.selected_items] == [
        chunk.source.chunk_id for chunk in result.final_chunks
    ]
    assert [item.citation_id for item in snapshot.citations] == [
        item.citation_id for item in result.citations
    ]
    assert snapshot.retrieval_latency_ms is not None
    assert snapshot.rerank_latency_ms is not None
    wire = snapshot.to_wire_dict()
    json.dumps(wire, ensure_ascii=False, allow_nan=False)
    assert "text" not in wire["retrieved_items"][0]
    assert "canonical_uri" not in wire["retrieved_items"][0]["source"]
    assert wire["selected_items"][0]["text"] == result.final_chunks[0].text


def test_projection_failure_does_not_change_retrieval_result() -> None:
    invocation = RetrievalInvocation.create(
        "q" * 32_769,
        collection_names=("kb",),
        top_k=4,
        rerank_top_k=2,
        requested_timeout_seconds=2.0,
        retrieval_id="oversize-query",
    )
    result, collector = _run_with_collector(
        FakeRetrievalAdapter(), invocation=invocation
    )

    status, error_code, snapshots = collector.envelope()
    assert result.status is RetrievalExecutionStatus.SUCCEEDED
    assert result.final_chunks
    assert status is RetrievalEvaluationCaptureStatus.FAILED
    assert error_code == "RAG_EVALUATION_QUERY_LIMIT_EXCEEDED"
    assert snapshots == ()


def test_terminal_and_degraded_results_still_produce_truthful_snapshots() -> None:
    cases = []
    degraded = FakeRetrievalAdapter()
    degraded.rewrite_failure = True
    cases.append((degraded, RetrievalExecutionStatus.DEGRADED))
    failed = FakeRetrievalAdapter()
    failed.vector_failure = True
    cases.append((failed, RetrievalExecutionStatus.FAILED))
    cases.append(
        (FakeRetrievalAdapter(document_count=0), RetrievalExecutionStatus.EMPTY)
    )

    for adapter, expected_status in cases:
        result, collector = _run_with_collector(adapter)
        capture_status, error_code, snapshots = collector.envelope()
        assert result.status is expected_status
        assert capture_status is RetrievalEvaluationCaptureStatus.COMPLETE
        assert error_code is None
        assert snapshots[0].retrieval_status == expected_status.value
        if expected_status is RetrievalExecutionStatus.FAILED:
            assert snapshots[0].error is not None
        if expected_status is RetrievalExecutionStatus.EMPTY:
            assert snapshots[0].selected_items == snapshots[0].citations == ()


def test_collector_start_order_duplicate_and_reverse_completion() -> None:
    run_id = uuid.uuid4().hex
    collector = RetrievalEvaluationCollector(run_id)
    invocations = [
        RetrievalInvocation.create(
            f"query-{index}",
            collection_names=("kb",),
            top_k=1,
            requested_timeout_seconds=2.0,
            retrieval_id=f"retrieval-{index}",
        )
        for index in (1, 2)
    ]
    builders = [
        collector.begin(
            run_id=run_id,
            invocation=invocation,
            max_context_chars=32_768,
        )
        for invocation in invocations
    ]
    assert all(builder is not None for builder in builders)
    duplicate = collector.begin(
        run_id=run_id,
        invocation=invocations[0],
        max_context_chars=32_768,
    )
    assert duplicate is None

    results = []
    for invocation in invocations:
        context, _source = make_context()
        empty_adapter = FakeRetrievalAdapter(document_count=0)
        results.append(
            RetrievalExecutionService(empty_adapter).execute(
                invocation, run_context=context
            )
        )
    release_first = threading.Event()

    def complete_first() -> None:
        release_first.wait(timeout=2.0)
        collector.complete(builders[0], results[0])

    def complete_second() -> None:
        collector.complete(builders[1], results[1])
        release_first.set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(complete_first), executor.submit(complete_second)]
        for future in futures:
            future.result(timeout=2.0)

    status, error_code, snapshots = collector.envelope()
    assert status is RetrievalEvaluationCaptureStatus.PARTIAL
    assert error_code == "RAG_EVALUATION_DUPLICATE_RETRIEVAL_ID"
    assert [item.retrieval_id for item in snapshots] == [
        "retrieval-1",
        "retrieval-2",
    ]
    assert [item.invocation_index for item in snapshots] == [1, 2]


@pytest.mark.asyncio
async def test_contextvar_propagates_to_thread_and_resets_without_leakage() -> None:
    collector = RetrievalEvaluationCollector(uuid.uuid4().hex)
    token = install_retrieval_evaluation_collector(collector)
    executor = BoundedBlockingExecutor(max_workers=1, max_pending_tasks=0)
    try:
        assert (
            await asyncio.to_thread(current_retrieval_evaluation_collector) is collector
        )
        handle = executor.submit_nowait(
            current_retrieval_evaluation_collector,
            kind=BlockingTaskKind.VECTOR_QUERY,
            run_id=collector.run_id,
            operation_id="context-propagation",
            cancellation_check=lambda: None,
        )
        assert handle.result(timeout=2.0) is collector
    finally:
        executor.shutdown(wait=True, timeout=2.0)
        reset_retrieval_evaluation_collector(token)
    assert current_retrieval_evaluation_collector() is None

    async def request_scope() -> RetrievalEvaluationCollector:
        scoped = RetrievalEvaluationCollector(uuid.uuid4().hex)
        scoped_token = install_retrieval_evaluation_collector(scoped)
        try:
            await asyncio.sleep(0)
            assert current_retrieval_evaluation_collector() is scoped
            return scoped
        finally:
            reset_retrieval_evaluation_collector(scoped_token)

    request_a, request_b = await asyncio.gather(request_scope(), request_scope())
    assert request_a is not request_b
    assert current_retrieval_evaluation_collector() is None


class _EvaluationService:
    admission_gate = SimpleNamespace(accepts_new_runs=True)

    def __init__(self, result, *, fail_capture: bool) -> None:
        self.result = result
        self.fail_capture = fail_capture
        self.seen_collector = None

    def selected_runtime_mode(self):
        return ChatRuntimeMode.COORDINATED

    async def run_coordinated_agent(self, **kwargs):
        collector = current_retrieval_evaluation_collector()
        self.seen_collector = collector
        if self.fail_capture:
            assert collector is not None
            collector.record_failure("RAG_EVALUATION_TEST_PROJECTION_FAILED")
        return None, self.result


@pytest.mark.asyncio
async def test_evaluation_endpoint_keeps_runtime_terminal_independent_and_resets(
    monkeypatch,
) -> None:
    run_id = uuid.uuid4().hex
    result = _result(
        RunStatus.SUCCEEDED,
        StopReason.COMPLETED,
        run_id=run_id,
    )
    monkeypatch.setattr(
        server, "chat_service", _EvaluationService(result, fail_capture=True)
    )
    response = await server.runtime_evaluation_execute_endpoint(
        server.RuntimeExecuteRequest(
            agent_id="core_router",
            query="test",
            run_id=run_id,
            timeout_seconds=30.0,
        )
    )
    body = json.loads(response.body)
    assert body["status"] == "SUCCEEDED"
    assert body["capture_status"] == "FAILED"
    assert body["capture_error_code"] == "RAG_EVALUATION_TEST_PROJECTION_FAILED"
    assert body["rag_evaluation_artifacts"] == []
    assert current_retrieval_evaluation_collector() is None


@pytest.mark.asyncio
async def test_evaluation_endpoint_without_retrieval_is_complete(monkeypatch) -> None:
    run_id = uuid.uuid4().hex
    result = _result(RunStatus.SUCCEEDED, StopReason.COMPLETED, run_id=run_id)
    service = _EvaluationService(result, fail_capture=False)
    monkeypatch.setattr(server, "chat_service", service)
    response = await server.runtime_evaluation_execute_endpoint(
        server.RuntimeExecuteRequest(
            agent_id="core_router",
            query="test",
            run_id=run_id,
            timeout_seconds=30.0,
        )
    )
    body = json.loads(response.body)
    assert service.seen_collector is not None
    assert body["capture_status"] == "COMPLETE"
    assert body["capture_error_code"] is None
    assert body["rag_evaluation_artifacts"] == []


@pytest.mark.asyncio
async def test_wp1_endpoint_remains_exact_five_fields_and_has_no_collector(
    monkeypatch,
) -> None:
    run_id = uuid.uuid4().hex
    result = _result(
        RunStatus.FAILED,
        StopReason.UNHANDLED_ERROR,
        run_id=run_id,
        error_code="TEST_FAILURE",
    )
    service = _EvaluationService(result, fail_capture=False)
    monkeypatch.setattr(server, "chat_service", service)
    response = await server.runtime_execute_endpoint(
        server.RuntimeExecuteRequest(
            agent_id="core_router",
            query="test",
            run_id=run_id,
            timeout_seconds=30.0,
        )
    )
    assert set(json.loads(response.body)) == {
        "run_id",
        "status",
        "stop_reason",
        "error_code",
        "safe_message",
    }
    assert service.seen_collector is None


# ---------------------------------------------------------------------------
# Verification Group C — TIMED_OUT / CANCELLED dynamic artifacts
# ---------------------------------------------------------------------------


def _candidate(
    chunk_id: str,
    score: float,
    original_rank: int,
    *,
    candidate_id: str | None = None,
    display_name: str = "x.md",
    page: int = 1,
    section: str = "S",
    sheet: str | None = None,
    content_hash: str | None = "abc",
    metadata: dict | None = None,
    text: str | None = None,
) -> RetrievalCandidate:
    source = SourceMetadata(
        source_id="source-stable",
        source_type="md",
        collection="kb",
        canonical_uri="docs/x.md",
        display_name=display_name,
        document_version="v1",
        page=page,
        section_path=section,
        chunk_id=chunk_id,
        chunk_index=0,
    )
    meta = {"chunk_id": chunk_id}
    if content_hash is not None:
        meta["content_hash"] = content_hash
    if sheet is not None:
        meta["sheet_name"] = sheet
    if metadata:
        meta.update(metadata)
    return RetrievalCandidate(
        candidate_id=candidate_id or chunk_id,
        source=source,
        score=score,
        original_rank=original_rank,
        metadata=meta,
        content_locator=f"chroma:kb:{chunk_id}",
        text=text if text is not None else f"doc {chunk_id} text",
    )


def _chunk(
    chunk_id: str, *, text: str = "final context", page: int = 3
) -> RetrievedChunk:
    source = SourceMetadata(
        source_id="source-stable",
        source_type="md",
        collection="kb",
        canonical_uri="docs/x.md",
        display_name="x.md",
        document_version="v1",
        page=page,
        section_path="S",
        chunk_id=chunk_id,
        chunk_index=0,
    )
    context_hash = content_digest(text)
    citation = CitationBinding(
        citation_id=f"Rtest-{chunk_id}",
        source_id=source.source_id,
        chunk_id=source.chunk_id,
        context_block_id="context-1",
        context_content_hash=context_hash,
        display_label="x.md (p.3)",
        page=page,
        section_path="S",
    )
    provenance = RetrievalProvenance(
        source_id=source.source_id,
        chunk_id=source.chunk_id,
        original_rank=1,
        reranked_rank=1,
        retrieval_score=0.9,
        transformations=(
            RetrievalTransformation.LOADED,
            RetrievalTransformation.CONTEXT_SELECTED,
        ),
        original_content_hash=context_hash,
        context_content_hash=context_hash,
    )
    return RetrievedChunk(
        "context-1",
        text,
        source,
        provenance,
        citation,
        ContextTrustLevel.UNTRUSTED_EXTERNAL,
        0.9,
    )


def _synthetic_result(
    invocation,
    chunks,
    *,
    status=RetrievalExecutionStatus.SUCCEEDED,
    degraded=False,
    reasons=(),
    error=None,
    duration_ms=5,
) -> RetrievalExecutionResult:
    now = datetime.now(UTC)
    return RetrievalExecutionResult(
        retrieval_id=invocation.retrieval_id,
        status=status,
        rewritten_query_digest=content_digest(invocation.original_query),
        final_chunks=chunks,
        citations=tuple(item.citation for item in chunks),
        stage_records=(),
        degraded=degraded,
        degradation_reasons=reasons,
        budget_usage=RetrievalBudgetUsage(),
        started_at=now,
        completed_at=now,
        duration_ms=duration_ms,
        error=error,
    )


def _context_for_run(run_id: str):
    context, _ = create_run_context(entry_agent_id="knowledge_expert", run_id=run_id)
    context.attach_budget_ledger(
        BudgetLedger(RunBudget(), deadline_remaining=context.remaining_seconds)
    )
    return context


def _complete_builder(
    collector, run_id: str, retrieval_id: str, *, query: str = "q"
) -> None:
    invocation = RetrievalInvocation.create(
        query,
        collection_names=("kb",),
        top_k=1,
        rerank_top_k=1,
        requested_timeout_seconds=2.0,
        retrieval_id=retrieval_id,
    )
    builder = collector.begin(
        run_id=run_id, invocation=invocation, max_context_chars=32_768
    )
    assert builder is not None
    candidate = _candidate("c0", 0.9, 1)
    builder.observe_candidates(
        [candidate], RetrievalEvaluationChannel.VECTOR_REWRITTEN_QUERY
    )
    builder.capture_retrieved([candidate])
    builder.capture_ranked([candidate], reranked=False)
    collector.complete(builder, _synthetic_result(invocation, (_chunk("c0"),)))


def test_timed_out_retrieval_produces_truthful_artifact() -> None:
    adapter = FakeRetrievalAdapter()
    adapter.sleep_rewrite_seconds = 0.1
    timeouts = dict(RetrievalExecutionSpec().stage_timeouts)
    timeouts[RetrievalStage.QUERY_REWRITE] = 0.01
    executor = BoundedBlockingExecutor(max_workers=1, max_pending_tasks=0)
    service = RetrievalExecutionService(
        adapter,
        spec=RetrievalExecutionSpec(stage_timeouts=timeouts),
        blocking_executor=executor,
    )
    context, _source = make_context()
    collector = RetrievalEvaluationCollector(context.run_id)
    token = install_retrieval_evaluation_collector(collector)
    try:
        result = service.execute(make_invocation(), run_context=context)
    finally:
        reset_retrieval_evaluation_collector(token)
        executor.shutdown(wait=True, timeout=2.0)

    assert result.status is RetrievalExecutionStatus.TIMED_OUT
    capture_status, error_code, snapshots = collector.envelope()
    assert capture_status is RetrievalEvaluationCaptureStatus.COMPLETE
    assert error_code is None
    snapshot = snapshots[0]
    assert snapshot.retrieval_status == "TIMED_OUT"
    assert snapshot.error is not None
    assert snapshot.error.category == "TIMEOUT"
    assert snapshot.query == "original query"
    # rewrite never completed -> effective rewritten query stays original
    assert snapshot.rewritten_query == "original query"
    assert snapshot.retrieved_items == ()
    assert snapshot.ranked_items == ()
    assert snapshot.selected_items == snapshot.citations == ()


def test_cancelled_retrieval_produces_truthful_artifact() -> None:
    adapter = FakeRetrievalAdapter()
    context, source = make_context()
    source.cancel(CancellationReason.USER_CANCELLED)
    collector = RetrievalEvaluationCollector(context.run_id)
    token = install_retrieval_evaluation_collector(collector)
    try:
        result = RetrievalExecutionService(adapter).execute(
            make_invocation(), run_context=context
        )
    finally:
        reset_retrieval_evaluation_collector(token)

    assert result.status is RetrievalExecutionStatus.CANCELLED
    capture_status, _error_code, snapshots = collector.envelope()
    assert capture_status is RetrievalEvaluationCaptureStatus.COMPLETE
    assert snapshots[0].retrieval_status == "CANCELLED"
    assert snapshots[0].error is not None
    assert snapshots[0].error.category == "CANCELLED"


# ---------------------------------------------------------------------------
# Verification Group D — multi-retrieval N + cross-run isolation
# ---------------------------------------------------------------------------


def test_two_retrievals_in_one_run_are_ordered_by_invocation_index() -> None:
    run_id = uuid.uuid4().hex
    collector = RetrievalEvaluationCollector(run_id)
    token = install_retrieval_evaluation_collector(collector)
    try:
        for index, retrieval_id in enumerate(("r-a", "r-b"), start=1):
            context = _context_for_run(run_id)
            result = RetrievalExecutionService(
                FakeRetrievalAdapter(document_count=2)
            ).execute(
                RetrievalInvocation.create(
                    f"query-{index}",
                    collection_names=("kb",),
                    top_k=2,
                    rerank_top_k=2,
                    requested_timeout_seconds=2.0,
                    retrieval_id=retrieval_id,
                ),
                run_context=context,
            )
            assert result.status is RetrievalExecutionStatus.SUCCEEDED
    finally:
        reset_retrieval_evaluation_collector(token)

    capture_status, error_code, snapshots = collector.envelope()
    assert capture_status is RetrievalEvaluationCaptureStatus.COMPLETE
    assert error_code is None
    assert [item.invocation_index for item in snapshots] == [1, 2]
    assert [item.retrieval_id for item in snapshots] == ["r-a", "r-b"]


def test_cross_run_isolation_no_artifact_leakage() -> None:
    run_a, run_b = uuid.uuid4().hex, uuid.uuid4().hex
    col_a = RetrievalEvaluationCollector(run_a)
    token_a = install_retrieval_evaluation_collector(col_a)
    try:
        result_a = RetrievalExecutionService(
            FakeRetrievalAdapter(document_count=2)
        ).execute(
            RetrievalInvocation.create(
                "query-a",
                collection_names=("kb",),
                top_k=2,
                rerank_top_k=2,
                requested_timeout_seconds=2.0,
                retrieval_id="rA",
            ),
            run_context=_context_for_run(run_a),
        )
        assert result_a.status is RetrievalExecutionStatus.SUCCEEDED
    finally:
        reset_retrieval_evaluation_collector(token_a)

    col_b = RetrievalEvaluationCollector(run_b)
    token_b = install_retrieval_evaluation_collector(col_b)
    try:
        result_b = RetrievalExecutionService(
            FakeRetrievalAdapter(document_count=2)
        ).execute(
            RetrievalInvocation.create(
                "query-b",
                collection_names=("kb",),
                top_k=2,
                rerank_top_k=2,
                requested_timeout_seconds=2.0,
                retrieval_id="rB",
            ),
            run_context=_context_for_run(run_b),
        )
        assert result_b.status is RetrievalExecutionStatus.SUCCEEDED
    finally:
        reset_retrieval_evaluation_collector(token_b)

    assert [item.retrieval_id for item in col_a.envelope()[2]] == ["rA"]
    assert [item.retrieval_id for item in col_b.envelope()[2]] == ["rB"]
    assert current_retrieval_evaluation_collector() is None


# ---------------------------------------------------------------------------
# Verification Group F — wire bounds fail closed (no silent truncation)
# ---------------------------------------------------------------------------


def test_retrieved_and_ranked_oversize_fail_closed_via_real_pipeline() -> None:
    class ManyAdapter(FakeRetrievalAdapter):
        def __init__(self) -> None:
            super().__init__(document_count=0)

        def retrieve(self, query, embedding, invocation, *, max_candidates):
            return [_candidate(f"chunk-{i}", 0.9, i + 1, text=f"doc {i} text") for i in range(70)]

    context, _source = make_context()
    collector = RetrievalEvaluationCollector(context.run_id)
    token = install_retrieval_evaluation_collector(collector)
    try:
        service = RetrievalExecutionService(
            ManyAdapter(),
            spec=RetrievalExecutionSpec(
                max_candidates=70,
                max_context_chars=32_768,
                max_single_chunk_chars=32_768,
            ),
        )
        result = service.execute(make_invocation(), run_context=context)
    finally:
        reset_retrieval_evaluation_collector(token)

    # Runtime itself still succeeds; only the evaluation capture is incomplete.
    assert result.status is RetrievalExecutionStatus.SUCCEEDED
    capture_status, error_code, snapshots = collector.envelope()
    assert capture_status is not RetrievalEvaluationCaptureStatus.COMPLETE
    assert error_code == "RAG_EVALUATION_RETRIEVED_CAPTURE_FAILED"
    assert snapshots == ()


def test_selected_item_and_text_limits_fail_closed() -> None:
    invocation = RetrievalInvocation.create(
        "q",
        collection_names=("kb",),
        top_k=20,
        rerank_top_k=20,
        requested_timeout_seconds=2.0,
        retrieval_id="item-limit",
    )
    builder = RetrievalEvaluationCaptureBuilder(
        run_id="r", invocation_index=1, invocation=invocation, max_context_chars=32_768
    )
    with pytest.raises(RetrievalEvaluationCaptureError) as exc_info:
        builder.finalize(
            _synthetic_result(
                invocation, tuple(_chunk(f"c{i}") for i in range(17))
            )
        )
    assert exc_info.value.code == "RAG_EVALUATION_ITEM_LIMIT_EXCEEDED"

    invocation2 = RetrievalInvocation.create(
        "q",
        collection_names=("kb",),
        top_k=1,
        rerank_top_k=1,
        requested_timeout_seconds=2.0,
        retrieval_id="text-limit",
    )
    builder2 = RetrievalEvaluationCaptureBuilder(
        run_id="r", invocation_index=1, invocation=invocation2, max_context_chars=32_768
    )
    with pytest.raises(RetrievalEvaluationCaptureError) as exc_info2:
        builder2.finalize(
            _synthetic_result(invocation2, (_chunk("c0", text="t" * 40_000),))
        )
    assert exc_info2.value.code == "RAG_EVALUATION_SELECTED_TEXT_LIMIT_EXCEEDED"


def test_scalar_metadata_oversize_fails_closed() -> None:
    invocation = RetrievalInvocation.create(
        "q",
        collection_names=("kb",),
        top_k=1,
        rerank_top_k=1,
        requested_timeout_seconds=2.0,
        retrieval_id="scalar",
    )
    builder = RetrievalEvaluationCaptureBuilder(
        run_id="r", invocation_index=1, invocation=invocation, max_context_chars=32_768
    )
    candidate = _candidate("c0", 0.9, 1, display_name="x" * (MAX_SCALAR_CHARS + 1))
    builder.observe_candidates(
        [candidate], RetrievalEvaluationChannel.VECTOR_REWRITTEN_QUERY
    )
    builder.capture_retrieved([candidate])
    assert builder.capture_error_code == "RAG_EVALUATION_RETRIEVED_CAPTURE_FAILED"


def test_artifact_count_limit_fail_closed_partial() -> None:
    run_id = uuid.uuid4().hex
    collector = RetrievalEvaluationCollector(run_id)
    for index in range(MAX_ARTIFACTS_PER_RUN):
        _complete_builder(collector, run_id, f"r{index}", query="q")
    over = collector.begin(
        run_id=run_id,
        invocation=RetrievalInvocation.create(
            "q",
            collection_names=("kb",),
            top_k=1,
            rerank_top_k=1,
            requested_timeout_seconds=2.0,
            retrieval_id="over",
        ),
        max_context_chars=32_768,
    )
    assert over is None
    capture_status, error_code, snapshots = collector.envelope()
    assert capture_status is RetrievalEvaluationCaptureStatus.PARTIAL
    assert error_code == "RAG_EVALUATION_ARTIFACT_LIMIT_EXCEEDED"
    assert len(snapshots) == MAX_ARTIFACTS_PER_RUN


# ---------------------------------------------------------------------------
# Stage5-Phase6-WP1 artifact v2 schema/plumbing（producer 侧）
# ---------------------------------------------------------------------------


def test_v2_schema_constants_and_enums_exist() -> None:
    from core.runtime.retrieval_evaluation import (
        ARTIFACT_SCHEMA_VERSION_V2,
        RetrievalEvaluationChannel,
        RetrievalEvaluationScoreKind,
    )

    assert ARTIFACT_SCHEMA_VERSION_V2 == "rag-evaluation-artifact.v2"
    assert RetrievalEvaluationChannel.BM25.value == "BM25"
    assert RetrievalEvaluationChannel.RRF.value == "RRF"
    assert RetrievalEvaluationScoreKind.BM25_RAW_SCORE.value == "BM25_RAW_SCORE"
    assert RetrievalEvaluationScoreKind.RRF_SCORE.value == "RRF_SCORE"


def test_baseline_snapshot_carries_truthful_strategy() -> None:
    """BASELINE producer 必须诚实标记 retrieval_strategy=BASELINE，且不填充
    BM25/RRF 通道值（不伪造 Hybrid 执行事实）。"""
    invocation = RetrievalInvocation.create(
        "q",
        collection_names=("kb",),
        top_k=1,
        rerank_top_k=1,
        requested_timeout_seconds=2.0,
        retrieval_id="baseline",
    )
    builder = RetrievalEvaluationCaptureBuilder(
        run_id="r", invocation_index=1, invocation=invocation, max_context_chars=32_768
    )
    candidate = _candidate("c0", 0.9, 1)
    builder.observe_candidates(
        [candidate], RetrievalEvaluationChannel.VECTOR_REWRITTEN_QUERY
    )
    builder.capture_retrieved([candidate])
    builder.capture_ranked([candidate], reranked=True)
    result = _synthetic_result(invocation, (_chunk("c0"),))
    snapshot = builder.finalize(result)
    assert snapshot.retrieval_strategy == "BASELINE"
    assert snapshot.provenance_sha256 is None
    wire = snapshot.to_wire_dict()
    assert wire["retrieval_strategy"] == "BASELINE"
    assert "provenance_sha256" not in wire
    channels = set()
    for item in snapshot.retrieved_items:
        channels.update(item.retrieval_channels)
    assert "BM25" not in channels and "RRF" not in channels
