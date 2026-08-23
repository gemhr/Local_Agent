"""固定双通道、chunk-level 的 Reciprocal Rank Fusion。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

RRF_ALGORITHM_REF = "rrf.v1"
RRF_K = 60
CURRENT_CHANNEL_REF = "current-dense-led-ranked.v1"
BM25_CHANNEL_REF = "bm25-lucene-idf.v1"
PER_CHANNEL_CANDIDATE_LIMIT = 8
PRE_FUSION_UNION_MAX = 16
FINAL_FUSED_CANDIDATE_LIMIT = 8


@dataclass(frozen=True, slots=True)
class RrfChannelCandidate:
    """一个已排序通道中的稳定 chunk candidate。"""

    document_id: str
    chunk_id: str
    rank: int
    payload: Any = None

    def __post_init__(self) -> None:
        if not self.document_id or not self.chunk_id:
            raise ValueError("RRF candidate identity must be non-empty")
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank <= 0:
            raise ValueError("RRF candidate rank must be a positive integer")

    @property
    def stable_identity(self) -> tuple[str, str]:
        return self.document_id, self.chunk_id


@dataclass(frozen=True, slots=True)
class RrfFusedCandidate:
    """RRF 最终排名及可审计的双通道 rank provenance。"""

    document_id: str
    chunk_id: str
    rank: int
    rrf_score: float
    current_rank: int | None
    bm25_rank: int | None
    source_channels: tuple[str, ...]
    payload: Any

    def __post_init__(self) -> None:
        if not math.isfinite(self.rrf_score) or self.rrf_score <= 0:
            raise ValueError("RRF fused score must be finite and positive")

    @property
    def stable_identity(self) -> tuple[str, str]:
        return self.document_id, self.chunk_id

    @property
    def contributing_channel_count(self) -> int:
        return len(self.source_channels)


@dataclass(slots=True)
class _Accumulator:
    current: RrfChannelCandidate | None = None
    bm25: RrfChannelCandidate | None = None


class HybridRrfRetriever:
    """消费冻结的 Current/BM25 排名，不混合原始 score。"""

    def __init__(self, *, rrf_k: int = RRF_K) -> None:
        if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or rrf_k <= 0:
            raise ValueError("rrf_k must be a positive integer")
        self.rrf_k = rrf_k

    @staticmethod
    def _validate_channel(
        candidates: Iterable[RrfChannelCandidate], *, name: str
    ) -> tuple[RrfChannelCandidate, ...]:
        ordered = tuple(candidates)
        if len(ordered) > PER_CHANNEL_CANDIDATE_LIMIT:
            raise ValueError(f"{name} candidate budget exceeded")
        identities = [item.stable_identity for item in ordered]
        if len(set(identities)) != len(identities):
            raise ValueError(f"duplicate candidate within {name} channel")
        ranks = [item.rank for item in ordered]
        if ranks != list(range(1, len(ordered) + 1)):
            raise ValueError(f"{name} ranks must be contiguous and start at 1")
        return ordered

    def fuse(
        self,
        current_candidates: Iterable[RrfChannelCandidate],
        bm25_candidates: Iterable[RrfChannelCandidate],
        *,
        final_top_k: int = FINAL_FUSED_CANDIDATE_LIMIT,
    ) -> tuple[RrfFusedCandidate, ...]:
        """按 chunk identity 对齐并执行固定双通道 RRF。"""
        if (
            isinstance(final_top_k, bool)
            or not isinstance(final_top_k, int)
            or final_top_k <= 0
            or final_top_k > FINAL_FUSED_CANDIDATE_LIMIT
        ):
            raise ValueError("RRF final_top_k must be between 1 and 8")
        current = self._validate_channel(current_candidates, name="current")
        bm25 = self._validate_channel(bm25_candidates, name="bm25")
        union: dict[tuple[str, str], _Accumulator] = {}
        for candidate in current:
            union.setdefault(candidate.stable_identity, _Accumulator()).current = candidate
        for candidate in bm25:
            union.setdefault(candidate.stable_identity, _Accumulator()).bm25 = candidate
        if len(union) > PRE_FUSION_UNION_MAX:  # defensive assertion of the frozen budget
            raise ValueError("RRF pre-fusion union budget exceeded")

        scored: list[tuple[float, int, int, tuple[str, str], _Accumulator]] = []
        for identity, item in union.items():
            ranks = tuple(
                candidate.rank
                for candidate in (item.current, item.bm25)
                if candidate is not None
            )
            score = sum(1.0 / (self.rrf_k + rank) for rank in ranks)
            if not math.isfinite(score) or score <= 0:
                raise ValueError("RRF fused score must be finite and positive")
            scored.append((score, min(ranks), len(ranks), identity, item))
        scored.sort(key=lambda value: (-value[0], value[1], -value[2], value[3]))

        fused = []
        for rank, (score, _best_rank, _count, identity, item) in enumerate(
            scored[:final_top_k], 1
        ):
            channels = tuple(
                channel
                for channel, candidate in (
                    (CURRENT_CHANNEL_REF, item.current),
                    (BM25_CHANNEL_REF, item.bm25),
                )
                if candidate is not None
            )
            payload = item.current.payload if item.current is not None else item.bm25.payload
            fused.append(
                RrfFusedCandidate(
                    document_id=identity[0],
                    chunk_id=identity[1],
                    rank=rank,
                    rrf_score=score,
                    current_rank=item.current.rank if item.current is not None else None,
                    bm25_rank=item.bm25.rank if item.bm25 is not None else None,
                    source_channels=channels,
                    payload=payload,
                )
            )
        return tuple(fused)


__all__ = [
    "BM25_CHANNEL_REF",
    "CURRENT_CHANNEL_REF",
    "FINAL_FUSED_CANDIDATE_LIMIT",
    "HybridRrfRetriever",
    "PER_CHANNEL_CANDIDATE_LIMIT",
    "PRE_FUSION_UNION_MAX",
    "RRF_ALGORITHM_REF",
    "RRF_K",
    "RrfChannelCandidate",
    "RrfFusedCandidate",
]
