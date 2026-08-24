"""Synthetic WP4 RAG Evaluation corpus 的独立 BM25 sparse index cache。

复用既有 ``Bm25SparseIndex`` build/save/load/search，不重写 sparse retrieval。
identity 绑定同一 source/chunk manifest，并与 Dense cache 逐项 exact match。
"""

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

from core.knowledge_base.bm25_sparse_index import (
    BM25_ALGORITHM_REF,
    BM25_B,
    BM25_K1,
    BM25_TOKENIZER_REF,
    Bm25Document,
    Bm25SparseIndex,
)
from core.knowledge_base.evaluation_environment import (
    BM25_CACHE_SCHEMA_VERSION,
    CACHE_READY,
    CORPUS_ID,
    ordered_chunk_identities,
    ordered_chunk_manifest_digest,
    prepare_evaluation_chunks,
    source_manifest_digest,
)

BM25_CACHE_READY = CACHE_READY


@dataclass(frozen=True, slots=True)
class EvaluationBm25CacheIdentity:
    cache_key: str
    source_manifest_sha256: str
    chunk_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class EvaluationBm25CacheResult:
    cache_dir: Path
    index_path: Path
    manifest_path: Path
    metadata_path: Path
    identity: EvaluationBm25CacheIdentity
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


def evaluation_bm25_cache_identity(
    *,
    source_manifest_sha256: str,
    chunk_manifest_sha256: str,
    algorithm_ref: str = BM25_ALGORITHM_REF,
    tokenizer_ref: str = BM25_TOKENIZER_REF,
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> EvaluationBm25CacheIdentity:
    """计算 synthetic sparse index identity；不接受 embedding / query-time 参数。"""
    payload = {
        "cache_schema_version": BM25_CACHE_SCHEMA_VERSION,
        "corpus_id": CORPUS_ID,
        "source_manifest_sha256": source_manifest_sha256,
        "chunk_manifest_sha256": chunk_manifest_sha256,
        "algorithm_ref": algorithm_ref,
        "tokenizer_ref": tokenizer_ref,
        "k1": k1,
        "b": b,
    }
    return EvaluationBm25CacheIdentity(
        cache_key=_canonical_sha256(payload),
        source_manifest_sha256=source_manifest_sha256,
        chunk_manifest_sha256=chunk_manifest_sha256,
    )


def _expected_metadata(
    identity: EvaluationBm25CacheIdentity,
    *,
    document_count: int,
    chunk_count: int,
) -> dict[str, object]:
    return {
        "cache_schema_version": BM25_CACHE_SCHEMA_VERSION,
        "cache_status": BM25_CACHE_READY,
        "cache_key": identity.cache_key,
        "corpus_id": CORPUS_ID,
        "source_manifest_sha256": identity.source_manifest_sha256,
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
    identity: EvaluationBm25CacheIdentity,
    document_count: int,
    chunk_count: int,
) -> EvaluationBm25CacheResult:
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
        _expected_metadata(identity, document_count=document_count, chunk_count=chunk_count),
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
    return EvaluationBm25CacheResult(
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
            "source": str(item.get("source", "")),
            "section_path": str(item.get("section_path") or ""),
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
                "content_hash": str(item["metadata"]["content_hash"]),
                "source": str(item["metadata"]["source"]),
                "section_path": str(item["metadata"].get("section_path") or ""),
                "chunk_index": int(item["metadata"].get("chunk_index", 0)),
            },
        )
        for item in chunks
    )


def build_or_reuse_evaluation_bm25_cache(
    *,
    corpus_dir: Path,
    dense_manifest_path: Path,
    cache_root: Path,
) -> EvaluationBm25CacheResult:
    """构建或验证复用 synthetic READY sparse cache，并逐项证明与 Dense corpus 相同。

    复用同一 ``prepare_evaluation_chunks`` 产出 exact 同一 ordered chunk identities，
    并与 Dense cache 的 manifest.json 逐项 exact equal。
    """
    root = cache_root.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".rag-eval-bm25-", dir=root))
    try:
        chunks, document_count = prepare_evaluation_chunks(corpus_dir)
        if not chunks:
            raise ValueError("evaluation corpus produced no chunks")
        identities = ordered_chunk_identities(chunks)
        _assert_same_dense_chunks(identities, dense_manifest_path)
        identity = evaluation_bm25_cache_identity(
            source_manifest_sha256=source_manifest_digest(corpus_dir),
            chunk_manifest_sha256=ordered_chunk_manifest_digest(chunks),
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
                    "corpus_id": CORPUS_ID,
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
            return EvaluationBm25CacheResult(
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


def load_evaluation_bm25_cache(
    cache_dir: Path,
    *,
    expected_identity: EvaluationBm25CacheIdentity,
    expected_document_count: int,
    expected_chunk_count: int,
) -> tuple[Bm25SparseIndex, EvaluationBm25CacheResult]:
    """按 external expected identity 加载 READY synthetic BM25 cache（load-by-identity）。

    Authority 顺序：Expected Reality -> Validator -> Artifact -> PASS/FAIL。
    要求 recomputed canonical identity == caller expected identity == directory identity
    == metadata identity 全部 exact；不接收待验证 metadata 自证的 expected identity。
    """
    resolved = Path(cache_dir).resolve(strict=True)
    if resolved.name != expected_identity.cache_key:
        raise ValueError("BM25_CACHE_INVALID: directory identity mismatch")
    recomputed = evaluation_bm25_cache_identity(
        source_manifest_sha256=expected_identity.source_manifest_sha256,
        chunk_manifest_sha256=expected_identity.chunk_manifest_sha256,
        algorithm_ref=BM25_ALGORITHM_REF,
        tokenizer_ref=BM25_TOKENIZER_REF,
        k1=BM25_K1,
        b=BM25_B,
    )
    if recomputed.cache_key != expected_identity.cache_key:
        raise ValueError("BM25_CACHE_INVALID: expected identity is not frozen-canonical")
    metadata_path = resolved / "cache_metadata.json"
    if not metadata_path.is_file():
        raise ValueError("BM25_CACHE_INCOMPLETE: cache metadata is missing")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("cache_status") != BM25_CACHE_READY:
        raise ValueError("BM25_CACHE_INCOMPLETE: cache is not READY")
    result = _validate_ready_cache(
        cache_dir=resolved,
        identity=expected_identity,
        document_count=expected_document_count,
        chunk_count=expected_chunk_count,
    )
    index = Bm25SparseIndex.load(result.index_path)
    if index.document_count != expected_chunk_count:
        raise ValueError("BM25_CACHE_INVALID: loaded index count mismatch")
    return index, result


__all__ = [
    "BM25_CACHE_READY",
    "EvaluationBm25CacheIdentity",
    "EvaluationBm25CacheResult",
    "build_or_reuse_evaluation_bm25_cache",
    "evaluation_bm25_cache_identity",
    "load_evaluation_bm25_cache",
]
