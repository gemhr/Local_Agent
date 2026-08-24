"""Fixed structural RRF smoke（真实 service 组件 + 真实 READY caches，CE disabled）。

不计算任何 quality metric；只验证 SUBSTRATE_STRUCTURALLY_READY 的结构性事实：
budgets、manifest 成员、RRF score 可由 Σ1/(60+rank) exact 复算、provenance 完整。
Dense channel 在本测试使用确定性 stub（真实 cache lifecycle / READY / manifest），
BM25 channel 使用真实 sparse index 与真实 READY cache。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.documents import Document

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
from scripts.hybrid_rrf_evaluation_runtime import (
    EvaluationRequest,
    HybridRrfEvaluationService,
    JsonlProvenanceWriter,
)
from scripts.provision_wp4_synthetic_substrate import DenseEvaluationService

QUERIES = {
    "cal-answer-terminal-owner": "哪个组件拥有 Run 的 terminal fact？",
    "cal-empty-rfc9999": "RFC 9999 为本系统规定了什么算法？",
    "cal-misleading-context-dedup-provenance": "ContextBuilder 以 content hash 的 dedup_key 去重，是否保证去重项一定来自同一个原始 document？",
}


def _run_async(coro):
    return asyncio.run(coro)


class _Collection:
    def __init__(self, metadata):
        self.metadata = dict(metadata)

    def count(self):
        return 0

    def modify(self, metadata=None):
        if metadata is not None:
            self.metadata = dict(metadata)


class _StubDenseManager:
    """确定性 Dense channel stub；只用于驱动真实 RRF 集成。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.vector_store = type("Store", (), {"_collection": _Collection({})})()
        self._query_prompt_name = None

    def similarity_search_with_scores(self, query, top_k=8):
        results = []
        for i, chunk in enumerate(self._chunks[:top_k]):
            results.append(
                (
                    Document(page_content=str(chunk["page_content"]), metadata=dict(chunk["metadata"])),
                    1.0 - i * 0.01,
                )
            )
        return results


def _tiny_corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("# Alpha\n\nthe run coordinator owns the terminal fact alpha beta.", encoding="utf-8")
    (corpus / "b.md").write_text("# Beta\n\nRFC 9999 defines no algorithm gamma delta epsilon.", encoding="utf-8")
    (corpus / "c.md").write_text("# Gamma\n\ncontext builder dedup key provenance zeta eta theta.", encoding="utf-8")
    return corpus


def _stub_dense_cache(monkeypatch, corpus_dir, cache_root, stub_manager, model_path):
    chunks, document_count = ee.prepare_evaluation_chunks(corpus_dir)
    manifest = ee.EvaluationKbManifest(
        corpus_id=ee.CORPUS_ID,
        collection_name=ee.COLLECTION_NAME,
        document_count=document_count,
        chunk_count=len(chunks),
        embedding_model_name=ee.EMBEDDING_MODEL_NAME,
        embedding_dimension=ee.DENSE_DIMENSION,
        chunk_size=ee.CHUNK_SIZE,
        chunk_overlap=ee.CHUNK_OVERLAP,
        chunks=tuple(ee.ordered_chunk_identities(chunks)),
    )

    def _fake_build_evaluation_kb(*, persist_dir, embedding_model_path, corpus_dir, embedding_batch_size, query_prompt_name):
        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        return stub_manager, manifest

    monkeypatch.setattr(ee, "build_evaluation_kb", _fake_build_evaluation_kb)
    return ee.build_or_reuse_evaluation_dense_cache(
        corpus_dir=corpus_dir, cache_root=cache_root, embedding_model_path=model_path
    )


def _fused_expected(row):
    """按 frozen tie-break（score desc, best_rank asc, count desc, identity asc）复算 fused。"""
    current_ranks = {c: i + 1 for i, (_d, c) in enumerate(row["current_chunk_ranking"])}
    bm25_ranks = {c: i + 1 for i, (_d, c) in enumerate(row["bm25_chunk_ranking"])}
    union = set(current_ranks) | set(bm25_ranks)
    scored = []
    for chunk_id in union:
        present = [r for r in (current_ranks.get(chunk_id), bm25_ranks.get(chunk_id)) if r is not None]
        score = sum(1.0 / (RRF_K + r) for r in present)
        scored.append((score, min(present), len(present), chunk_id))
    scored.sort(key=lambda v: (-v[0], v[1], -v[2], v[3]))
    return [(chunk_id, score) for score, _best, _count, chunk_id in scored[:FINAL_FUSED_CANDIDATE_LIMIT]]


def test_structural_rrf_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corpus_dir = _tiny_corpus(tmp_path)
    chunks, document_count = ee.prepare_evaluation_chunks(corpus_dir)
    manifest_ids = {c["metadata"]["chunk_id"] for c in chunks}
    stub_manager = _StubDenseManager(chunks)

    model_dir = tmp_path / ee.EMBEDDING_MODEL_NAME
    model_dir.mkdir()
    dense_result = _stub_dense_cache(
        monkeypatch, corpus_dir, tmp_path / "dense-cache", stub_manager, model_dir
    )
    bm25_result = build_or_reuse_evaluation_bm25_cache(
        corpus_dir=corpus_dir,
        dense_manifest_path=dense_result.manifest_path,
        cache_root=tmp_path / "bm25-cache",
    )
    bm25_index, _ = load_evaluation_bm25_cache(
        bm25_result.cache_dir,
        expected_identity=bm25_result.identity,
        expected_document_count=document_count,
        expected_chunk_count=len(chunks),
    )

    # Dense/BM25 共享同一 ordered chunk manifest。
    assert dense_result.identity.chunk_manifest_sha256 == bm25_result.identity.chunk_manifest_sha256

    import scripts.bm25_evaluation_runtime as bm25_runtime
    import scripts.hybrid_rrf_evaluation_runtime as hybrid_mod

    dense_service = DenseEvaluationService(stub_manager, collection=ee.COLLECTION_NAME)
    bm25_service = bm25_runtime.Bm25EvaluationService(bm25_index, collection=ee.COLLECTION_NAME)

    def _post(url: str, payload: dict, timeout: float) -> dict:
        request = EvaluationRequest(**payload)
        if "current" in url:
            return dense_service.execute(request)
        return bm25_service.execute(request)

    monkeypatch.setattr(hybrid_mod.HybridRrfEvaluationService, "_post", staticmethod(_post))

    sidecar = tmp_path / "provenance.jsonl"
    service = HybridRrfEvaluationService(
        current_base_url="http://current",
        bm25_base_url="http://bm25",
        provenance_writer=JsonlProvenanceWriter(sidecar),
        cross_encoder_reranker=None,
    )

    for query in QUERIES.values():
        response = _run_async(
            service.execute(
                EvaluationRequest(agent_id="knowledge", query=query, run_id="00000000-0000-0000-0000-000000000000")
            )
        )
        artifact = response["rag_evaluation_artifacts"][0]
        fused = artifact["ranked_items"]
        assert len(fused) <= FINAL_FUSED_CANDIDATE_LIMIT
        assert all(item["chunk_id"] in manifest_ids for item in fused)
        assert all(item["retrieval_score_kind"] == "RRF_RANK_FUSION" for item in fused)
        assert all(item["rerank_rank"] is None for item in fused)
        assert artifact["retrieval_status"] in {"SUCCEEDED", "EMPTY"}
        assert artifact["artifact_id"]
        assert artifact["budget_usage"]["retrieval_calls"] >= 1

        rows = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
        row = rows[-1]
        assert len(row["current_chunk_ranking"]) <= PER_CHANNEL_CANDIDATE_LIMIT
        assert len(row["bm25_chunk_ranking"]) <= PER_CHANNEL_CANDIDATE_LIMIT
        union_ids = {c for _, c in row["current_chunk_ranking"]} | {c for _, c in row["bm25_chunk_ranking"]}
        assert len(union_ids) <= PRE_FUSION_UNION_MAX
        assert len(row["fused_items"]) <= FINAL_FUSED_CANDIDATE_LIMIT

        actual = [(item["chunk_id"], item["rrf_score"]) for item in row["fused_items"]]
        assert actual == _fused_expected(row)
        for item in row["fused_items"]:
            ranks = [r for r in (item["current_rank"], item["bm25_rank"]) if r is not None]
            assert item["rrf_score"] == sum(1.0 / (RRF_K + r) for r in ranks)
            assert item["contributing_channel_count"] in (1, 2)
        assert row["query_sha256"]
        assert row["algorithm_ref"] == "rrf.v1"
        assert row["rrf_k"] == RRF_K
