#!/usr/bin/env python
"""构建/服务独立 BM25 evaluation channel；不进入 Dense 或 hybrid pipeline。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, StrictStr

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.knowledge_base.beir_scifact_bm25_environment import (  # noqa: E402
    build_or_reuse_beir_scifact_bm25_cache,
    load_beir_scifact_bm25_cache,
)
from core.knowledge_base.bm25_sparse_index import (  # noqa: E402
    BM25_ALGORITHM_REF,
    BM25_B,
    BM25_K1,
    BM25_TOKENIZER_REF,
    Bm25Document,
    Bm25SparseIndex,
)
from core.knowledge_base.evaluation_environment import (  # noqa: E402
    COLLECTION_NAME as SYNTHETIC_COLLECTION,
    default_corpus_dir,
    prepare_evaluation_chunks,
)

SCIFACT_COLLECTION = "beir_scifact_bm25_eval_v1"
BM25_CANDIDATE_LIMIT = 8


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: StrictStr
    query: StrictStr
    run_id: StrictStr
    timeout_seconds: float = 60.0


class Bm25EvaluationService:
    def __init__(self, index: Bm25SparseIndex, *, collection: str) -> None:
        self._index = index
        self._collection = collection

    def execute(self, request: EvaluationRequest) -> dict[str, object]:
        try:
            UUID(request.run_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="invalid run_id") from error
        started = time.perf_counter_ns()
        results = self._index.search(request.query, top_k=BM25_CANDIDATE_LIMIT)
        elapsed_ms = max(0, (time.perf_counter_ns() - started) // 1_000_000)
        items = []
        for result in results:
            metadata = result.document.metadata
            items.append(
                {
                    "document_id": result.document.document_id,
                    "chunk_id": result.document.chunk_id,
                    "rank": result.rank,
                    "retrieval_rank": result.rank,
                    "rerank_rank": None,
                    "retrieval_score": result.score,
                    "retrieval_score_kind": "BM25_RAW_SCORE",
                    "retrieval_channels": ["BM25_SPARSE"],
                    "rerank_score": None,
                    "rerank_score_kind": None,
                    "source": {
                        "source_type": "bm25_sparse_index",
                        "collection": self._collection,
                        "display_name": str(metadata.get("source", result.document.chunk_id)),
                        "document_version": str(metadata.get("content_hash", "unknown")),
                    },
                    "page": None,
                    "section": str(metadata.get("section_path") or "") or None,
                    "sheet": None,
                    "content_hash": str(metadata.get("content_hash") or "") or None,
                    "selected": False,
                }
            )
        retrieval_id = "bm25-1"
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
            "retrieval_latency_ms": elapsed_ms,
            "rerank_latency_ms": None,
            "total_latency_ms": elapsed_ms,
            "degraded": False,
            "degradation_reasons": [],
            "error": None,
            "budget_usage": {
                "retrieval_calls": 1,
                "embedding_calls": 0,
                "vector_queries": 0,
                "keyword_queries": 0,
                "document_reads": 0,
                "context_chars": 0,
            },
        }
        content = "bm25 retrieval completed"
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


def create_app(service: Bm25EvaluationService) -> FastAPI:
    app = FastAPI()

    @app.post("/api/runtime/evaluation-execute/v2")
    async def execute(request: EvaluationRequest):
        return service.execute(request)

    @app.post("/api/runtime/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        return {"run_id": run_id, "status": "inactive"}

    return app


def _synthetic_index() -> Bm25SparseIndex:
    chunks, _document_count = prepare_evaluation_chunks(default_corpus_dir())
    documents = (
        Bm25Document(
            document_id=str(item["metadata"]["doc_id"]),
            chunk_id=str(item["metadata"]["chunk_id"]),
            text=str(item["page_content"]),
            metadata={
                "content_hash": str(item["metadata"]["content_hash"]),
                "source": str(item["metadata"]["source"]),
                "section_path": str(item["metadata"].get("section_path") or ""),
                "chunk_index": int(item["metadata"].get("chunk_index", 0)),
            },
        )
        for item in chunks
    )
    return Bm25SparseIndex.build(documents)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build-scifact")
    build.add_argument("--beir-corpus", type=Path, required=True)
    build.add_argument("--dense-cache-dir", type=Path, required=True)
    build.add_argument("--cache-root", type=Path)
    serve = sub.add_parser("serve-scifact")
    serve.add_argument("--cache-dir", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, required=True)
    synthetic = sub.add_parser("serve-synthetic")
    synthetic.add_argument("--host", default="127.0.0.1")
    synthetic.add_argument("--port", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build-scifact":
        result = build_or_reuse_beir_scifact_bm25_cache(
            corpus_jsonl=args.beir_corpus,
            dense_manifest_path=args.dense_cache_dir / "manifest.json",
            cache_root=args.cache_root,
        )
        print(
            json.dumps(
                {
                    "status": result.status,
                    "cache_dir": str(result.cache_dir),
                    "cache_key": result.identity.cache_key,
                    "chunk_manifest_sha256": result.identity.chunk_manifest_sha256,
                    "algorithm_ref": BM25_ALGORITHM_REF,
                    "tokenizer_ref": BM25_TOKENIZER_REF,
                    "k1": BM25_K1,
                    "b": BM25_B,
                    "build_elapsed_seconds": result.build_elapsed_seconds,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0

    if args.command == "serve-scifact":
        index, result = load_beir_scifact_bm25_cache(args.cache_dir)
        service = Bm25EvaluationService(index, collection=SCIFACT_COLLECTION)
        ready = {"status": "CACHE_HIT", "cache_key": result.identity.cache_key}
    else:
        index = _synthetic_index()
        service = Bm25EvaluationService(index, collection=SYNTHETIC_COLLECTION)
        ready = {"status": "READY", "chunk_count": index.document_count}
    import uvicorn

    print(json.dumps(ready, sort_keys=True), flush=True)
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
