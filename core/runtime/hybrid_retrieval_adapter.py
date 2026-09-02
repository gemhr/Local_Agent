#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""HYBRID_RRF 的策略专用 retrieval adapter 与 fusion collaborator。

Frozen by ``.ai/handoff/stage5-phase6-wp2/20_codex_decision.md``：

- Dense rewritten/original 结果按既有 merge 语义去重后是**一个** RRF channel；
- BM25（``rewritten_query`` 一次搜索，top_k=8）是唯一 sparse channel；
- Chroma keyword supplement 在 Hybrid 下禁用（``should_keyword_retrieve`` 恒 False）；
- RRF 身份永远是 ``(document_id, chunk_id)``；fused 输出按 fused rank 承载在
  既有 ``reranked_rank``/``reranked_score`` 字段上，score 绝不冒充 Dense relevance；
- BM25-only winner 通过 ``VectorDBManager.get_chunk_by_identity`` 精确回取
  Dense/Chroma 权威正文与 SourceMetadata；BM25 metadata 只用于 ranking/audit；
- 每次融合的全部状态都是 call-local（application-scoped adapter 可并发复用）；
- ``charge`` 回调由 Service 提供，按"操作开始才计数"的语义记账 bm25/rrf/document_reads。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Callable, Sequence

from core.knowledge_base.bm25_sparse_index import Bm25SparseIndex
from core.knowledge_base.hybrid_rrf_retriever import (
    HybridRrfRetriever,
    HybridRrfProfile,
    RrfChannelCandidate,
)
from core.runtime.budget import BudgetUsage
from core.runtime.retrieval_adapters import (
    RetrievalAdapterError,
    RuntimeKnowledgeRetrievalAdapter,
    VectorScoreSemantics,
)
from core.runtime.retrieval_contract import (
    RetrievalCandidate,
    RetrievalErrorCategory,
)

HYBRID_RETRIEVAL_STRATEGY = "HYBRID_RRF"
HYBRID_CHANNEL_CANDIDATE_LIMIT = 8


@dataclass(frozen=True, slots=True)
class Bm25EvidenceRow:
    """一次 BM25 搜索的单条 channel 证据（raw score，未归一）。"""

    document_id: str
    chunk_id: str
    rank: int
    raw_score: float


@dataclass(frozen=True, slots=True)
class HybridRankedFact:
    """一个 fused candidate 的通道参与事实（rank-only 语义）。"""

    candidate_id: str
    dense_channel_rank: int | None
    bm25_channel_rank: int | None


@dataclass(frozen=True, slots=True)
class HybridFusionResult:
    """单次 Hybrid 融合的全部 call-local 事实。"""

    candidates: list[RetrievalCandidate]
    bm25_evidence: tuple[Bm25EvidenceRow, ...]
    ranked_facts: tuple[HybridRankedFact, ...]
    dense_candidate_count: int
    bm25_candidate_count: int
    rrf_fused_count: int
    bm25_latency_ms: int
    rrf_latency_ms: int
    document_lookups: int


def _channel_candidates(
    items: Sequence[tuple[str, str, int, object]],
    *,
    channel: str,
) -> tuple[RrfChannelCandidate, ...]:
    """按冻结合同构造 channel candidates：身份非空、无重复、rank 连续 1..N。"""
    if not items:
        raise RetrievalAdapterError(
            RetrievalErrorCategory.FUSION_FAILED,
            "HYBRID_CHANNEL_MISSING",
            f"Hybrid {channel} channel is missing or invalid.",
        )
    seen: set[tuple[str, str]] = set()
    channel_candidates: list[RrfChannelCandidate] = []
    for document_id, chunk_id, rank, payload in items[:HYBRID_CHANNEL_CANDIDATE_LIMIT]:
        if not document_id or not chunk_id:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.FUSION_FAILED,
                "HYBRID_CHANNEL_MISSING",
                f"Hybrid {channel} channel candidate identity is invalid.",
            )
        identity = (document_id, chunk_id)
        if identity in seen:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.FUSION_FAILED,
                "RRF_FUSION_FAILED",
                f"Hybrid {channel} channel contains duplicate identity.",
            )
        seen.add(identity)
        channel_candidates.append(
            RrfChannelCandidate(
                document_id=document_id,
                chunk_id=chunk_id,
                rank=rank,
                payload=payload,
            )
        )
    ranks = [item.rank for item in channel_candidates]
    if ranks != list(range(1, len(ranks) + 1)):
        raise RetrievalAdapterError(
            RetrievalErrorCategory.FUSION_FAILED,
            "RRF_FUSION_FAILED",
            f"Hybrid {channel} channel ranks are not contiguous.",
        )
    return tuple(channel_candidates)


class HybridKnowledgeRetrievalAdapter(RuntimeKnowledgeRetrievalAdapter):
    """Dense merged channel + 唯一 BM25 channel 的 RRF 编排（策略专用）。"""

    hybrid_rrf = True
    retrieval_strategy = HYBRID_RETRIEVAL_STRATEGY

    def __init__(
        self,
        *args,
        bm25_index: Bm25SparseIndex,
        generation_id: str,
        provenance_sha256: str,
        hybrid_rrf_profile: HybridRrfProfile | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        if bm25_index is None:
            raise ValueError("HybridKnowledgeRetrievalAdapter requires bm25_index")
        self._bm25_index = bm25_index
        self._rrf = HybridRrfRetriever(profile=hybrid_rrf_profile)
        self.has_reranker = True
        self.hybrid_generation_id = generation_id
        self.hybrid_provenance_sha256 = provenance_sha256

    def should_keyword_retrieve(self, terms, invocation) -> bool:  # type: ignore[no-untyped-def]
        """Hybrid 完全禁用 Chroma keyword supplement（frozen decision §6）。"""
        return False

    def fuse(
        self,
        rewritten_query: str,
        dense_candidates: Sequence[RetrievalCandidate],
        *,
        charge: Callable[[BudgetUsage], None],
    ) -> HybridFusionResult:
        """执行一次完整 Hybrid 融合；全部状态 call-local，可并发复用。"""
        if not dense_candidates:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.FUSION_FAILED,
                "HYBRID_DENSE_CHANNEL_EMPTY",
                "Hybrid Dense channel is empty.",
            )
        # Dense channel：merged Dense 列表（original_rank 已是 1..N 连续）。
        dense_items = tuple(
            (item.source_id, item.chunk_id, item.original_rank, item)
            for item in dense_candidates
        )
        dense_channel = _channel_candidates(dense_items, channel="Dense")

        # BM25 channel：rewritten_query 一次搜索（§6）。
        charge(BudgetUsage(bm25_queries=1))
        bm25_started = time.perf_counter()
        try:
            bm25_rows = self._bm25_index.search(rewritten_query, top_k=HYBRID_CHANNEL_CANDIDATE_LIMIT)
        except RetrievalAdapterError:
            raise
        except Exception:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.SPARSE_RETRIEVAL_FAILED,
                "BM25_SEARCH_FAILED",
                "BM25 search failed.",
            ) from None
        bm25_latency_ms = max(0, int((time.perf_counter() - bm25_started) * 1000))
        if not bm25_rows:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.FUSION_FAILED,
                "HYBRID_BM25_CHANNEL_EMPTY",
                "Hybrid BM25 channel is empty.",
            )
        bm25_evidence = tuple(
            Bm25EvidenceRow(
                document_id=row.document.document_id,
                chunk_id=row.document.chunk_id,
                rank=row.rank,
                raw_score=row.score,
            )
            for row in bm25_rows
        )
        bm25_channel = _channel_candidates(
            tuple(
                (row.document.document_id, row.document.chunk_id, row.rank, row)
                for row in bm25_rows
            ),
            channel="BM25",
        )

        # RRF fusion（冻结实现；身份 (document_id, chunk_id)）。
        charge(BudgetUsage(rrf_fusions=1))
        rrf_started = time.perf_counter()
        try:
            fused = self._rrf.fuse(dense_channel, bm25_channel)
        except RetrievalAdapterError:
            raise
        except Exception:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.FUSION_FAILED,
                "RRF_FUSION_FAILED",
                "Hybrid RRF fusion failed.",
            ) from None
        rrf_latency_ms = max(0, int((time.perf_counter() - rrf_started) * 1000))

        dense_by_identity = {
            (item.source_id, item.chunk_id): item for item in dense_candidates
        }
        result_candidates: list[RetrievalCandidate] = []
        ranked_facts: list[HybridRankedFact] = []
        document_lookups = 0
        for item in fused:
            candidate = dense_by_identity.get((item.document_id, item.chunk_id))
            dense_rank: int | None = None
            if candidate is not None:
                dense_rank = item.current_rank
            else:
                # BM25-only winner：从 active Dense generation 精确回取权威
                # 正文与 SourceMetadata（§19）；读取按真实 document_reads 计账。
                document_lookups += 1
                charge(BudgetUsage(document_reads=1))
                try:
                    document = self.db_manager.get_chunk_by_identity(
                        item.document_id, item.chunk_id
                    )
                except Exception:
                    raise RetrievalAdapterError(
                        RetrievalErrorCategory.FUSION_FAILED,
                        "RRF_FUSION_FAILED",
                        "BM25-only candidate Dense identity lookup failed.",
                    ) from None
                if document is None:
                    raise RetrievalAdapterError(
                        RetrievalErrorCategory.FUSION_FAILED,
                        "RRF_FUSION_FAILED",
                        "BM25-only candidate Dense identity lookup failed.",
                    )
                try:
                    candidate = self._candidate_from_row(
                        document,
                        None,
                        item.bm25_rank or item.rank,
                        collection=str(
                            getattr(self.db_manager, "collection_name", "")
                        ),
                        default_score=0.0,
                        score_semantics=VectorScoreSemantics.NORMALIZED_RELEVANCE,
                    )
                except RetrievalAdapterError as exc:
                    # metadata 无效 → RRF_FUSION_FAILED（§19），不得伪装成
                    # 普通 METADATA_INVALID 检索失败。
                    if exc.category is RetrievalErrorCategory.METADATA_INVALID:
                        raise RetrievalAdapterError(
                            RetrievalErrorCategory.FUSION_FAILED,
                            "RRF_FUSION_FAILED",
                            "BM25-only candidate Dense metadata is invalid.",
                        ) from None
                    raise
            # §18 mapping：original_rank=Dense rank（若参与）否则 BM25 rank；
            # reranked_rank/reranked_score 承载 fused rank 与 raw RRF score；
            # score 保留合法 Dense score 或 0.0 transport 值（绝不用于 filter）。
            result_candidates.append(
                replace(
                    candidate,
                    original_rank=item.current_rank or item.bm25_rank or item.rank,
                    reranked_rank=item.rank,
                    reranked_score=item.rrf_score,
                )
            )
            ranked_facts.append(
                HybridRankedFact(
                    candidate_id=candidate.candidate_id,
                    dense_channel_rank=dense_rank,
                    bm25_channel_rank=item.bm25_rank,
                )
            )
        return HybridFusionResult(
            candidates=result_candidates,
            bm25_evidence=bm25_evidence,
            ranked_facts=tuple(ranked_facts),
            dense_candidate_count=len(dense_candidates),
            bm25_candidate_count=len(bm25_rows),
            rrf_fused_count=len(fused),
            bm25_latency_ms=bm25_latency_ms,
            rrf_latency_ms=rrf_latency_ms,
            document_lookups=document_lookups,
        )

    def rerank(
        self,
        rewritten_query: str,
        original_query: str,
        candidates: Sequence[RetrievalCandidate],
    ) -> list[RetrievalCandidate]:
        """协议兼容入口：真实 Hybrid 编排走 Service 的 fusion collaborator。

        直接调用本方法（例如测试）时以无记账回调执行完整融合。
        """
        return list(
            self.fuse(rewritten_query, candidates, charge=lambda _usage: None).candidates
        )


__all__ = [
    "HYBRID_CHANNEL_CANDIDATE_LIMIT",
    "HYBRID_RETRIEVAL_STRATEGY",
    "Bm25EvidenceRow",
    "HybridFusionResult",
    "HybridKnowledgeRetrievalAdapter",
    "HybridRankedFact",
]
