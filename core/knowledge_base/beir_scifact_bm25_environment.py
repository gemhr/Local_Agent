"""BEIR SciFact 的独立 BM25 sparse index cache。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.knowledge_base.beir_scifact_environment import (
    BEIR_BENCHMARK,
    BEIR_SCIFACT_CORPUS_ID,
    SCIFACT_DATASET,
    materialize_beir_corpus,
    prepare_beir_scifact_chunks,
)
from core.knowledge_base.bm25_sparse_index import (
    BM25_ALGORITHM_REF,
    BM25_B,
    BM25_K1,
    BM25_TOKENIZER_REF,
    Bm25Document,
    Bm25SparseIndex,
)

BM25_CACHE_SCHEMA_VERSION = "beir-scifact-bm25-index-cache.v1"
BM25_CACHE_READY = "READY"


@dataclass(frozen=True, slots=True)
class BeirScifactBm25CacheIdentity:
    cache_key: str
    corpus_sha256: str
    chunk_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class BeirScifactBm25CacheResult:
    cache_dir: Path
    index_path: Path
    manifest_path: Path
    metadata_path: Path
    identity: BeirScifactBm25CacheIdentity
    status: str
    build_elapsed_seconds: float


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _chunk_identities(chunks: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "document_id": str(item["metadata"]["doc_id"]),
            "chunk_id": str(item["metadata"]["chunk_id"]),
            "benchmark_document_id": str(item["metadata"]["benchmark_document_id"]),
            "content_hash": str(item["metadata"]["content_hash"]),
        }
        for item in chunks
    ]


def beir_scifact_bm25_cache_identity(
    *,
    corpus_sha256: str,
    chunk_manifest_sha256: str,
    algorithm_ref: str = BM25_ALGORITHM_REF,
    tokenizer_ref: str = BM25_TOKENIZER_REF,
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> BeirScifactBm25CacheIdentity:
    """计算 sparse index identity；不接受 embedding 或 query-time 参数。"""
    payload = {
        "cache_schema_version": BM25_CACHE_SCHEMA_VERSION,
        "benchmark": BEIR_BENCHMARK,
        "dataset": SCIFACT_DATASET,
        "split": "test",
        "corpus_id": BEIR_SCIFACT_CORPUS_ID,
        "corpus_sha256": corpus_sha256,
        "chunk_manifest_sha256": chunk_manifest_sha256,
        "algorithm_ref": algorithm_ref,
        "tokenizer_ref": tokenizer_ref,
        "k1": k1,
        "b": b,
    }
    return BeirScifactBm25CacheIdentity(
        cache_key=_canonical_sha256(payload),
        corpus_sha256=corpus_sha256,
        chunk_manifest_sha256=chunk_manifest_sha256,
    )


def default_beir_scifact_bm25_cache_root(corpus_jsonl: Path) -> Path:
    corpus = Path(corpus_jsonl).resolve(strict=True)
    return corpus.parents[2] / "cache" / f"{SCIFACT_DATASET}-bm25"


def _expected_metadata(
    identity: BeirScifactBm25CacheIdentity,
    *,
    document_count: int,
    chunk_count: int,
) -> dict[str, object]:
    return {
        "cache_schema_version": BM25_CACHE_SCHEMA_VERSION,
        "cache_status": BM25_CACHE_READY,
        "cache_key": identity.cache_key,
        "benchmark": BEIR_BENCHMARK,
        "dataset": SCIFACT_DATASET,
        "split": "test",
        "corpus_id": BEIR_SCIFACT_CORPUS_ID,
        "corpus_sha256": identity.corpus_sha256,
        "chunk_manifest_sha256": identity.chunk_manifest_sha256,
        "algorithm_ref": BM25_ALGORITHM_REF,
        "tokenizer_ref": BM25_TOKENIZER_REF,
        "k1": BM25_K1,
        "b": BM25_B,
        "document_count": document_count,
        "chunk_count": chunk_count,
    }


def _write_json_atomic(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _assert_metadata(metadata: dict[str, object], expected: dict[str, object]) -> None:
    mismatches = [key for key, value in expected.items() if metadata.get(key) != value]
    if mismatches:
        raise ValueError(
            "BM25_CACHE_INVALID: metadata mismatch: " + ",".join(sorted(mismatches))
        )


def _validate_ready_cache(
    *,
    cache_dir: Path,
    identity: BeirScifactBm25CacheIdentity,
    document_count: int,
    chunk_count: int,
) -> BeirScifactBm25CacheResult:
    index_path = cache_dir / "bm25_index.json"
    manifest_path = cache_dir / "manifest.json"
    metadata_path = cache_dir / "cache_metadata.json"
    if not index_path.is_file() or not manifest_path.is_file() or not metadata_path.is_file():
        raise ValueError("BM25_CACHE_INCOMPLETE: required completion artifacts are missing")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("BM25_CACHE_INVALID: metadata must be an object")
    _assert_metadata(
        metadata,
        _expected_metadata(
            identity, document_count=document_count, chunk_count=chunk_count
        ),
    )
    if metadata.get("index_file_sha256") != _sha256_file(index_path):
        raise ValueError("BM25_CACHE_INVALID: sparse index digest mismatch")
    if metadata.get("manifest_file_sha256") != _sha256_file(manifest_path):
        raise ValueError("BM25_CACHE_INVALID: manifest digest mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("chunk_count") != chunk_count
        or _canonical_sha256(manifest.get("chunks")) != identity.chunk_manifest_sha256
    ):
        raise ValueError("BM25_CACHE_INVALID: chunk manifest mismatch")
    return BeirScifactBm25CacheResult(
        cache_dir=cache_dir,
        index_path=index_path,
        manifest_path=manifest_path,
        metadata_path=metadata_path,
        identity=identity,
        status="CACHE_HIT",
        build_elapsed_seconds=float(metadata.get("build_elapsed_seconds", 0.0)),
    )


def _assert_same_dense_chunks(
    identities: list[dict[str, str]], dense_manifest_path: Path
) -> None:
    dense_manifest = json.loads(dense_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(dense_manifest, dict) or not isinstance(dense_manifest.get("chunks"), list):
        raise ValueError("BM25_DENSE_MANIFEST_INVALID")
    dense_identities = [
        {
            "document_id": str(item["document_id"]),
            "chunk_id": str(item["chunk_id"]),
            "benchmark_document_id": str(item["benchmark_document_id"]),
            "content_hash": str(item["content_hash"]),
        }
        for item in dense_manifest["chunks"]
    ]
    if identities != dense_identities:
        raise ValueError("BM25_CHUNK_MANIFEST_MISMATCH_WITH_DENSE")


def _documents(chunks: list[dict[str, Any]]) -> tuple[Bm25Document, ...]:
    return tuple(
        Bm25Document(
            document_id=str(item["metadata"]["doc_id"]),
            chunk_id=str(item["metadata"]["chunk_id"]),
            text=str(item["page_content"]),
            metadata={
                "benchmark_document_id": str(
                    item["metadata"]["benchmark_document_id"]
                ),
                "content_hash": str(item["metadata"]["content_hash"]),
                "source": str(item["metadata"]["source"]),
                "section_path": str(item["metadata"].get("section_path") or ""),
                "chunk_index": int(item["metadata"].get("chunk_index", 0)),
            },
        )
        for item in chunks
    )


def build_or_reuse_beir_scifact_bm25_cache(
    *,
    corpus_jsonl: Path,
    dense_manifest_path: Path,
    cache_root: Path | None = None,
) -> BeirScifactBm25CacheResult:
    """构建或验证复用 READY sparse cache，并逐项证明与 Dense chunk corpus 相同。"""
    corpus = Path(corpus_jsonl).resolve(strict=True)
    dense_manifest = Path(dense_manifest_path).resolve(strict=True)
    root = (cache_root or default_beir_scifact_bm25_cache_root(corpus)).resolve(
        strict=False
    )
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".beir-scifact-bm25-", dir=root))
    try:
        materialized = staging / "materialized"
        materialize_beir_corpus(corpus, materialized)
        chunks, document_count, _mapping = prepare_beir_scifact_chunks(materialized)
        identities = _chunk_identities(chunks)
        _assert_same_dense_chunks(identities, dense_manifest)
        identity = beir_scifact_bm25_cache_identity(
            corpus_sha256=_sha256_file(corpus),
            chunk_manifest_sha256=_canonical_sha256(identities),
        )
        cache_dir = root / identity.cache_key
        if cache_dir.exists():
            return _validate_ready_cache(
                cache_dir=cache_dir,
                identity=identity,
                document_count=document_count,
                chunk_count=len(chunks),
            )
        lock_path = root / f"{identity.cache_key}.lock"
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise RuntimeError("BM25_CACHE_BUILDING") from error
        os.close(descriptor)
        try:
            if cache_dir.exists():
                return _validate_ready_cache(
                    cache_dir=cache_dir,
                    identity=identity,
                    document_count=document_count,
                    chunk_count=len(chunks),
                )
            build_dir = root / f".{identity.cache_key}.building-{os.getpid()}"
            if build_dir.exists():
                raise RuntimeError("BM25_CACHE_INCOMPLETE: stale build directory")
            build_dir.mkdir()
            build_started = time.monotonic()
            index = Bm25SparseIndex.build(_documents(chunks))
            index.save(build_dir / "bm25_index.json")
            build_elapsed = time.monotonic() - build_started
            _write_json_atomic(
                build_dir / "manifest.json",
                {
                    "corpus_id": BEIR_SCIFACT_CORPUS_ID,
                    "document_count": document_count,
                    "chunk_count": len(chunks),
                    "chunks": identities,
                },
            )
            metadata = _expected_metadata(
                identity, document_count=document_count, chunk_count=len(chunks)
            )
            metadata.update(
                {
                    "build_elapsed_seconds": build_elapsed,
                    "index_file_sha256": _sha256_file(build_dir / "bm25_index.json"),
                    "manifest_file_sha256": _sha256_file(build_dir / "manifest.json"),
                }
            )
            _write_json_atomic(build_dir / "cache_metadata.json", metadata)
            _validate_ready_cache(
                cache_dir=build_dir,
                identity=identity,
                document_count=document_count,
                chunk_count=len(chunks),
            )
            os.replace(build_dir, cache_dir)
            return BeirScifactBm25CacheResult(
                cache_dir=cache_dir,
                index_path=cache_dir / "bm25_index.json",
                manifest_path=cache_dir / "manifest.json",
                metadata_path=cache_dir / "cache_metadata.json",
                identity=identity,
                status="CACHE_BUILT",
                build_elapsed_seconds=build_elapsed,
            )
        finally:
            lock_path.unlink(missing_ok=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def load_beir_scifact_bm25_cache(
    cache_dir: Path,
) -> tuple[Bm25SparseIndex, BeirScifactBm25CacheResult]:
    resolved = Path(cache_dir).resolve(strict=True)
    metadata_path = resolved / "cache_metadata.json"
    if not metadata_path.is_file():
        raise ValueError("BM25_CACHE_INCOMPLETE: cache metadata is missing")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("cache_status") != BM25_CACHE_READY:
        raise ValueError("BM25_CACHE_INCOMPLETE: cache is not READY")
    identity = BeirScifactBm25CacheIdentity(
        cache_key=str(metadata.get("cache_key", "")),
        corpus_sha256=str(metadata.get("corpus_sha256", "")),
        chunk_manifest_sha256=str(metadata.get("chunk_manifest_sha256", "")),
    )
    result = _validate_ready_cache(
        cache_dir=resolved,
        identity=identity,
        document_count=int(metadata.get("document_count", -1)),
        chunk_count=int(metadata.get("chunk_count", -1)),
    )
    index = Bm25SparseIndex.load(result.index_path)
    if index.document_count != int(metadata["chunk_count"]):
        raise ValueError("BM25_CACHE_INVALID: loaded index count mismatch")
    return index, result


__all__ = [
    "BM25_CACHE_SCHEMA_VERSION",
    "BeirScifactBm25CacheIdentity",
    "BeirScifactBm25CacheResult",
    "beir_scifact_bm25_cache_identity",
    "build_or_reuse_beir_scifact_bm25_cache",
    "default_beir_scifact_bm25_cache_root",
    "load_beir_scifact_bm25_cache",
]
