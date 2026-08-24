"""Synthetic WP4 BM25 cache identity 与 READY lifecycle（fail-closed + Dense exact-match）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.knowledge_base import evaluation_environment as ee
from core.knowledge_base.bm25_sparse_index import (
    BM25_ALGORITHM_REF,
    BM25_TOKENIZER_REF,
)
from core.knowledge_base.evaluation_bm25_environment import (
    BM25_CACHE_READY,
    build_or_reuse_evaluation_bm25_cache,
    evaluation_bm25_cache_identity,
    load_evaluation_bm25_cache,
)


def _dense_manifest(root: Path, corpus_dir: Path) -> Path:
    chunks, document_count = ee.prepare_evaluation_chunks(corpus_dir)
    path = root / "dense-manifest.json"
    path.write_text(
        json.dumps(
            {
                "corpus_id": ee.CORPUS_ID,
                "document_count": document_count,
                "chunk_count": len(chunks),
                "chunks": ee.ordered_chunk_identities(chunks),
            }
        ),
        encoding="utf-8",
    )
    return path


def _identity(**overrides) -> ee.EvaluationBm25CacheIdentity:
    base = {"source_manifest_sha256": "src-a", "chunk_manifest_sha256": "chunks-a"}
    base.update(overrides)
    return evaluation_bm25_cache_identity(**base)


def test_bm25_cache_identity_contract() -> None:
    baseline = _identity()
    assert baseline == _identity()
    assert baseline.cache_key != _identity(k1=1.3).cache_key
    assert baseline.cache_key != _identity(b=0.6).cache_key
    assert baseline.cache_key != _identity(tokenizer_ref="tokenizer-v2").cache_key
    assert baseline.cache_key != _identity(algorithm_ref="bm25-v2").cache_key
    assert baseline.cache_key != _identity(source_manifest_sha256="src-b").cache_key
    assert baseline.cache_key != _identity(chunk_manifest_sha256="chunks-b").cache_key


def test_bm25_cold_build_warm_reuse_and_load(tmp_path: Path) -> None:
    corpus_dir = ee.default_corpus_dir()
    dense_manifest = _dense_manifest(tmp_path, corpus_dir)
    cache_root = tmp_path / "bm25-cache"

    built = build_or_reuse_evaluation_bm25_cache(
        corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=cache_root
    )
    reused = build_or_reuse_evaluation_bm25_cache(
        corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=cache_root
    )
    index, loaded = load_evaluation_bm25_cache(
        built.cache_dir,
        expected_identity=built.identity,
        expected_document_count=15,
        expected_chunk_count=60,
    )

    assert built.status == "CACHE_BUILT"
    assert reused.status == "CACHE_HIT"
    assert reused.identity == built.identity == loaded.identity
    assert index.document_count == 60
    metadata = json.loads(built.metadata_path.read_text(encoding="utf-8"))
    assert metadata["cache_status"] == BM25_CACHE_READY
    assert metadata["algorithm_ref"] == BM25_ALGORITHM_REF
    assert metadata["tokenizer_ref"] == BM25_TOKENIZER_REF


def test_bm25_dense_manifest_exact_match(tmp_path: Path) -> None:
    corpus_dir = ee.default_corpus_dir()
    chunks, _ = ee.prepare_evaluation_chunks(corpus_dir)
    dense_digest = ee.ordered_chunk_manifest_digest(chunks)
    dense_manifest = _dense_manifest(tmp_path, corpus_dir)
    result = build_or_reuse_evaluation_bm25_cache(
        corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=tmp_path / "bm25-cache"
    )
    assert result.identity.chunk_manifest_sha256 == dense_digest
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["chunks"] == ee.ordered_chunk_identities(chunks)


def test_bm25_partial_cache_is_rejected(tmp_path: Path) -> None:
    corpus_dir = ee.default_corpus_dir()
    dense_manifest = _dense_manifest(tmp_path, corpus_dir)
    result = build_or_reuse_evaluation_bm25_cache(
        corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=tmp_path / "bm25-cache"
    )
    result.index_path.unlink()
    with pytest.raises(ValueError, match="BM25_CACHE_INCOMPLETE"):
        build_or_reuse_evaluation_bm25_cache(
            corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=tmp_path / "bm25-cache"
        )


def test_bm25_stale_metadata_fails_closed(tmp_path: Path) -> None:
    corpus_dir = ee.default_corpus_dir()
    dense_manifest = _dense_manifest(tmp_path, corpus_dir)
    result = build_or_reuse_evaluation_bm25_cache(
        corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=tmp_path / "bm25-cache"
    )
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    metadata["chunk_manifest_sha256"] = "wrong"
    result.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata mismatch"):
        build_or_reuse_evaluation_bm25_cache(
            corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=tmp_path / "bm25-cache"
        )


def test_bm25_dense_chunk_manifest_mismatch_fails_closed(tmp_path: Path) -> None:
    corpus_dir = ee.default_corpus_dir()
    dense_manifest = _dense_manifest(tmp_path, corpus_dir)
    payload = json.loads(dense_manifest.read_text(encoding="utf-8"))
    payload["chunks"][0]["chunk_id"] = "different"
    dense_manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="BM25_CHUNK_MANIFEST_MISMATCH_WITH_DENSE"):
        build_or_reuse_evaluation_bm25_cache(
            corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=tmp_path / "bm25-cache"
        )


def test_bm25_manifest_digest_mismatch_fails_closed(tmp_path: Path) -> None:
    corpus_dir = ee.default_corpus_dir()
    dense_manifest = _dense_manifest(tmp_path, corpus_dir)
    result = build_or_reuse_evaluation_bm25_cache(
        corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=tmp_path / "bm25-cache"
    )
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["chunk_count"] = 59
    result.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="BM25_CACHE_INVALID: manifest digest mismatch"):
        build_or_reuse_evaluation_bm25_cache(
            corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=tmp_path / "bm25-cache"
        )


def test_bm25_wrong_k1_identity_rejects_stale_cache(tmp_path: Path) -> None:
    corpus_dir = ee.default_corpus_dir()
    dense_manifest = _dense_manifest(tmp_path, corpus_dir)
    result = build_or_reuse_evaluation_bm25_cache(
        corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=tmp_path / "bm25-cache"
    )
    # A cache built with different k1 would map to a different identity dir; the
    # current identity must still resolve and warm-hit only for the same key.
    assert result.identity.cache_key == evaluation_bm25_cache_identity(
        source_manifest_sha256=result.identity.source_manifest_sha256,
        chunk_manifest_sha256=result.identity.chunk_manifest_sha256,
    ).cache_key
    assert result.identity.cache_key != evaluation_bm25_cache_identity(
        source_manifest_sha256=result.identity.source_manifest_sha256,
        chunk_manifest_sha256=result.identity.chunk_manifest_sha256,
        k1=1.3,
    ).cache_key


# ---------------------------------------------------------------------------
# P1-03：BM25 load-by-identity forgery regressions（external expected identity）
# ---------------------------------------------------------------------------


def test_bm25_forged_self_described_identity_rejected(tmp_path: Path) -> None:
    """metadata cache_key/source digest 同步改成 self-consistent fake → external authority reject。"""
    corpus_dir = ee.default_corpus_dir()
    dense_manifest = _dense_manifest(tmp_path, corpus_dir)
    result = build_or_reuse_evaluation_bm25_cache(
        corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=tmp_path / "bm25-cache"
    )
    forged = evaluation_bm25_cache_identity(
        source_manifest_sha256="forged-src", chunk_manifest_sha256="forged-chunk"
    )
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    metadata["cache_key"] = forged.cache_key
    metadata["source_manifest_sha256"] = forged.source_manifest_sha256
    metadata["chunk_manifest_sha256"] = forged.chunk_manifest_sha256
    result.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata mismatch"):
        load_evaluation_bm25_cache(
            result.cache_dir,
            expected_identity=result.identity,
            expected_document_count=15,
            expected_chunk_count=60,
        )


def test_bm25_wrong_caller_expected_identity_rejected(tmp_path: Path) -> None:
    corpus_dir = ee.default_corpus_dir()
    dense_manifest = _dense_manifest(tmp_path, corpus_dir)
    result = build_or_reuse_evaluation_bm25_cache(
        corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=tmp_path / "bm25-cache"
    )
    wrong = evaluation_bm25_cache_identity(
        source_manifest_sha256="src-b", chunk_manifest_sha256="chunks-b"
    )
    with pytest.raises(ValueError, match="directory identity mismatch"):
        load_evaluation_bm25_cache(
            result.cache_dir,
            expected_identity=wrong,
            expected_document_count=15,
            expected_chunk_count=60,
        )


def test_bm25_directory_identity_mismatch_rejected(tmp_path: Path) -> None:
    corpus_dir = ee.default_corpus_dir()
    dense_manifest = _dense_manifest(tmp_path, corpus_dir)
    result = build_or_reuse_evaluation_bm25_cache(
        corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=tmp_path / "bm25-cache"
    )
    import shutil

    moved = tmp_path / "renamed-bm25-dir"
    shutil.move(str(result.cache_dir), str(moved))
    with pytest.raises(ValueError, match="directory identity mismatch"):
        load_evaluation_bm25_cache(
            moved,
            expected_identity=result.identity,
            expected_document_count=15,
            expected_chunk_count=60,
        )


def test_bm25_forged_source_digest_with_recomputed_identity_rejected(tmp_path: Path) -> None:
    """source manifest digest 被修改 + metadata identity 同步重算 → external Authority reject。"""
    corpus_dir = ee.default_corpus_dir()
    dense_manifest = _dense_manifest(tmp_path, corpus_dir)
    result = build_or_reuse_evaluation_bm25_cache(
        corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=tmp_path / "bm25-cache"
    )
    forged = evaluation_bm25_cache_identity(
        source_manifest_sha256="forged-source", chunk_manifest_sha256=result.identity.chunk_manifest_sha256
    )
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    metadata["source_manifest_sha256"] = forged.source_manifest_sha256
    metadata["cache_key"] = forged.cache_key
    result.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    # 即使 metadata self-consistent，external expected identity 不变 → reject。
    with pytest.raises(ValueError, match="metadata mismatch"):
        load_evaluation_bm25_cache(
            result.cache_dir,
            expected_identity=result.identity,
            expected_document_count=15,
            expected_chunk_count=60,
        )


def test_bm25_wrong_chunk_manifest_rejected_on_load(tmp_path: Path) -> None:
    corpus_dir = ee.default_corpus_dir()
    dense_manifest = _dense_manifest(tmp_path, corpus_dir)
    result = build_or_reuse_evaluation_bm25_cache(
        corpus_dir=corpus_dir, dense_manifest_path=dense_manifest, cache_root=tmp_path / "bm25-cache"
    )
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["chunks"][0]["chunk_id"] = "c-changed"
    result.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    metadata["manifest_file_sha256"] = _sha256(result.manifest_path)
    result.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="BM25_CACHE_INVALID: chunk manifest mismatch"):
        load_evaluation_bm25_cache(
            result.cache_dir,
            expected_identity=result.identity,
            expected_document_count=15,
            expected_chunk_count=60,
        )


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
