#!/usr/bin/env python
"""显式评估用 Current + BM25 + RRF runtime；不切换 production default。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, StrictStr

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.knowledge_base.hybrid_rrf_retriever import (  # noqa: E402
    BM25_CHANNEL_REF,
    CURRENT_CHANNEL_REF,
    FINAL_FUSED_CANDIDATE_LIMIT,
    HybridRrfRetriever,
    RRF_ALGORITHM_REF,
    RRF_K,
    RrfChannelCandidate,
)

_TECHNICAL_STATUSES = frozenset({"FAILED", "TIMED_OUT", "CANCELLED"})


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: StrictStr
    query: StrictStr
    run_id: StrictStr
    timeout_seconds: float = 60.0


class HybridChannelError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class JsonlProvenanceWriter:
    """只写 evaluation sidecar，不进入 Runtime/Public Result Contract。"""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()

    def append(self, payload: Mapping[str, object]) -> None:
        if self._path is None:
            return
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded + "\n")


class HybridRrfEvaluationService:
    """顺序调用既有双通道，并只在 query-time 执行 chunk-level RRF。"""

    def __init__(
        self,
        *,
        current_base_url: str,
        bm25_base_url: str,
        provenance_writer: JsonlProvenanceWriter,
    ) -> None:
        self._current_url = current_base_url.rstrip("/") + "/api/runtime/evaluation-execute/v2"
        self._bm25_url = bm25_base_url.rstrip("/") + "/api/runtime/evaluation-execute/v2"
        self._writer = provenance_writer
        self._retriever = HybridRrfRetriever(rrf_k=RRF_K)

    @staticmethod
    def _post(url: str, payload: Mapping[str, object], timeout: float) -> Mapping[str, Any]:
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.post(url, json=dict(payload), timeout=timeout)
            response.raise_for_status()
            value = response.json()
        except (requests.RequestException, RuntimeError, ValueError) as error:
            raise HybridChannelError("HYBRID_CHANNEL_REQUEST_FAILED") from error
        finally:
            session.close()
        if not isinstance(value, dict):
            raise HybridChannelError("HYBRID_CHANNEL_RESPONSE_INVALID")
        return value

    @staticmethod
    def _artifact(response: Mapping[str, Any], *, channel: str) -> Mapping[str, Any]:
        if response.get("status") in _TECHNICAL_STATUSES:
            raise HybridChannelError(f"{channel}_CHANNEL_{response['status']}")
        if response.get("capture_status") != "COMPLETE":
            raise HybridChannelError(f"{channel}_CHANNEL_CAPTURE_FAILED")
        artifacts = response.get("rag_evaluation_artifacts")
        if not isinstance(artifacts, list) or len(artifacts) != 1 or not isinstance(artifacts[0], dict):
            raise HybridChannelError(f"{channel}_CHANNEL_ARTIFACT_INVALID")
        artifact = artifacts[0]
        if artifact.get("retrieval_status") in _TECHNICAL_STATUSES:
            raise HybridChannelError(f"{channel}_CHANNEL_{artifact['retrieval_status']}")
        if artifact.get("retrieval_status") not in {"SUCCEEDED", "EMPTY", "DEGRADED"}:
            raise HybridChannelError(f"{channel}_CHANNEL_STATUS_INVALID")
        ranked = artifact.get("ranked_items")
        if not isinstance(ranked, list):
            raise HybridChannelError(f"{channel}_CHANNEL_RANKING_INVALID")
        return artifact

    @staticmethod
    def _candidates(artifact: Mapping[str, Any]) -> tuple[RrfChannelCandidate, ...]:
        candidates = []
        for expected_rank, raw in enumerate(artifact["ranked_items"], 1):
            if not isinstance(raw, dict) or raw.get("rank") != expected_rank:
                raise HybridChannelError("HYBRID_CHANNEL_RANKING_INVALID")
            document_id = raw.get("document_id")
            chunk_id = raw.get("chunk_id")
            if not isinstance(document_id, str) or not isinstance(chunk_id, str):
                raise HybridChannelError("HYBRID_CHANNEL_IDENTITY_INVALID")
            candidates.append(
                RrfChannelCandidate(
                    document_id=document_id,
                    chunk_id=chunk_id,
                    rank=expected_rank,
                    payload=raw,
                )
            )
        return tuple(candidates)

    @staticmethod
    def _budget(current: Mapping[str, Any], bm25: Mapping[str, Any]) -> dict[str, int]:
        result: dict[str, int] = {}
        for field in (
            "retrieval_calls",
            "embedding_calls",
            "vector_queries",
            "keyword_queries",
            "document_reads",
            "context_chars",
        ):
            left = current.get("budget_usage", {}).get(field, 0)
            right = bm25.get("budget_usage", {}).get(field, 0)
            if not isinstance(left, int) or not isinstance(right, int):
                raise HybridChannelError("HYBRID_CHANNEL_BUDGET_INVALID")
            result[field] = left + right
        return result

    async def execute(self, request: EvaluationRequest) -> dict[str, object]:
        try:
            UUID(request.run_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="invalid run_id") from error
        payload = request.model_dump(mode="json")
        hybrid_started = time.perf_counter_ns()
        current_started = time.perf_counter_ns()
        try:
            current_response = await asyncio.to_thread(
                self._post, self._current_url, payload, request.timeout_seconds
            )
            current_latency_ms = (time.perf_counter_ns() - current_started) / 1_000_000
            bm25_started = time.perf_counter_ns()
            bm25_response = await asyncio.to_thread(
                self._post, self._bm25_url, payload, request.timeout_seconds
            )
            bm25_latency_ms = (time.perf_counter_ns() - bm25_started) / 1_000_000
            current = self._artifact(current_response, channel="CURRENT")
            bm25 = self._artifact(bm25_response, channel="BM25")
            fusion_started = time.perf_counter_ns()
            fused = self._retriever.fuse(
                self._candidates(current),
                self._candidates(bm25),
                final_top_k=FINAL_FUSED_CANDIDATE_LIMIT,
            )
            fusion_latency_ms = (time.perf_counter_ns() - fusion_started) / 1_000_000
        except (HybridChannelError, ValueError) as error:
            code = error.code if isinstance(error, HybridChannelError) else "RRF_FUSION_INVALID"
            raise HTTPException(status_code=503, detail=code) from None

        items = []
        provenance_items = []
        for candidate in fused:
            raw = candidate.payload
            channels = list(candidate.source_channels)
            items.append(
                {
                    "document_id": candidate.document_id,
                    "chunk_id": candidate.chunk_id,
                    "rank": candidate.rank,
                    "retrieval_rank": candidate.rank,
                    "rerank_rank": None,
                    "retrieval_score": candidate.rrf_score,
                    "retrieval_score_kind": "RRF_RANK_FUSION",
                    "retrieval_channels": channels,
                    "rerank_score": None,
                    "rerank_score_kind": None,
                    "source": raw["source"],
                    "page": raw.get("page"),
                    "section": raw.get("section"),
                    "sheet": raw.get("sheet"),
                    "content_hash": raw.get("content_hash"),
                    "selected": False,
                }
            )
            provenance_items.append(
                {
                    "document_id": candidate.document_id,
                    "chunk_id": candidate.chunk_id,
                    "current_rank": candidate.current_rank,
                    "bm25_rank": candidate.bm25_rank,
                    "rrf_score": candidate.rrf_score,
                    "rrf_rank": candidate.rank,
                    "source_channels": channels,
                    "contributing_channel_count": candidate.contributing_channel_count,
                }
            )
        hybrid_latency_ms = (time.perf_counter_ns() - hybrid_started) / 1_000_000
        def artifact_latency(value: float) -> int:
            return max(0, int(round(value)))
        retrieval_id = "hybrid-rrf-1"
        artifact = {
            "schema_version": "rag-evaluation-artifact.v1",
            "artifact_id": f"rag-eval://{request.run_id}/{retrieval_id}",
            "run_id": request.run_id,
            "attempt_id": request.run_id,
            "retrieval_id": retrieval_id,
            "invocation_index": 1,
            "retrieval_status": "SUCCEEDED" if items else "EMPTY",
            "query": request.query,
            "rewritten_query": request.query,
            "retrieved_items": items,
            "ranked_items": items,
            "selected_items": [],
            "citations": [],
            "retrieval_latency_ms": artifact_latency(current_latency_ms + bm25_latency_ms),
            "rerank_latency_ms": artifact_latency(fusion_latency_ms),
            "total_latency_ms": artifact_latency(hybrid_latency_ms),
            "degraded": False,
            "degradation_reasons": [],
            "error": None,
            "budget_usage": self._budget(current, bm25),
        }
        self._writer.append(
            {
                "run_id": request.run_id,
                "query_sha256": hashlib.sha256(request.query.encode("utf-8")).hexdigest(),
                "algorithm_ref": RRF_ALGORITHM_REF,
                "rrf_k": RRF_K,
                "current_status": current["retrieval_status"],
                "bm25_status": bm25["retrieval_status"],
                "current_chunk_ranking": [
                    [item["document_id"], item["chunk_id"]] for item in current["ranked_items"]
                ],
                "bm25_chunk_ranking": [
                    [item["document_id"], item["chunk_id"]] for item in bm25["ranked_items"]
                ],
                "fused_items": provenance_items,
                "latency_ms": {
                    "current_channel": current_latency_ms,
                    "bm25_channel": bm25_latency_ms,
                    "rrf_fusion": fusion_latency_ms,
                    "hybrid_total": hybrid_latency_ms,
                },
            }
        )
        content = "hybrid RRF retrieval completed"
        final_answer = {
            "schema_version": "final-answer-evidence.v1",
            "evidence_id": f"final-answer://{request.run_id}",
            "run_id": request.run_id,
            "attempt_id": request.run_id,
            "media_type": "text/plain; charset=utf-8",
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "content": content,
        }
        return {
            "protocol_version": "localagent-evaluation-execute.v2",
            "run_id": request.run_id,
            "status": "SUCCEEDED",
            "stop_reason": "COMPLETED",
            "error_code": None,
            "safe_message": "",
            "capture_status": "COMPLETE",
            "capture_error_code": None,
            "rag_evaluation_artifacts": [artifact],
            "final_answer_capture_status": "COMPLETE",
            "final_answer_capture_error_code": None,
            "final_answer_evidence": final_answer,
        }


def create_app(service: HybridRrfEvaluationService) -> FastAPI:
    app = FastAPI()

    @app.post("/api/runtime/evaluation-execute/v2")
    async def execute(request: EvaluationRequest):
        return await service.execute(request)

    @app.post("/api/runtime/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        return {"run_id": run_id, "status": "inactive"}

    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-base-url", required=True)
    parser.add_argument("--bm25-base-url", required=True)
    parser.add_argument("--provenance-out", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    service = HybridRrfEvaluationService(
        current_base_url=args.current_base_url,
        bm25_base_url=args.bm25_base_url,
        provenance_writer=JsonlProvenanceWriter(args.provenance_out),
    )
    import uvicorn

    print(
        json.dumps(
            {
                "status": "READY",
                "algorithm_ref": RRF_ALGORITHM_REF,
                "rrf_k": RRF_K,
                "left": CURRENT_CHANNEL_REF,
                "right": BM25_CHANNEL_REF,
                "execution_mode": "sequential",
            },
            sort_keys=True,
        ),
        flush=True,
    )
    uvicorn.run(
        create_app(service),
        host=args.host,
        port=args.port,
        lifespan="off",
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
