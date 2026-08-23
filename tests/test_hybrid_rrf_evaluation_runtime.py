"""Hybrid RRF evaluation adapter 的 artifact、provenance 与失败语义。"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

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
