"""Hybrid RRF evaluation adapter 的 artifact、provenance 与失败语义。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from core.knowledge_base.cross_encoder_reranker import (
    CeLatency,
    CeProvenanceRecord,
    CeRankedCandidate,
    CeRerankResult,
    CrossEncoderCancellationError,
    CrossEncoderError,
    CrossEncoderTextResolutionError,
    CrossEncoderTimeoutError,
)
from core.runtime.blocking_executor import BlockingTaskWaitState
from scripts.hybrid_rrf_evaluation_runtime import (
    EvaluationRequest,
    HybridChannelError,
    HybridRrfEvaluationService,
    JsonlProvenanceWriter,
)


def _item(name: str, rank: int) -> dict[str, object]:
    return {
        "document_id": f"doc-{name}",
        "chunk_id": f"chunk-{name}",
        "rank": rank,
        "retrieval_rank": rank,
        "rerank_rank": None,
        "retrieval_score": 0.9,
        "retrieval_score_kind": "TEST",
        "retrieval_channels": ["TEST"],
        "rerank_score": None,
        "rerank_score_kind": None,
        "source": {
            "source_type": "test",
            "collection": "test",
            "display_name": name,
            "document_version": "v1",
        },
        "page": None,
        "section": None,
        "sheet": None,
        "content_hash": name,
        "selected": False,
    }


def _response(items: list[dict[str, object]], *, status: str | None = None) -> dict[str, object]:
    artifact_status = status or ("SUCCEEDED" if items else "EMPTY")
    return {
        "status": "SUCCEEDED",
        "capture_status": "COMPLETE",
        "rag_evaluation_artifacts": [
            {
                "retrieval_status": artifact_status,
                "ranked_items": items,
                "budget_usage": {
                    "retrieval_calls": 1,
                    "embedding_calls": 0,
                    "vector_queries": 1,
                    "keyword_queries": 0,
                    "document_reads": 0,
                    "context_chars": 0,
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_adapter_fuses_top8_without_selection_or_second_heuristic(
    monkeypatch, tmp_path: Path
) -> None:
    current = _response([_item("shared", 1), _item("current", 2)])
    bm25 = _response([_item("shared", 1), _item("bm25", 2)])
    responses = iter((current, bm25))
    monkeypatch.setattr(
        HybridRrfEvaluationService,
        "_post",
        staticmethod(lambda *_args, **_kwargs: next(responses)),
    )
    sidecar = tmp_path / "provenance.jsonl"
    service = HybridRrfEvaluationService(
        current_base_url="http://current",
        bm25_base_url="http://bm25",
        provenance_writer=JsonlProvenanceWriter(sidecar),
    )
    response = await service.execute(
        EvaluationRequest(agent_id="knowledge", query="query", run_id=str(uuid4()))
    )
    artifact = response["rag_evaluation_artifacts"][0]
    assert artifact["retrieved_items"] == artifact["ranked_items"]
    assert artifact["selected_items"] == []
    assert artifact["citations"] == []
    assert [item["chunk_id"] for item in artifact["ranked_items"]] == [
        "chunk-shared",
        "chunk-bm25",
        "chunk-current",
    ]
    assert all(item["retrieval_score_kind"] == "RRF_RANK_FUSION" for item in artifact["ranked_items"])
    provenance = json.loads(sidecar.read_text(encoding="utf-8"))
    assert provenance["fused_items"][0]["current_rank"] == 1
    assert provenance["fused_items"][0]["bm25_rank"] == 1
    assert provenance["algorithm_ref"] == "rrf.v1"
    assert provenance["rrf_k"] == 60


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "bm25", "expected"),
    [
        (_response([_item("current", 1)]), _response([]), ["chunk-current"]),
        (_response([]), _response([_item("bm25", 1)]), ["chunk-bm25"]),
        (_response([]), _response([]), []),
    ],
)
async def test_empty_channel_degradation_is_not_a_technical_failure(
    monkeypatch, current, bm25, expected
) -> None:
    responses = iter((current, bm25))
    monkeypatch.setattr(
        HybridRrfEvaluationService,
        "_post",
        staticmethod(lambda *_args, **_kwargs: next(responses)),
    )
    service = HybridRrfEvaluationService(
        current_base_url="http://current",
        bm25_base_url="http://bm25",
        provenance_writer=JsonlProvenanceWriter(None),
    )
    response = await service.execute(
        EvaluationRequest(agent_id="knowledge", query="query", run_id=str(uuid4()))
    )
    artifact = response["rag_evaluation_artifacts"][0]
    assert [item["chunk_id"] for item in artifact["ranked_items"]] == expected
    assert artifact["retrieval_status"] == ("SUCCEEDED" if expected else "EMPTY")


@pytest.mark.asyncio
async def test_technical_failure_is_not_silently_treated_as_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        HybridRrfEvaluationService,
        "_post",
        staticmethod(lambda *_args, **_kwargs: _response([], status="TIMED_OUT")),
    )
    service = HybridRrfEvaluationService(
        current_base_url="http://current",
        bm25_base_url="http://bm25",
        provenance_writer=JsonlProvenanceWriter(None),
    )
    with pytest.raises(Exception) as exc_info:
        await service.execute(
            EvaluationRequest(agent_id="knowledge", query="query", run_id=str(uuid4()))
        )
    assert getattr(exc_info.value, "detail", "") == "CURRENT_CHANNEL_TIMED_OUT"


def test_channel_request_error_has_stable_safe_code(monkeypatch) -> None:
    class FailedSession:
        trust_env = True

        def post(self, *_args, **_kwargs):
            raise RuntimeError("secret provider failure")

        def close(self):
            pass

    monkeypatch.setattr("scripts.hybrid_rrf_evaluation_runtime.requests.Session", FailedSession)
    with pytest.raises(HybridChannelError, match="HYBRID_CHANNEL_REQUEST_FAILED"):
        HybridRrfEvaluationService._post("http://channel", {}, 1.0)


# ---------------------------------------------------------------------------
# Cross-Encoder opt-in path（evaluation-only，显式注入 fake reranker）
# ---------------------------------------------------------------------------


class _FakeConfig:
    model_ref = "approved/cross-encoder@v1"
    asset_tree_sha256 = "a" * 64


class _FakeEnv:
    cache_identity = "cache-key"
    cache_digests = {"manifest_sha256": "manifest"}


class FakeCeReranker:
    def __init__(self, *, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls = 0
        self.config = _FakeConfig()
        self.env = _FakeEnv()

    async def rerank(self, *, query, candidates, remaining_seconds, run_id):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result

    def close(self):
        pass


def _ranked_item(name: str, pre_rank: int, post_rank: int, score: float) -> CeRankedCandidate:
    raw = dict(_item(name, pre_rank))
    return CeRankedCandidate(
        document_id=raw["document_id"],
        chunk_id=raw["chunk_id"],
        content_hash=str(raw["content_hash"]),
        resolved_text_sha256="0" * 64,
        pre_ce_rrf_rank=pre_rank,
        post_ce_rank=post_rank,
        cross_encoder_score=score,
        rrf_score=1 / 61,
        source_channels=("current-dense-led-ranked.v1",),
        payload=raw,
    )


def _success_result() -> CeRerankResult:
    ranked = (
        _ranked_item("current", 3, 1, 0.9),
        _ranked_item("shared", 1, 2, 0.7),
        _ranked_item("bm25", 2, 3, 0.5),
    )
    provenance = CeProvenanceRecord(
        query_sha256="query-sha",
        model_ref=_FakeConfig.model_ref,
        asset_tree_sha256=_FakeConfig.asset_tree_sha256,
        cache_identity=_FakeEnv.cache_identity,
        candidate_count=3,
        status="SUCCEEDED",
        latency=CeLatency(model_load_latency_ms=10.0, inference_latency_ms=1.5, ce_total_latency_ms=11.5),
    )
    return CeRerankResult(status="SUCCEEDED", ranked=ranked, provenance=provenance)


def _empty_result() -> CeRerankResult:
    provenance = CeProvenanceRecord(
        query_sha256="query-sha",
        model_ref=_FakeConfig.model_ref,
        asset_tree_sha256=_FakeConfig.asset_tree_sha256,
        candidate_count=0,
        status="EMPTY",
    )
    return CeRerankResult(status="EMPTY", ranked=(), provenance=provenance)


def _run_service(monkeypatch, *, reranker, ce_sidecar: Path | None = None, current=None, bm25=None):
    responses = iter(
        (
            current if current is not None else _response([_item("shared", 1), _item("current", 2)]),
            bm25 if bm25 is not None else _response([_item("shared", 1), _item("bm25", 2)]),
        )
    )
    monkeypatch.setattr(
        HybridRrfEvaluationService,
        "_post",
        staticmethod(lambda *_args, **_kwargs: next(responses)),
    )
    service = HybridRrfEvaluationService(
        current_base_url="http://current",
        bm25_base_url="http://bm25",
        provenance_writer=JsonlProvenanceWriter(None),
        cross_encoder_reranker=reranker,
        ce_provenance_writer=JsonlProvenanceWriter(ce_sidecar),
    )
    return service


@pytest.mark.asyncio
async def test_ce_not_configured_keeps_wp2_behavior(monkeypatch) -> None:
    responses = iter(
        (
            _response([_item("shared", 1), _item("current", 2)]),
            _response([_item("shared", 1), _item("bm25", 2)]),
        )
    )
    monkeypatch.setattr(
        HybridRrfEvaluationService,
        "_post",
        staticmethod(lambda *_args, **_kwargs: next(responses)),
    )
    service = HybridRrfEvaluationService(
        current_base_url="http://current",
        bm25_base_url="http://bm25",
        provenance_writer=JsonlProvenanceWriter(None),
    )
    response = await service.execute(
        EvaluationRequest(agent_id="knowledge", query="query", run_id=str(uuid4()))
    )
    artifact = response["rag_evaluation_artifacts"][0]
    assert artifact["retrieved_items"] == artifact["ranked_items"]
    assert isinstance(artifact["rerank_latency_ms"], int)


@pytest.mark.asyncio
async def test_ce_enabled_uses_post_ce_ranked_and_pre_ce_retrieved(
    monkeypatch, tmp_path: Path
) -> None:
    ce_sidecar = tmp_path / "ce-provenance.jsonl"
    reranker = FakeCeReranker(result=_success_result())
    service = _run_service(monkeypatch, reranker=reranker, ce_sidecar=ce_sidecar)
    response = await service.execute(
        EvaluationRequest(agent_id="knowledge", query="SECRET_QUERY_TEXT", run_id=str(uuid4()))
    )
    artifact = response["rag_evaluation_artifacts"][0]
    # retrieved_items = pre-CE RRF sequence。
    assert [item["chunk_id"] for item in artifact["retrieved_items"]] == [
        "chunk-shared",
        "chunk-bm25",
        "chunk-current",
    ]
    assert [item["retrieval_rank"] for item in artifact["retrieved_items"]] == [1, 2, 3]
    # ranked_items = post-CE 同一 identity 集合。
    assert [item["chunk_id"] for item in artifact["ranked_items"]] == [
        "chunk-current",
        "chunk-shared",
        "chunk-bm25",
    ]
    assert [item["rank"] for item in artifact["ranked_items"]] == [1, 2, 3]
    assert [item["retrieval_rank"] for item in artifact["ranked_items"]] == [3, 1, 2]
    # 同一 identity 集合（permutation 保持）。
    assert {item["chunk_id"] for item in artifact["ranked_items"]} == {
        item["chunk_id"] for item in artifact["retrieved_items"]
    }
    # CE 不解释 artifact rerank_*（继续 null）。
    assert all(item["rerank_rank"] is None for item in artifact["ranked_items"])
    assert all(item["rerank_score"] is None for item in artifact["ranked_items"])
    assert all(item["rerank_score_kind"] is None for item in artifact["ranked_items"])
    # RRF score/kind 保持。
    assert all(item["retrieval_score_kind"] == "RRF_RANK_FUSION" for item in artifact["ranked_items"])
    # CE sidecar 写入。
    rows = [json.loads(line) for line in ce_sidecar.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["schema_version"] == "localagent-cross-encoder-provenance.v1"
    assert rows[0]["status"] == "SUCCEEDED"
    assert rows[0]["candidate_count"] == 3
    assert rows[0]["latency_ms"]["model_load_latency_ms"] == 10.0
    assert rows[0]["latency_ms"]["inference_latency_ms"] == 1.5
    assert "SECRET_QUERY_TEXT" not in str(rows[0])


@pytest.mark.asyncio
async def test_ce_empty_candidates_stays_EMPTY_without_load(monkeypatch, tmp_path: Path) -> None:
    ce_sidecar = tmp_path / "ce-provenance.jsonl"
    reranker = FakeCeReranker(result=_empty_result())
    service = _run_service(
        monkeypatch, reranker=reranker, ce_sidecar=ce_sidecar,
        current=_response([]), bm25=_response([]),
    )
    response = await service.execute(
        EvaluationRequest(agent_id="knowledge", query="query", run_id=str(uuid4()))
    )
    artifact = response["rag_evaluation_artifacts"][0]
    assert artifact["retrieval_status"] == "EMPTY"
    assert artifact["ranked_items"] == []
    assert reranker.calls == 1  # N=0 仍走 CE 路径返回 EMPTY（不 load/infer 由 reranker 保证）
    rows = [json.loads(line) for line in ce_sidecar.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["status"] == "EMPTY"
    assert rows[0]["candidate_count"] == 0
    assert rows[0]["items"] == []


@pytest.mark.asyncio
async def test_ce_technical_failure_is_503_with_safe_failure_sidecar(
    monkeypatch, tmp_path: Path
) -> None:
    ce_sidecar = tmp_path / "ce-provenance.jsonl"
    reranker = FakeCeReranker(
        error=CrossEncoderError("CROSS_ENCODER_INFERENCE_EXCEPTION", ce_latency_ms=3.0)
    )
    service = _run_service(monkeypatch, reranker=reranker, ce_sidecar=ce_sidecar)
    with pytest.raises(Exception) as exc_info:
        await service.execute(
            EvaluationRequest(agent_id="knowledge", query="query", run_id=str(uuid4()))
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "CROSS_ENCODER_INFERENCE_EXCEPTION"
    rows = [json.loads(line) for line in ce_sidecar.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["status"] == "FAILED"
    assert rows[0]["safe_code"] == "CROSS_ENCODER_INFERENCE_EXCEPTION"
    assert rows[0]["items"] == []
    assert "cross_encoder_score" not in str(rows[0])


@pytest.mark.asyncio
async def test_ce_text_resolution_typed_error_writes_safe_failure_sidecar(
    monkeypatch, tmp_path: Path
) -> None:
    """P1-06：malformed Chroma columns -> typed CrossEncoderTextResolutionError -> 503 + safe sidecar（无 plaintext/raw exception）."""
    ce_sidecar = tmp_path / "ce-provenance.jsonl"
    reranker = FakeCeReranker(
        error=CrossEncoderTextResolutionError("CROSS_ENCODER_TEXT_RESOLUTION", ce_latency_ms=1.0)
    )
    service = _run_service(monkeypatch, reranker=reranker, ce_sidecar=ce_sidecar)
    with pytest.raises(Exception) as exc_info:
        await service.execute(
            EvaluationRequest(agent_id="knowledge", query="SECRET_P1_06_QUERY", run_id=str(uuid4()))
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "CROSS_ENCODER_TEXT_RESOLUTION"
    rows = [json.loads(line) for line in ce_sidecar.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["status"] == "FAILED"
    assert rows[0]["safe_code"] == "CROSS_ENCODER_TEXT_RESOLUTION"
    assert rows[0]["items"] == []
    assert "SECRET_P1_06_QUERY" not in str(rows[0])
    assert "ValueError" not in str(rows[0])


@pytest.mark.asyncio
async def test_ce_timeout_is_504_with_detach_truth(monkeypatch, tmp_path: Path) -> None:
    ce_sidecar = tmp_path / "ce-provenance.jsonl"
    reranker = FakeCeReranker(
        error=CrossEncoderTimeoutError(
            "CROSS_ENCODER_TIMEOUT",
            detach=BlockingTaskWaitState(False, True, True),
            ce_latency_ms=12.0,
        )
    )
    service = _run_service(monkeypatch, reranker=reranker, ce_sidecar=ce_sidecar)
    with pytest.raises(Exception) as exc_info:
        await service.execute(
            EvaluationRequest(agent_id="knowledge", query="query", run_id=str(uuid4()))
        )
    assert exc_info.value.status_code == 504
    rows = [json.loads(line) for line in ce_sidecar.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["status"] == "TIMED_OUT"
    assert rows[0]["detach"] == {
        "worker_terminated": False,
        "execution_detached": True,
        "background_work_pending": True,
    }


@pytest.mark.asyncio
async def test_ce_cancellation_reraises_asyncio_cancelled_error(monkeypatch, tmp_path: Path) -> None:
    ce_sidecar = tmp_path / "ce-provenance.jsonl"
    reranker = FakeCeReranker(
        error=CrossEncoderCancellationError(
            "CROSS_ENCODER_CANCELLED",
            detach=BlockingTaskWaitState(False, True, True),
        )
    )
    service = _run_service(monkeypatch, reranker=reranker, ce_sidecar=ce_sidecar)
    with pytest.raises(asyncio.CancelledError):
        await service.execute(
            EvaluationRequest(agent_id="knowledge", query="query", run_id=str(uuid4()))
        )
    rows = [json.loads(line) for line in ce_sidecar.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["status"] == "CANCELLED"
