"""BM25 evaluation adapter 保留 raw ranking 且不混入 Dense/RRF。"""

from __future__ import annotations

from uuid import uuid4

from scripts.bm25_evaluation_runtime import Bm25EvaluationService, EvaluationRequest
from core.knowledge_base.bm25_sparse_index import Bm25Document, Bm25SparseIndex


def test_adapter_emits_raw_bm25_top8_without_dense_or_rerank() -> None:
    index = Bm25SparseIndex.build(
        tuple(Bm25Document(str(i), f"c{i}", f"alpha {i}", {}) for i in range(10))
    )
    response = Bm25EvaluationService(index, collection="test").execute(
        EvaluationRequest(agent_id="knowledge_expert", query="alpha", run_id=str(uuid4()))
    )
    artifact = response["rag_evaluation_artifacts"][0]
    assert len(artifact["ranked_items"]) == 8
    assert artifact["retrieved_items"] == artifact["ranked_items"]
    assert artifact["selected_items"] == []
    assert artifact["rerank_latency_ms"] is None
    assert all(item["retrieval_score_kind"] == "BM25_RAW_SCORE" for item in artifact["ranked_items"])
    assert all(item["retrieval_channels"] == ["BM25_SPARSE"] for item in artifact["ranked_items"])
    assert artifact["budget_usage"] == {
        "retrieval_calls": 1,
        "embedding_calls": 0,
        "vector_queries": 0,
        "keyword_queries": 0,
        "document_reads": 0,
        "context_chars": 0,
    }


def test_adapter_empty_query_result_is_explicit() -> None:
    index = Bm25SparseIndex.build((Bm25Document("1", "c1", "alpha", {}),))
    response = Bm25EvaluationService(index, collection="test").execute(
        EvaluationRequest(agent_id="knowledge_expert", query="missing", run_id=str(uuid4()))
    )
    artifact = response["rag_evaluation_artifacts"][0]
    assert artifact["retrieval_status"] == "EMPTY"
    assert artifact["ranked_items"] == []
