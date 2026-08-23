"""冻结的 chunk-level RRF 数学、预算与稳定排序合同。"""

from __future__ import annotations

import math

import pytest

from core.knowledge_base.hybrid_rrf_retriever import (
    BM25_CHANNEL_REF,
    CURRENT_CHANNEL_REF,
    FINAL_FUSED_CANDIDATE_LIMIT,
    HybridRrfRetriever,
    PER_CHANNEL_CANDIDATE_LIMIT,
    PRE_FUSION_UNION_MAX,
    RRF_ALGORITHM_REF,
    RRF_K,
    RrfChannelCandidate,
)


def _candidate(name: str, rank: int) -> RrfChannelCandidate:
    return RrfChannelCandidate(f"doc-{name}", f"chunk-{name}", rank, payload=name)


def test_rrf_contract_is_frozen_before_benchmark() -> None:
    assert RRF_ALGORITHM_REF == "rrf.v1"
    assert RRF_K == 60
    assert PER_CHANNEL_CANDIDATE_LIMIT == 8
    assert PRE_FUSION_UNION_MAX == 16
    assert FINAL_FUSED_CANDIDATE_LIMIT == 8


def test_single_channel_preserves_ranking_and_absent_channel() -> None:
    fused = HybridRrfRetriever().fuse([_candidate("a", 1), _candidate("b", 2)], [])
    assert [item.payload for item in fused] == ["a", "b"]
    assert fused[0].current_rank == 1
    assert fused[0].bm25_rank is None
    assert fused[0].source_channels == (CURRENT_CHANNEL_REF,)


def test_two_channels_same_ranking_uses_exact_formula_and_rank_starts_at_one() -> None:
    current = [_candidate("a", 1), _candidate("b", 2)]
    bm25 = [_candidate("a", 1), _candidate("b", 2)]
    fused = HybridRrfRetriever().fuse(current, bm25)
    assert fused[0].rank == 1
    assert fused[0].rrf_score == 2 / 61
    assert fused[1].rrf_score == 2 / 62
    assert fused[0].source_channels == (CURRENT_CHANNEL_REF, BM25_CHANNEL_REF)


def test_two_channels_inverse_ranking_uses_explicit_stable_tie_break() -> None:
    current = [_candidate("b", 1), _candidate("a", 2)]
    bm25 = [_candidate("a", 1), _candidate("b", 2)]
    fused = HybridRrfRetriever().fuse(current, bm25)
    assert [item.stable_identity for item in fused] == [
        ("doc-a", "chunk-a"),
        ("doc-b", "chunk-b"),
    ]
    assert fused[0].rrf_score == fused[1].rrf_score


def test_candidates_only_in_each_channel_do_not_receive_fabricated_rank() -> None:
    fused = HybridRrfRetriever().fuse([_candidate("current", 1)], [_candidate("bm25", 1)])
    by_payload = {item.payload: item for item in fused}
    assert by_payload["current"].bm25_rank is None
    assert by_payload["bm25"].current_rank is None
    assert by_payload["bm25"].source_channels == (BM25_CHANNEL_REF,)


def test_duplicate_chunk_across_channels_is_one_fused_candidate() -> None:
    fused = HybridRrfRetriever().fuse([_candidate("same", 1)], [_candidate("same", 1)])
    assert len(fused) == 1
    assert fused[0].contributing_channel_count == 2


def test_empty_channels_are_valid() -> None:
    retriever = HybridRrfRetriever()
    assert retriever.fuse([], []) == ()
    assert [item.payload for item in retriever.fuse([], [_candidate("b", 1)])] == ["b"]


def test_final_top_k_is_enforced_after_union() -> None:
    current = [_candidate(f"c-{rank}", rank) for rank in range(1, 9)]
    bm25 = [_candidate(f"b-{rank}", rank) for rank in range(1, 9)]
    fused = HybridRrfRetriever().fuse(current, bm25)
    assert len(fused) == 8
    assert all(math.isfinite(item.rrf_score) for item in fused)


@pytest.mark.parametrize("rank", [0, -1, 1.5, True])
def test_malformed_rank_is_rejected(rank) -> None:
    with pytest.raises(ValueError, match="rank"):
        RrfChannelCandidate("doc", "chunk", rank)


def test_non_contiguous_and_duplicate_channel_candidates_are_rejected() -> None:
    retriever = HybridRrfRetriever()
    with pytest.raises(ValueError, match="contiguous"):
        retriever.fuse([_candidate("a", 2)], [])
    with pytest.raises(ValueError, match="duplicate"):
        retriever.fuse([_candidate("a", 1), _candidate("a", 2)], [])


def test_per_channel_and_final_candidate_budgets_fail_closed() -> None:
    over_budget = [_candidate(str(rank), rank) for rank in range(1, 10)]
    with pytest.raises(ValueError, match="budget"):
        HybridRrfRetriever().fuse(over_budget, [])
    with pytest.raises(ValueError, match="final_top_k"):
        HybridRrfRetriever().fuse([], [], final_top_k=9)


def test_rrf_k_must_be_positive_integer() -> None:
    with pytest.raises(ValueError, match="rrf_k"):
        HybridRrfRetriever(rrf_k=0)
