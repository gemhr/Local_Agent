from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

import server
from core.runtime import (
    BudgetLedger,
    RetrievalCandidate,
    RetrievalExecutionService,
    RetrievalExecutionSpec,
    RetrievalExecutionStatus,
    RetrievalInvocation,
    RunBudget,
    RunRegistry,
    RunStatus,
    SourceMetadata,
    StopReason,
    create_run_context,
)
from core.runtime.retrieval_evaluation import (
    current_retrieval_evaluation_collector,
)
from tests._runtime_assembly_fixtures import FakeRouter, make_services
from tests.test_runtime_execute_endpoint import _ExplodingFactory, _result


def _context_for_run(run_id: str):
    context, _ = create_run_context(entry_agent_id="knowledge_expert", run_id=run_id)
    context.attach_budget_ledger(
        BudgetLedger(RunBudget(), deadline_remaining=context.remaining_seconds)
    )
    return context


def _payload(*, run_id: str | None = None) -> server.RuntimeExecuteRequest:
    return server.RuntimeExecuteRequest(
        agent_id="core_router",
        query="test",
        run_id=run_id or uuid.uuid4().hex,
        timeout_seconds=30.0,
    )


class _EvalService:
    admission_gate = SimpleNamespace(accepts_new_runs=True)

    def __init__(self, result, output: str | None = None) -> None:
        self.result = result
        self.output = output
        self.seen_collector = None

    async def run_coordinated_agent(self, **kwargs):
        self.seen_collector = current_retrieval_evaluation_collector()
        return self.output, self.result


class _LargeTextAdapter:
    query_rewrite_strategy = "EXISTING_MODEL"
    has_explicit_embedding = True
    has_reranker = False

    def __init__(self, text_len: int) -> None:
        self.text = "t" * text_len
        self.rewritten_query = "w" * text_len

    def rewrite_query(self, query, *, run_context, event_emitter):
        return self.rewritten_query

    def embed_query(self, query):
        from core.runtime import QueryEmbedding

        return QueryEmbedding.create(query, [0.1, 0.2, 0.3], "fake")

    def retrieve(self, query, embedding, invocation, *, max_candidates):
        source = SourceMetadata(
            source_id="source-stable",
            source_type="md",
            collection="kb",
            canonical_uri="docs/big.md",
            display_name="big.md",
            document_version="v1",
            page=1,
            section_path="S",
            chunk_id="chunk-0",
            chunk_index=0,
        )
        return [
            RetrievalCandidate(
                candidate_id="chunk-0",
                source=source,
                score=0.9,
                original_rank=1,
                metadata={"chunk_id": "chunk-0"},
                content_locator="chroma:kb:chunk-0",
                text=self.text,
            )
        ]

    def keyword_retrieve(self, terms, invocation, *, max_candidates):
        return []

    def should_keyword_retrieve(self, terms, invocation):
        return False

    def materialize(self, candidate):
        from core.runtime import content_digest
        from core.runtime.retrieval_contract import MaterializedDocument

        return MaterializedDocument(
            candidate, candidate.text, content_digest(candidate.text)
        )


class _LargeEvalService:
    admission_gate = SimpleNamespace(accepts_new_runs=True)

    def __init__(self, run_id: str, count: int) -> None:
        self.run_id = run_id
        self.count = count

    async def run_coordinated_agent(self, **kwargs):
        run_id = kwargs["run_id"]
        service = RetrievalExecutionService(
            _LargeTextAdapter(32_768),
            spec=RetrievalExecutionSpec(
                max_candidates=4,
                max_context_chars=32_768,
                max_single_chunk_chars=32_768,
                max_context_chunks=1,
            ),
        )
        for index in range(self.count):
            invocation = RetrievalInvocation.create(
                "q" * 32_768,
                collection_names=("kb",),
                top_k=1,
                rerank_top_k=1,
                requested_timeout_seconds=5.0,
                retrieval_id=f"big-{index}",
            )
            result = service.execute(invocation, run_context=_context_for_run(run_id))
            assert result.status is RetrievalExecutionStatus.SUCCEEDED
        return None, _result(RunStatus.SUCCEEDED, StopReason.COMPLETED, run_id=run_id)


class _RagAndAnswerEvalService:
    admission_gate = SimpleNamespace(accepts_new_runs=True)

    async def run_coordinated_agent(self, **kwargs):
        run_id = kwargs["run_id"]
        invocation = RetrievalInvocation.create(
            "query",
            collection_names=("kb",),
            top_k=1,
            rerank_top_k=1,
            requested_timeout_seconds=5.0,
            retrieval_id="rag-and-answer",
        )
        result = RetrievalExecutionService(_LargeTextAdapter(20)).execute(
            invocation,
            run_context=_context_for_run(run_id),
        )
        assert result.status is RetrievalExecutionStatus.SUCCEEDED
        return "answer-v2", _result(
            RunStatus.SUCCEEDED,
            StopReason.COMPLETED,
            run_id=run_id,
        )


async def _execute(payload: server.RuntimeExecuteRequest):
    return await server.runtime_evaluation_execute_v2_endpoint(payload, _test_request())


async def _execute_v2(payload: server.RuntimeExecuteRequest):
    return await server.runtime_evaluation_execute_v2_endpoint(payload, _test_request())


def _test_request() -> Request:
    """为 direct-call endpoint 单测提供现行 Request 参数；认证由 HTTP middleware 覆盖。"""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/runtime/evaluation-execute/v2",
            "raw_path": b"/api/runtime/evaluation-execute/v2",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1),
            "root_path": "",
        }
    )


@pytest.fixture(autouse=True)
def _bypass_http_ownership_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """这些 direct-call tests 覆盖 endpoint contract，HTTP auth 在 middleware 测试。"""
    async def _noop(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(server, "_bind_new_run_and_conversation", _noop)


# ---------------------------------------------------------------------------
# Strict request + COORDINATED-only admission
# ---------------------------------------------------------------------------


def test_evaluation_request_extra_field_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        server.RuntimeExecuteRequest(
            agent_id="a",
            query="q",
            run_id=uuid.uuid4().hex,
            timeout_seconds=30.0,
            extra_field=1,
        )


# ---------------------------------------------------------------------------
# Response contract + runtime / capture orthogonality
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluation_response_exact_keys_protocol_and_run_id(monkeypatch):
    run_id = uuid.uuid4().hex
    result = _result(RunStatus.SUCCEEDED, StopReason.COMPLETED, run_id=run_id)
    service = _EvalService(result)
    monkeypatch.setattr(server.app.state, "chat_service", service, raising=False)
    response = await _execute(_payload(run_id=run_id))
    body = json.loads(response.body)
    assert set(body) == {
        "protocol_version",
        "run_id",
        "status",
        "stop_reason",
        "error_code",
        "safe_message",
        "capture_status",
            "capture_error_code",
            "rag_evaluation_artifacts",
            "final_answer_capture_status",
            "final_answer_capture_error_code",
            "final_answer_evidence",
        }
    assert body["protocol_version"] == "localagent-evaluation-execute.v2"
    assert body["run_id"] == run_id
    assert body["status"] == "SUCCEEDED"
    assert body["capture_status"] == "COMPLETE"
    assert service.seen_collector is not None
    assert service.seen_collector.run_id == run_id


@pytest.mark.asyncio
async def test_runtime_succeeded_capture_complete_orthogonal(monkeypatch):
    run_id = uuid.uuid4().hex
    result = _result(RunStatus.SUCCEEDED, StopReason.COMPLETED, run_id=run_id)
    monkeypatch.setattr(server.app.state, "chat_service", _EvalService(result), raising=False)
    body = json.loads((await _execute(_payload(run_id=run_id))).body)
    assert body["status"] == "SUCCEEDED"
    assert body["capture_status"] == "COMPLETE"
    assert body["rag_evaluation_artifacts"] == []


@pytest.mark.asyncio
async def test_runtime_failed_capture_complete_keeps_failed_terminal(monkeypatch):
    run_id = uuid.uuid4().hex
    result = _result(
        RunStatus.FAILED,
        StopReason.UNHANDLED_ERROR,
        run_id=run_id,
        error_code="RUNTIME_AGENT_FAILURE",
    )
    monkeypatch.setattr(server.app.state, "chat_service", _EvalService(result), raising=False)
    body = json.loads((await _execute(_payload(run_id=run_id))).body)
    assert body["status"] == "FAILED"
    assert body["stop_reason"] == "UNHANDLED_ERROR"
    assert body["error_code"] == "RUNTIME_AGENT_FAILURE"
    assert body["capture_status"] == "COMPLETE"
    assert body["rag_evaluation_artifacts"] == []


# ---------------------------------------------------------------------------
# v2 final answer evidence — 独立于 RAG 与 Runtime terminal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v2_captures_exact_delivered_final_answer(monkeypatch):
    run_id = uuid.uuid4().hex
    answer = "answer-v2\n原样保留"
    result = _result(RunStatus.SUCCEEDED, StopReason.COMPLETED, run_id=run_id)
    monkeypatch.setattr(server.app.state, "chat_service", _EvalService(result, answer), raising=False)

    body = json.loads((await _execute_v2(_payload(run_id=run_id))).body)

    assert set(body) == {
        "protocol_version", "run_id", "status", "stop_reason", "error_code",
        "safe_message", "capture_status", "capture_error_code",
        "rag_evaluation_artifacts", "final_answer_capture_status",
        "final_answer_capture_error_code", "final_answer_evidence",
    }
    assert body["protocol_version"] == "localagent-evaluation-execute.v2"
    assert body["final_answer_capture_status"] == "COMPLETE"
    assert body["final_answer_capture_error_code"] is None
    assert body["final_answer_evidence"] == {
        "schema_version": "final-answer-evidence.v1",
        "evidence_id": f"final-answer://{run_id}",
        "run_id": run_id,
        "attempt_id": run_id,
        "media_type": "text/plain; charset=utf-8",
        "content_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "content": answer,
    }


@pytest.mark.asyncio
async def test_v2_failed_runtime_never_captures_incidental_output(monkeypatch):
    run_id = uuid.uuid4().hex
    result = _result(
        RunStatus.FAILED,
        StopReason.UNHANDLED_ERROR,
        run_id=run_id,
        error_code="RUNTIME_AGENT_FAILURE",
    )
    monkeypatch.setattr(
        server.app.state, "chat_service", _EvalService(result, "must-not-be-captured"), raising=False
    )

    body = json.loads((await _execute_v2(_payload(run_id=run_id))).body)

    assert body["status"] == "FAILED"
    assert body["final_answer_capture_status"] == "FAILED"
    assert body["final_answer_capture_error_code"] == "FINAL_ANSWER_RUNTIME_NOT_SUCCEEDED"
    assert body["final_answer_evidence"] is None


@pytest.mark.asyncio
async def test_v2_final_answer_over_utf8_bound_fails_without_partial_content(monkeypatch):
    run_id = uuid.uuid4().hex
    answer = "中" * 21_846  # 65,538 UTF-8 bytes, not 21,846 bytes.
    result = _result(RunStatus.SUCCEEDED, StopReason.COMPLETED, run_id=run_id)
    monkeypatch.setattr(server.app.state, "chat_service", _EvalService(result, answer), raising=False)

    body = json.loads((await _execute_v2(_payload(run_id=run_id))).body)

    assert body["status"] == "SUCCEEDED"
    assert body["capture_status"] == "COMPLETE"
    assert body["final_answer_capture_status"] == "FAILED"
    assert body["final_answer_capture_error_code"] == "FINAL_ANSWER_CONTENT_LIMIT_EXCEEDED"
    assert body["final_answer_evidence"] is None


@pytest.mark.asyncio
async def test_v2_keeps_rag_and_final_answer_evidence_independent(monkeypatch):
    run_id = uuid.uuid4().hex
    monkeypatch.setattr(server.app.state, "chat_service", _RagAndAnswerEvalService(), raising=False)

    body = json.loads((await _execute_v2(_payload(run_id=run_id))).body)

    assert body["capture_status"] == "COMPLETE"
    assert len(body["rag_evaluation_artifacts"]) == 1
    assert body["final_answer_capture_status"] == "COMPLETE"
    assert body["final_answer_evidence"]["content"] == "answer-v2"


# ---------------------------------------------------------------------------
# 1 MiB response boundary — runtime preserved, capture fails closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluation_response_over_1mib_fails_closed_keeps_terminal(monkeypatch):
    run_id = uuid.uuid4().hex
    service = _LargeEvalService(run_id, count=16)
    monkeypatch.setattr(server.app.state, "chat_service", service, raising=False)
    response = await _execute(_payload(run_id=run_id))
    body = json.loads(response.body)
    assert body["status"] == "SUCCEEDED"
    assert body["capture_status"] == "FAILED"
    assert body["capture_error_code"] == "RAG_EVALUATION_RESPONSE_SIZE_LIMIT_EXCEEDED"
    assert body["rag_evaluation_artifacts"] == []
