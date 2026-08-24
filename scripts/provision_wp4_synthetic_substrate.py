#!/usr/bin/env python
"""Stage5 Phase3 WP4 Phase A — synthetic Dense/BM25 READY cache provisioning + structural RRF smoke.

只做 EXPERIMENT_SUBSTRATE_PROVISIONING：构建 synthetic 15-doc / 60-chunk corpus 的
Dense/BM25 READY caches，机械取得真实 cache identities，warm 复核，并运行 3 个固定
CALIBRATION-only structural RRF smoke（CE disabled、真实 service 组件、不计算 quality）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, StrictStr

from core.knowledge_base import evaluation_environment as ee
from core.knowledge_base.evaluation_bm25_environment import (
    build_or_reuse_evaluation_bm25_cache,
    load_evaluation_bm25_cache,
)
from core.knowledge_base.hybrid_rrf_retriever import (
    FINAL_FUSED_CANDIDATE_LIMIT,
    PER_CHANNEL_CANDIDATE_LIMIT,
    PRE_FUSION_UNION_MAX,
    RRF_K,
)
from core.settings import Settings
from scripts.bm25_evaluation_runtime import Bm25EvaluationService
from scripts.bm25_evaluation_runtime import create_app as bm25_create_app
from scripts.hybrid_rrf_evaluation_runtime import (
    EvaluationRequest,
    HybridRrfEvaluationService,
    JsonlProvenanceWriter,
)

# Codex 预冻结 CALIBRATION-only structural smoke population。
SMOKE_CASES = (
    "cal-answer-terminal-owner",
    "cal-empty-rfc9999",
    "cal-misleading-context-dedup-provenance",
)


class DenseEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: StrictStr
    query: StrictStr
    run_id: StrictStr
    timeout_seconds: float = 60.0


class DenseEvaluationService:
    """薄 Dense channel service：只从 READY synthetic Dense cache 检索 top-8。"""

    def __init__(self, manager, *, collection: str) -> None:
        self._manager = manager
        self._collection = collection

    def execute(self, request: DenseEvaluationRequest) -> dict[str, object]:
        try:
            UUID(request.run_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="invalid run_id") from error
        started = time.perf_counter_ns()
        results = self._manager.similarity_search_with_scores(request.query, top_k=8)
        elapsed_ms = max(0, (time.perf_counter_ns() - started) // 1_000_000)
        items = []
        for rank, (document, score) in enumerate(results, 1):
            metadata = document.metadata
            items.append(
                {
                    "document_id": str(metadata.get("doc_id", "")),
                    "chunk_id": str(metadata.get("chunk_id", "")),
                    "rank": rank,
                    "retrieval_rank": rank,
                    "rerank_rank": None,
                    "retrieval_score": score,
                    "retrieval_score_kind": "DENSE_NORMALIZED_RELEVANCE",
                    "retrieval_channels": ["DENSE_VECTOR"],
                    "rerank_score": None,
                    "rerank_score_kind": None,
                    "source": {
                        "source_type": "vector_collection",
                        "collection": self._collection,
                        "display_name": str(metadata.get("source", "")),
                        "document_version": str(metadata.get("content_hash", "")),
                    },
                    "page": None,
                    "section": str(metadata.get("section_path") or "") or None,
                    "sheet": None,
                    "content_hash": str(metadata.get("content_hash") or "") or None,
                    "selected": False,
                }
            )
        artifact = {
            "schema_version": "rag-evaluation-artifact.v1",
            "artifact_id": f"rag-eval://{request.run_id}/dense-current-1",
            "run_id": request.run_id,
            "attempt_id": request.run_id,
            "retrieval_id": "dense-current-1",
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
                "embedding_calls": 1,
                "vector_queries": 1,
                "keyword_queries": 0,
                "document_reads": 0,
                "context_chars": 0,
            },
        }
        content = "dense current retrieval completed"
        final_answer = {
            "schema_version": "final-answer-evidence.v1",
            "evidence_id": f"final-answer://{request.run_id}",
            "run_id": request.run_id,
            "attempt_id": request.run_id,
            "media_type": "text/plain; charset=utf-8",
            "content_sha256": __import__("hashlib").sha256(content.encode("utf-8")).hexdigest(),
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


def create_dense_app(service: DenseEvaluationService) -> FastAPI:
    app = FastAPI()

    @app.post("/api/runtime/evaluation-execute/v2")
    async def execute(request: DenseEvaluationRequest):
        return service.execute(request)

    @app.post("/api/runtime/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        return {"run_id": run_id, "status": "inactive"}

    return app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_server(app, port: int) -> threading.Thread:
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("uvicorn server failed to start")
    return thread, server


def _structural_smoke(
    *,
    dense_service: DenseEvaluationService,
    bm25_index,
    dense_cache_dir: Path,
    bm25_cache_dir: Path,
    manifest_chunk_ids: set[str],
) -> dict[str, object]:
    import tempfile

    dense_port = _free_port()
    bm25_port = _free_port()
    dense_app = create_dense_app(dense_service)
    bm25_app = bm25_create_app(Bm25EvaluationService(bm25_index, collection=ee.COLLECTION_NAME))
    dense_thread, dense_server = _run_server(dense_app, dense_port)
    bm25_thread, bm25_server = _run_server(bm25_app, bm25_port)
    try:
        dense_ready = json.loads((dense_cache_dir / "cache_metadata.json").read_text(encoding="utf-8"))
        bm25_ready = json.loads((bm25_cache_dir / "cache_metadata.json").read_text(encoding="utf-8"))
        sidecar = Path(tempfile.mkdtemp(prefix="wp4-smoke-")) / "provenance.jsonl"
        service = HybridRrfEvaluationService(
            current_base_url=f"http://127.0.0.1:{dense_port}",
            bm25_base_url=f"http://127.0.0.1:{bm25_port}",
            provenance_writer=JsonlProvenanceWriter(sidecar),
            cross_encoder_reranker=None,
        )
        results = {}
        all_ok = True
        for case_id in SMOKE_CASES:
            response = asyncio.run(
                service.execute(
                    EvaluationRequest(
                        agent_id="knowledge",
                        query=_query_for(case_id),
                        run_id=str(uuid4()),
                    )
                )
            )
            artifact = response["rag_evaluation_artifacts"][0]
            rows = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
            row = rows[-1]
            checks = _validate_smoke_row(row, artifact, manifest_chunk_ids)
            results[case_id] = {
                "status": artifact["retrieval_status"],
                "dense_count": len(row["current_chunk_ranking"]),
                "bm25_count": len(row["bm25_chunk_ranking"]),
                "union_count": len(
                    {c for _, c in row["current_chunk_ranking"]} | {c for _, c in row["bm25_chunk_ranking"]}
                ),
                "fused_count": len(artifact["ranked_items"]),
                "fused_chunk_ids": [item["chunk_id"] for item in artifact["ranked_items"]],
                "ce_configured": False,
                "checks": checks,
                "validated": all(checks.values()),
            }
            all_ok = all_ok and results[case_id]["validated"]
        return {
            "dense_cache_status": dense_ready["cache_status"],
            "dense_cache_key": dense_ready["cache_key"],
            "bm25_cache_status": bm25_ready["cache_status"],
            "bm25_cache_key": bm25_ready["cache_key"],
            "rrf_k": RRF_K,
            "per_channel_candidate_limit": PER_CHANNEL_CANDIDATE_LIMIT,
            "pre_fusion_union_max": PRE_FUSION_UNION_MAX,
            "final_fused_candidate_limit": FINAL_FUSED_CANDIDATE_LIMIT,
            "ce_used": False,
            "structural_ready": all_ok,
            "results": results,
        }
    finally:
        dense_server.should_exit = True
        bm25_server.should_exit = True
        dense_thread.join(timeout=10)
        bm25_thread.join(timeout=10)


def _validate_smoke_row(row: dict, artifact: dict, manifest_chunk_ids: set[str]) -> dict[str, bool]:
    union_ids = {c for _, c in row["current_chunk_ranking"]} | {c for _, c in row["bm25_chunk_ranking"]}
    return {
        "ce_disabled": True,
        "dense_candidates_le_8": len(row["current_chunk_ranking"]) <= PER_CHANNEL_CANDIDATE_LIMIT,
        "bm25_candidates_le_8": len(row["bm25_chunk_ranking"]) <= PER_CHANNEL_CANDIDATE_LIMIT,
        "union_le_16": len(union_ids) <= PRE_FUSION_UNION_MAX,
        "fused_le_8": len(row["fused_items"]) <= FINAL_FUSED_CANDIDATE_LIMIT,
        "all_fused_in_manifest": all(item["chunk_id"] in manifest_chunk_ids for item in row["fused_items"]),
        "rrf_score_exact": all(
            item["rrf_score"]
            == sum(1.0 / (RRF_K + r) for r in (item["current_rank"], item["bm25_rank"]) if r is not None)
            for item in row["fused_items"]
        ),
        "provenance_complete": bool(row.get("query_sha256"))
        and bool(artifact.get("artifact_id"))
        and artifact.get("retrieval_status") in {"SUCCEEDED", "EMPTY"},
    }


def _query_for(case_id: str) -> str:
    queries = {
        "cal-answer-terminal-owner": "哪个组件拥有 Run 的 terminal fact？",
        "cal-empty-rfc9999": "RFC 9999 为本系统规定了什么算法？",
        "cal-misleading-context-dedup-provenance": "ContextBuilder 以 content hash 的 dedup_key 去重，是否保证去重项一定来自同一个原始 document？",
    }
    return queries[case_id]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--corpus-dir", type=Path)
    parser.add_argument("--embedding-model-path", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--run-smoke", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = Settings.load()
    corpus_dir = (args.corpus_dir or ee.default_corpus_dir()).resolve()
    model_path = (args.embedding_model_path or Path(settings.embedding_model_path)).resolve()
    cache_root = args.cache_root.resolve()
    evidence_dir = args.evidence_dir.resolve() if args.evidence_dir else None

    # 1. exact synthetic corpus
    chunks, document_count = ee.prepare_evaluation_chunks(corpus_dir)
    if document_count != 15 or len(chunks) != 60 or len({c["metadata"]["chunk_id"] for c in chunks}) != 60:
        raise SystemExit("SUBSTRATE_NOT_READY: corpus scale mismatch")
    source_digest = ee.source_manifest_digest(corpus_dir)
    chunk_digest = ee.ordered_chunk_manifest_digest(chunks)

    # 2. shared source/chunk manifest
    corpus_manifest = {
        "corpus_id": ee.CORPUS_ID,
        "document_count": document_count,
        "chunk_count": len(chunks),
        "source_manifest_sha256": source_digest,
        "chunk_manifest_sha256": chunk_digest,
        "chunk_size": ee.CHUNK_SIZE,
        "chunk_overlap": ee.CHUNK_OVERLAP,
        "chunks": ee.ordered_chunk_identities(chunks),
    }

    # 3. build Dense cache (cold build / reuse)
    dense_result = ee.build_or_reuse_evaluation_dense_cache(
        corpus_dir=corpus_dir,
        cache_root=cache_root,
        embedding_model_path=model_path,
        embedding_batch_size=settings.embedding_batch_size,
    )

    # 4-5. Dense READY + real identity
    dense_metadata = json.loads(dense_result.metadata_path.read_text(encoding="utf-8"))
    dense_chunk_manifest = json.loads(dense_result.manifest_path.read_text(encoding="utf-8"))

    # 6-8. build BM25 cache from exact same chunk manifest
    bm25_result = build_or_reuse_evaluation_bm25_cache(
        corpus_dir=corpus_dir,
        dense_manifest_path=dense_result.manifest_path,
        cache_root=cache_root,
    )
    bm25_metadata = json.loads(bm25_result.metadata_path.read_text(encoding="utf-8"))
    bm25_chunk_manifest = json.loads(bm25_result.manifest_path.read_text(encoding="utf-8"))

    # 9. warm load both (fresh objects) via strict load-by-identity
    warm_dense_manager, warm_dense = ee.load_evaluation_dense_cache(
        cache_dir=dense_result.cache_dir,
        expected_identity=dense_result.identity,
        expected_document_count=document_count,
        expected_chunk_count=len(chunks),
        embedding_model_path=model_path,
        embedding_batch_size=settings.embedding_batch_size,
    )
    warm_bm25_index, warm_bm25 = load_evaluation_bm25_cache(
        bm25_result.cache_dir,
        expected_identity=bm25_result.identity,
        expected_document_count=document_count,
        expected_chunk_count=len(chunks),
    )

    # 10. record provenance
    run_manifest = {
        "phase": "stage5_phase3_wp4_substrate_phase_a",
        "status": "READY",
        "dense": {
            "cache_schema": ee.DENSE_CACHE_SCHEMA_VERSION,
            "cache_identity": dense_metadata["cache_key"],
            "cache_status": dense_metadata["cache_status"],
            "document_count": dense_metadata["document_count"],
            "chunk_count": dense_metadata["chunk_count"],
            "source_manifest_sha256": dense_metadata["source_manifest_sha256"],
            "chunk_manifest_sha256": dense_metadata["chunk_manifest_sha256"],
            "embedding_model": dense_metadata["embedding_model"],
            "embedding_dimension": dense_metadata["embedding_dimension"],
            "embedding_local_files_only": dense_metadata["embedding_local_files_only"],
            "normalize_embeddings": dense_metadata["normalize_embeddings"],
            "embedding_query_prompt": dense_metadata["embedding_query_prompt"],
            "splitter_identity": dense_metadata["splitter_identity"],
            "chunk_size": dense_metadata["chunk_size"],
            "chunk_overlap": dense_metadata["chunk_overlap"],
            "cold_build": dense_result.status,
            "warm_status": warm_dense.status,
        },
        "bm25": {
            "cache_schema": ee.BM25_CACHE_SCHEMA_VERSION,
            "cache_identity": bm25_metadata["cache_key"],
            "cache_status": bm25_metadata["cache_status"],
            "document_count": bm25_metadata["document_count"],
            "chunk_count": bm25_metadata["chunk_count"],
            "source_manifest_sha256": bm25_metadata["source_manifest_sha256"],
            "chunk_manifest_sha256": bm25_metadata["chunk_manifest_sha256"],
            "algorithm_ref": bm25_metadata["algorithm_ref"],
            "tokenizer_ref": bm25_metadata["tokenizer_ref"],
            "k1": bm25_metadata["k1"],
            "b": bm25_metadata["b"],
            "cold_build": bm25_result.status,
            "warm_status": warm_bm25.status,
        },
        "manifest_match": {
            "dense_ordered_chunk_digest": dense_metadata["chunk_manifest_sha256"],
            "bm25_ordered_chunk_digest": bm25_metadata["chunk_manifest_sha256"],
            "exact_equal": dense_metadata["chunk_manifest_sha256"]
            == bm25_metadata["chunk_manifest_sha256"],
        },
    }

    print(json.dumps(run_manifest, ensure_ascii=False, indent=2, sort_keys=True), flush=True)

    if args.run_smoke:
        manifest_chunk_ids = {c["chunk_id"] for c in corpus_manifest["chunks"]}
        smoke = _structural_smoke(
            dense_service=DenseEvaluationService(warm_dense_manager, collection=ee.COLLECTION_NAME),
            bm25_index=warm_bm25_index,
            dense_cache_dir=dense_result.cache_dir,
            bm25_cache_dir=bm25_result.cache_dir,
            manifest_chunk_ids=manifest_chunk_ids,
        )
        print(json.dumps({"smoke": smoke}, ensure_ascii=False, indent=2, sort_keys=True), flush=True)

    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "corpus_manifest.json").write_text(
            json.dumps(corpus_manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        (evidence_dir / "dense_cache_metadata.json").write_text(
            json.dumps(dense_metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        (evidence_dir / "dense_chunk_manifest.json").write_text(
            json.dumps(dense_chunk_manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        (evidence_dir / "bm25_cache_metadata.json").write_text(
            json.dumps(bm25_metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        (evidence_dir / "bm25_chunk_manifest.json").write_text(
            json.dumps(bm25_chunk_manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        (evidence_dir / "run_manifest.json").write_text(
            json.dumps(run_manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        if args.run_smoke:
            (evidence_dir / "smoke_result.json").write_text(
                json.dumps({"smoke": smoke}, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
