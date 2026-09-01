"""Hybrid adapter 的直接融合合同：双通道、原始 RRF 分数与 BM25-only 物化。"""

from __future__ import annotations

from types import SimpleNamespace

from core.knowledge_base.bm25_sparse_index import Bm25Document, Bm25SparseIndex
from core.knowledge_base.hybrid_rrf_retriever import HybridRrfRetriever
from core.runtime.hybrid_retrieval_adapter import HybridKnowledgeRetrievalAdapter
from core.runtime.retrieval_contract import RetrievalCandidate, SourceMetadata


def _candidate(document_id: str, chunk_id: str, rank: int) -> RetrievalCandidate:
    source = SourceMetadata(
        source_id=document_id,
        source_type="md",
        collection="kb",
        canonical_uri=f"{document_id}.md",
        display_name=f"{document_id}.md",
        document_version="v1",
        page=1,
        section_path="S",
        chunk_id=chunk_id,
        chunk_index=rank,
    )
    return RetrievalCandidate(
        candidate_id=f"{document_id}|{chunk_id}",
        source=source,
        score=0.9,
        original_rank=rank,
        metadata={"doc_id": document_id, "chunk_id": chunk_id},
        content_locator=f"fake:{chunk_id}",
        text=f"{document_id} text",
    )


def _adapter(index: Bm25SparseIndex) -> HybridKnowledgeRetrievalAdapter:
    adapter = object.__new__(HybridKnowledgeRetrievalAdapter)
    adapter._bm25_index = index
    adapter._rrf = HybridRrfRetriever()
    adapter.db_manager = SimpleNamespace(collection_name="kb")
    return adapter


def test_fuse_uses_one_bm25_search_and_preserves_raw_rrf_score() -> None:
    index = Bm25SparseIndex.build(
        (
            Bm25Document("d1", "c1", "alpha", {}),
        )
    )
    adapter = _adapter(index)
    charges = []
    result = adapter.fuse("alpha", [_candidate("d1", "c1", 1)], charge=charges.append)

    assert result.bm25_candidate_count == 1
    assert result.rrf_fused_count == 1
    assert result.candidates[0].reranked_score is not None
    assert result.candidates[0].reranked_score < 0.55
    assert [charge.bm25_queries for charge in charges] == [1, 0]
    assert [charge.rrf_fusions for charge in charges] == [0, 1]


def test_empty_dense_channel_does_not_start_bm25() -> None:
    index = Bm25SparseIndex.build((Bm25Document("d1", "c1", "alpha", {}),))
    adapter = _adapter(index)
    charges = []

    try:
        adapter.fuse("alpha", [], charge=charges.append)
    except Exception as exc:
        assert getattr(exc, "safe_error_code", None) == "HYBRID_DENSE_CHANNEL_EMPTY"
    else:  # pragma: no cover - contract assertion
        raise AssertionError("empty Dense channel must fail closed")
    assert charges == []
