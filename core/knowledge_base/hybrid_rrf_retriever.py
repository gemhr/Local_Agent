"""固定双通道、chunk-level 的 Reciprocal Rank Fusion。"""

from __future__ import annotations

import math
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

RRF_ALGORITHM_REF = "rrf.v1"
RRF_K = 60
CURRENT_CHANNEL_REF = "current-dense-led-ranked.v1"
BM25_CHANNEL_REF = "bm25-lucene-idf.v1"
PER_CHANNEL_CANDIDATE_LIMIT = 8
PRE_FUSION_UNION_MAX = 16
FINAL_FUSED_CANDIDATE_LIMIT = 8
PROFILE_SCHEMA_VERSION = "localagent-hybrid-rrf-profile.v1"


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


@dataclass(frozen=True, slots=True)
class HybridRrfProfile:
    """evaluation-only 的有界加权 RRF profile。"""

    candidate_id: str
    profile_version: str
    dense_weight: float
    bm25_weight: float
    rrf_k: int = RRF_K
    final_top_k: int = FINAL_FUSED_CANDIDATE_LIMIT
    algorithm_ref: str = "weighted_rrf.v1"
    candidate_profile_sha256: str = ""

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.profile_version:
            raise ValueError("hybrid RRF profile identity must be non-empty")
        for name, value in (("dense_weight", self.dense_weight), ("bm25_weight", self.bm25_weight)):
            if not math.isfinite(value) or value <= 0 or value > 2:
                raise ValueError(f"{name} must be finite and in (0, 2]")
        if not isinstance(self.rrf_k, int) or isinstance(self.rrf_k, bool) or self.rrf_k != RRF_K:
            raise ValueError("hybrid RRF profile must use frozen rrf_k")
        if not isinstance(self.final_top_k, int) or isinstance(self.final_top_k, bool) or not 1 <= self.final_top_k <= FINAL_FUSED_CANDIDATE_LIMIT:
            raise ValueError("hybrid RRF profile final_top_k is invalid")
        expected = self._digest()
        if self.candidate_profile_sha256 and self.candidate_profile_sha256 != expected:
            raise ValueError("candidate_profile_sha256 mismatch")
        object.__setattr__(self, "candidate_profile_sha256", expected)

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "candidate_id": self.candidate_id,
            "profile_version": self.profile_version,
            "algorithm_ref": self.algorithm_ref,
            "rrf_k": self.rrf_k,
            "dense_weight": self.dense_weight,
            "bm25_weight": self.bm25_weight,
            "final_top_k": self.final_top_k,
        }

    def _digest(self) -> str:
        encoded = json.dumps(self._payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {**self._payload(), "candidate_profile_sha256": self.candidate_profile_sha256}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "HybridRrfProfile":
        if payload.get("schema_version") != PROFILE_SCHEMA_VERSION:
            raise ValueError("hybrid RRF profile schema mismatch")
        return cls(
            candidate_id=str(payload["candidate_id"]),
            profile_version=str(payload["profile_version"]),
            algorithm_ref=str(payload.get("algorithm_ref", "weighted_rrf.v1")),
            rrf_k=int(payload.get("rrf_k", RRF_K)),
            dense_weight=float(payload["dense_weight"]),
            bm25_weight=float(payload["bm25_weight"]),
            final_top_k=int(payload.get("final_top_k", FINAL_FUSED_CANDIDATE_LIMIT)),
            candidate_profile_sha256=str(payload.get("candidate_profile_sha256", "")),
        )


def load_hybrid_rrf_profile(path: str | Path) -> HybridRrfProfile:
    """加载并校验 evaluation-only profile。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("hybrid RRF profile must be an object")
    return HybridRrfProfile.from_dict(payload)


class HybridRrfRetriever:
    """消费冻结的 Current/BM25 排名，不混合原始 score。"""

    def __init__(self, *, rrf_k: int = RRF_K, profile: HybridRrfProfile | None = None) -> None:
        if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or rrf_k <= 0:
            raise ValueError("rrf_k must be a positive integer")
        self.rrf_k = rrf_k
        self.profile = profile
        if profile is not None and profile.rrf_k != rrf_k:
            raise ValueError("profile rrf_k mismatch")

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
            dense_weight = self.profile.dense_weight if self.profile is not None else 1.0
            bm25_weight = self.profile.bm25_weight if self.profile is not None else 1.0
            score = 0.0
            if item.current is not None:
                score += dense_weight / (self.rrf_k + item.current.rank)
            if item.bm25 is not None:
                score += bm25_weight / (self.rrf_k + item.bm25.rank)
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
    "PROFILE_SCHEMA_VERSION",
    "HybridRrfProfile",
    "load_hybrid_rrf_profile",
    "RrfChannelCandidate",
    "RrfFusedCandidate",
]
