"""SciFact BM25 sparse cache identity 与 READY lifecycle。"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from core.knowledge_base.beir_scifact_bm25_environment import (
    BM25_CACHE_SCHEMA_VERSION,
    beir_scifact_bm25_cache_identity,
    build_or_reuse_beir_scifact_bm25_cache,
    load_beir_scifact_bm25_cache,
)
from core.knowledge_base.beir_scifact_environment import (
    materialize_beir_corpus,
    prepare_beir_scifact_chunks,
)
from core.knowledge_base.bm25_sparse_index import BM25_ALGORITHM_REF, BM25_TOKENIZER_REF


def _write_tiny_corpus(path: Path) -> None:
    rows = [
        {"_id": "1", "title": "Alpha", "text": "alpha beta beta"},
        {"_id": "2", "title": "Gamma", "text": "gamma delta"},
    ]
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _dense_manifest(corpus: Path, root: Path) -> Path:
    materialized = root / "dense-materialized"
    materialize_beir_corpus(corpus, materialized)
    chunks, document_count, _mapping = prepare_beir_scifact_chunks(materialized)
    payload = {
        "document_count": document_count,
        "chunk_count": len(chunks),
        "chunks": [
            {
                "document_id": item["metadata"]["doc_id"],
                "chunk_id": item["metadata"]["chunk_id"],
                "benchmark_document_id": item["metadata"]["benchmark_document_id"],
                "content_hash": item["metadata"]["content_hash"],
            }
            for item in chunks
        ],
    }
    path = root / "dense-manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_sparse_cache_identity_contract_and_invalidation() -> None:
    baseline = beir_scifact_bm25_cache_identity(
        corpus_sha256="corpus", chunk_manifest_sha256="chunks"
    )
    assert baseline == beir_scifact_bm25_cache_identity(
        corpus_sha256="corpus", chunk_manifest_sha256="chunks"
    )
    assert baseline.cache_key != beir_scifact_bm25_cache_identity(
        corpus_sha256="corpus", chunk_manifest_sha256="chunks", k1=1.3
    ).cache_key
    assert baseline.cache_key != beir_scifact_bm25_cache_identity(
        corpus_sha256="corpus", chunk_manifest_sha256="chunks", tokenizer_ref="tokenizer-v2"
    ).cache_key
    assert baseline.cache_key != beir_scifact_bm25_cache_identity(
        corpus_sha256="corpus", chunk_manifest_sha256="chunks", algorithm_ref="bm25-v2"
    ).cache_key


def test_embedding_and_candidate_limit_are_not_cache_identity_inputs() -> None:
    parameters = inspect.signature(beir_scifact_bm25_cache_identity).parameters
    assert "embedding_model" not in parameters
    assert "embedding_dimension" not in parameters
    assert "candidate_limit" not in parameters


def test_ready_cache_is_reused_and_loads(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.jsonl"
    _write_tiny_corpus(corpus)
    dense_manifest = _dense_manifest(corpus, tmp_path)
    cache_root = tmp_path / "sparse-cache"

    built = build_or_reuse_beir_scifact_bm25_cache(
        corpus_jsonl=corpus,
        dense_manifest_path=dense_manifest,
        cache_root=cache_root,
    )
    reused = build_or_reuse_beir_scifact_bm25_cache(
        corpus_jsonl=corpus,
        dense_manifest_path=dense_manifest,
        cache_root=cache_root,
    )
    index, loaded = load_beir_scifact_bm25_cache(built.cache_dir)

    assert built.status == "CACHE_BUILT"
    assert reused.status == "CACHE_HIT"
    assert reused.identity == built.identity == loaded.identity
    assert index.document_count == 2
    assert index.search("beta", top_k=1)[0].document.metadata["benchmark_document_id"] == "1"
    metadata = json.loads(built.metadata_path.read_text(encoding="utf-8"))
    assert metadata["cache_schema_version"] == BM25_CACHE_SCHEMA_VERSION
    assert metadata["algorithm_ref"] == BM25_ALGORITHM_REF
    assert metadata["tokenizer_ref"] == BM25_TOKENIZER_REF


def test_partial_cache_is_rejected(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.jsonl"
    _write_tiny_corpus(corpus)
    dense_manifest = _dense_manifest(corpus, tmp_path)
    result = build_or_reuse_beir_scifact_bm25_cache(
        corpus_jsonl=corpus,
        dense_manifest_path=dense_manifest,
        cache_root=tmp_path / "sparse-cache",
    )
    result.index_path.unlink()
    with pytest.raises(ValueError, match="BM25_CACHE_INCOMPLETE"):
        build_or_reuse_beir_scifact_bm25_cache(
            corpus_jsonl=corpus,
            dense_manifest_path=dense_manifest,
            cache_root=tmp_path / "sparse-cache",
        )


def test_dense_chunk_manifest_mismatch_fails_closed(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.jsonl"
    _write_tiny_corpus(corpus)
    dense_manifest = _dense_manifest(corpus, tmp_path)
    payload = json.loads(dense_manifest.read_text(encoding="utf-8"))
    payload["chunks"][0]["chunk_id"] = "different"
    dense_manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="BM25_CHUNK_MANIFEST_MISMATCH_WITH_DENSE"):
        build_or_reuse_beir_scifact_bm25_cache(
            corpus_jsonl=corpus,
            dense_manifest_path=dense_manifest,
            cache_root=tmp_path / "sparse-cache",
        )
