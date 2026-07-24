from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from core.runtime import (
    BudgetLedger,
    CitationBinding,
    ContextTrustLevel,
    QueryEmbedding,
    QueryRewriteStrategy,
    RetrievalCandidate,
    RetrievalExecutionResult,
    RetrievalExecutionService,
    RetrievalExecutionStatus,
    RetrievalInvocation,
    RetrievalProvenance,
    RetrievalTransformation,
    RetrievedChunk,
    RunBudget,
    SourceMetadata,
    content_digest,
    create_run_context,
)
from core.runtime.retrieval_contract import (
    MaterializedDocument,
    RetrievalBudgetUsage,
)


def make_source(chunk_id: str = "chunk-1") -> SourceMetadata:
    return SourceMetadata(
        source_id="source-stable",
        source_type="md",
        collection="kb",
        canonical_uri="docs/source.md",
        display_name="source.md",
        document_version="version-1",
        page=3,
        section_path="Citation",
        chunk_id=chunk_id,
        chunk_index=0,
    )


def make_chunk(text: str = "final context") -> RetrievedChunk:
    source = make_source()
    context_hash = content_digest(text)
    citation = CitationBinding(
        citation_id="Rresult-1",
        source_id=source.source_id,
        chunk_id=source.chunk_id,
        context_block_id="context-1",
        context_content_hash=context_hash,
        display_label="source.md (p.3)",
        page=3,
        section_path="Citation",
    )
    provenance = RetrievalProvenance(
        source_id=source.source_id,
        chunk_id=source.chunk_id,
        original_rank=1,
        reranked_rank=1,
        retrieval_score=0.9,
        transformations=(
            RetrievalTransformation.LOADED,
            RetrievalTransformation.RERANKED,
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


def test_truncation_requires_new_context_hash() -> None:
    digest = content_digest("same")
    with pytest.raises(ValueError, match="截断后的 Context Hash"):
        RetrievalProvenance(
            source_id="source",
            chunk_id="chunk",
            original_rank=1,
            reranked_rank=None,
            retrieval_score=0.8,
            transformations=(
                RetrievalTransformation.LOADED,
                RetrievalTransformation.TRUNCATED,
                RetrievalTransformation.CONTEXT_SELECTED,
            ),
            original_content_hash=digest,
            context_content_hash=digest,
        )


def test_chunk_rejects_wrong_citation_hash_or_elevated_trust() -> None:
    chunk = make_chunk()
    with pytest.raises(ValueError, match="Context Hash"):
        replace(
            chunk,
            citation=replace(
                chunk.citation,
                context_content_hash=content_digest("different"),
            ),
        )
    with pytest.raises(ValueError, match="信任级别"):
        replace(chunk, trust_level=ContextTrustLevel.TRUSTED_INSTRUCTION)


def test_result_rejects_orphan_or_reordered_citation() -> None:
    chunk = make_chunk()
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="一一对应"):
        RetrievalExecutionResult(
            retrieval_id="result",
            status=RetrievalExecutionStatus.SUCCEEDED,
            rewritten_query_digest=content_digest("query"),
            final_chunks=(chunk,),
            citations=(),
            stage_records=(),
            degraded=False,
            degradation_reasons=(),
            budget_usage=RetrievalBudgetUsage(),
            started_at=now,
            completed_at=now,
            duration_ms=0,
        )


class DuplicateAdapter:
    query_rewrite_strategy = QueryRewriteStrategy.NONE
    has_explicit_embedding = True
    has_reranker = False

    def rewrite_query(self, query):
        return query

    def embed_query(self, query):
        return QueryEmbedding.create(query, [1.0, 0.0], "fake")

    def retrieve(self, query, embedding, invocation, *, max_candidates):
        source_a = make_source("chunk-a")
        source_b = replace(source_a, chunk_id="chunk-b", chunk_index=1)
        text = "duplicate final text"
        return [
            RetrievalCandidate(
                "candidate-a",
                source_a,
                0.9,
                1,
                {"chunk_id": "chunk-a"},
                "fake:a",
                text,
            ),
            RetrievalCandidate(
                "candidate-b",
                source_b,
                0.8,
                2,
                {"chunk_id": "chunk-b"},
                "fake:b",
                text,
            ),
        ]

    def keyword_retrieve(self, terms, invocation, *, max_candidates):
        return []

    def rerank(self, rewritten_query, original_query, candidates):
        return list(candidates)

    def materialize(self, candidate):
        return MaterializedDocument(
            candidate,
            candidate.text,
            content_digest(candidate.text),
        )


def test_deduplicated_candidate_has_no_orphan_citation() -> None:
    context, _source = create_run_context(entry_agent_id="knowledge_expert")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    result = RetrievalExecutionService(DuplicateAdapter()).execute(
        RetrievalInvocation.create(
            "query",
            collection_names=("kb",),
            top_k=4,
            rerank_top_k=2,
        ),
        run_context=context,
    )

    assert result.status == RetrievalExecutionStatus.SUCCEEDED
    assert len(result.final_chunks) == len(result.citations) == 1
    assert result.citations[0].chunk_id == "chunk-a"
    assert (
        RetrievalTransformation.DEDUPLICATED
        in result.final_chunks[0].provenance.transformations
    )
