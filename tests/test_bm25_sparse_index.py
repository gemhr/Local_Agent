"""BM25 数学与 tokenizer 冻结合同。"""

from __future__ import annotations

import math

import pytest

from core.knowledge_base.bm25_sparse_index import (
    BM25_K1,
    Bm25Document,
    Bm25SparseIndex,
    bm25_tokenize,
)


def _document(identity: str, text: str) -> Bm25Document:
    return Bm25Document(identity, f"chunk-{identity}", text, {})


def _score(*texts: str, query: str) -> tuple[float, ...]:
    index = Bm25SparseIndex.build(
        _document(str(position), text) for position, text in enumerate(texts, 1)
    )
    return tuple(item.score for item in index.search(query, top_k=len(texts)))


def test_empty_corpus_and_empty_query_return_no_results() -> None:
    assert Bm25SparseIndex.build(()).search("term", top_k=1) == ()
    assert Bm25SparseIndex.build((_document("1", "term"),)).search("", top_k=1) == ()


def test_single_document_term_absent_and_present_formula() -> None:
    index = Bm25SparseIndex.build((_document("1", "alpha beta"),))
    assert index.search("missing", top_k=1) == ()
    result = index.search("alpha", top_k=1)[0]
    expected_idf = math.log(1.0 + 0.5 / 1.5)
    assert result.score == pytest.approx(expected_idf)


def test_multiple_tf_matches_frozen_formula() -> None:
    scores = _score("alpha alpha beta", "beta gamma gamma", query="alpha")
    idf = math.log(1.0 + (2 - 1 + 0.5) / (1 + 0.5))
    expected = idf * (2 * (BM25_K1 + 1)) / (2 + BM25_K1)
    assert scores == pytest.approx((expected,))


def test_document_length_normalization_prefers_shorter_document() -> None:
    results = Bm25SparseIndex.build(
        (_document("short", "alpha"), _document("long", "alpha beta gamma delta"))
    ).search("alpha", top_k=2)
    assert [item.document.document_id for item in results] == ["short", "long"]
    assert results[0].score > results[1].score


def test_idf_increases_when_term_is_rarer() -> None:
    index = Bm25SparseIndex.build(
        (_document("1", "common rare"), _document("2", "common"), _document("3", "common"))
    )
    common = index.search("common", top_k=3)[0].score
    rare = index.search("rare", top_k=1)[0].score
    assert rare > common


def test_deterministic_tie_break_uses_stable_chunk_identity() -> None:
    index = Bm25SparseIndex.build(
        (_document("b", "alpha"), _document("a", "alpha"))
    )
    assert [item.document.document_id for item in index.search("alpha", top_k=2)] == [
        "a",
        "b",
    ]


def test_scores_are_finite_and_top_k_is_enforced() -> None:
    index = Bm25SparseIndex.build(
        tuple(_document(str(index), "alpha " * 1000) for index in range(10))
    )
    results = index.search("alpha", top_k=3)
    assert len(results) == 3
    assert all(math.isfinite(item.score) for item in results)


def test_duplicate_query_terms_repeat_contribution() -> None:
    index = Bm25SparseIndex.build((_document("1", "alpha"),))
    single = index.search("alpha", top_k=1)[0].score
    duplicate = index.search("alpha alpha", top_k=1)[0].score
    assert duplicate == pytest.approx(single * 2)


def test_unicode_tokenizer_contract() -> None:
    assert bm25_tokenize("Ｑwen3-Embedding-0.6B API_KEY 中文") == (
        "qwen3-embedding-0.6b",
        "api_key",
        "中",
        "文",
    )


def test_invalid_top_k_is_rejected() -> None:
    with pytest.raises(ValueError, match="top_k"):
        Bm25SparseIndex.build((_document("1", "alpha"),)).search("alpha", top_k=0)
