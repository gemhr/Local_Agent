"""Stage5 Phase3 可重建 RAG Evaluation KB 环境。"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.knowledge_base.document_loader import (
    iter_supported_files,
    load_document_file,
    split_documents,
)
from core.knowledge_base.vector_db_manager import VectorDBManager

CORPUS_ID = "rag-evaluation-corpus.v1"
COLLECTION_NAME = "rag_evaluation_kb_v1"
TRUTHFULNESS_LABEL = "SYNTHETIC_RAG_EVALUATION_CORPUS"
CHUNK_SIZE = 1400
CHUNK_OVERLAP = 180
INGEST_BATCH_ID = CORPUS_ID
EMBEDDING_MODEL_NAME = "Qwen3-Embedding-0.6B"
PURPOSE = "evaluation"

# Frozen Dense semantic facts（WP4 Phase A）。identity 绑定这些值，build 时显式复核。
DENSE_DIMENSION = 1024
DENSE_LOCAL_FILES_ONLY = True
DENSE_NORMALIZE_EMBEDDINGS = True
DENSE_QUERY_PROMPT_NAME = ""
SPLITTER_IDENTITY = "structure-aware-splitter.v2"

# Synthetic WP4 cache schema；禁止复制 SciFact 的 benchmark/dataset/split 硬编码。
DENSE_CACHE_SCHEMA_VERSION = "rag-evaluation-dense-index-cache.v1"
BM25_CACHE_SCHEMA_VERSION = "rag-evaluation-bm25-index-cache.v1"
CACHE_READY = "READY"
CACHE_BUILDING = "BUILDING"


@dataclass(frozen=True, slots=True)
class EvaluationKbManifest:
    corpus_id: str
    collection_name: str
    document_count: int
    chunk_count: int
    embedding_model_name: str
    embedding_dimension: int
    chunk_size: int
    chunk_overlap: int
    chunks: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_id": self.corpus_id,
            "collection_name": self.collection_name,
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
            "embedding_model_name": self.embedding_model_name,
            "embedding_dimension": self.embedding_dimension,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "chunks": list(self.chunks),
        }


def default_corpus_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "evaluation_assets" / "rag_kb_v1" / "documents"


def prepare_evaluation_chunks(corpus_dir: Path) -> tuple[list[dict[str, Any]], int]:
    root = corpus_dir.resolve(strict=True)
    files = list(iter_supported_files(str(root)))
    chunks: list[dict[str, Any]] = []
    for path in files:
        documents = load_document_file(path, root)
        chunks.extend(
            split_documents(
                documents,
                chunk_size=CHUNK_SIZE,
                chunk_overlap=CHUNK_OVERLAP,
                ingest_batch_id=INGEST_BATCH_ID,
            )
        )
    return chunks, len(files)


def _assert_fresh_persistence(persist_dir: Path) -> None:
    resolved = persist_dir.resolve(strict=False)
    if resolved.exists() and any(resolved.iterdir()):
        raise ValueError("evaluation persistence must be new or empty")


def build_evaluation_kb(
    *,
    persist_dir: Path,
    embedding_model_path: Path,
    corpus_dir: Path | None = None,
    embedding_batch_size: int = 8,
    query_prompt_name: str | None = None,
) -> tuple[VectorDBManager, EvaluationKbManifest]:
    """只在 fresh persistence 中构建专用 Evaluation collection。"""
    _assert_fresh_persistence(persist_dir)
    chunks, document_count = prepare_evaluation_chunks(corpus_dir or default_corpus_dir())
    if not chunks:
        raise ValueError("evaluation corpus produced no chunks")
    manager = VectorDBManager(
        db_persist_dir=str(persist_dir),
        local_model_path=str(embedding_model_path),
        collection_name=COLLECTION_NAME,
        ingest_batch_size=32,
        embedding_batch_size=embedding_batch_size,
        query_prompt_name=query_prompt_name,
    )
    written = manager.ingest_chunks(chunks)
    if written != len(chunks):
        raise RuntimeError("evaluation KB ingest count mismatch")
    manager.publish_collection_marker()
    collection = manager.vector_store._collection
    metadata = dict(collection.metadata or {})
    dimension = manager.embedding_dimension()
    metadata.update(
        {
            "purpose": PURPOSE,
            "corpus_id": CORPUS_ID,
            "truthfulness_label": TRUTHFULNESS_LABEL,
            "embedding_model": EMBEDDING_MODEL_NAME,
            "embedding_dimension": dimension,
            "chunk_size": CHUNK_SIZE,
            "chunk_overlap": CHUNK_OVERLAP,
        }
    )
    collection.modify(metadata=metadata)
    identities = tuple(
        {
            "document_id": str(item["metadata"]["doc_id"]),
            "chunk_id": str(item["metadata"]["chunk_id"]),
            "source": str(item["metadata"]["source"]),
            "section_path": str(item["metadata"].get("section_path") or ""),
            "content_hash": str(item["metadata"]["content_hash"]),
        }
        for item in chunks
    )
    return manager, EvaluationKbManifest(
        corpus_id=CORPUS_ID,
        collection_name=COLLECTION_NAME,
        document_count=document_count,
        chunk_count=len(chunks),
        embedding_model_name=EMBEDDING_MODEL_NAME,
        embedding_dimension=dimension,
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        chunks=identities,
    )


def manifest_json(manifest: EvaluationKbManifest) -> str:
    return json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# Canonical corpus / chunk manifest digests（WP4 Phase A substrate identity）
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def source_manifest_digest(corpus_dir: Path) -> str:
    """canonical source-manifest digest：按 relative source path 稳定排序，
    对每个文件 content digest 机械计算；不依赖 absolute path / mtime / 偶然顺序。"""
    root = corpus_dir.resolve(strict=True)
    files = sorted(
        iter_supported_files(str(root)),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    entries = [
        {"path": path.relative_to(root).as_posix(), "sha256": _sha256_file(path)}
        for path in files
    ]
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ordered_chunk_identities(chunks: list[dict[str, Any]]) -> list[dict[str, str]]:
    """deterministic ordered chunk identity；Dense/BM25 必须使用 exact 同一序列。"""
    return [
        {
            "document_id": str(item["metadata"]["doc_id"]),
            "chunk_id": str(item["metadata"]["chunk_id"]),
            "source": str(item["metadata"]["source"]),
            "section_path": str(item["metadata"].get("section_path") or ""),
            "content_hash": str(item["metadata"]["content_hash"]),
        }
        for item in chunks
    ]


def ordered_chunk_manifest_digest(chunks: list[dict[str, Any]]) -> str:
    """ordered chunk-manifest digest：绑定 deterministic chunk identity 序列。"""
    return _identity_list_digest(ordered_chunk_identities(chunks))


def _identity_list_digest(identities: list[dict[str, Any]]) -> str:
    """对有序 identity-dict 列表计算 canonical digest（load 时从 manifest content 重算）。"""
    canonical = json.dumps(
        list(identities),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EvaluationDenseCacheIdentity:
    """synthetic Dense cache identity；只含改变 vector 语义的输入。"""

    cache_key: str
    source_manifest_sha256: str
    chunk_manifest_sha256: str
    embedding_dimension: int


@dataclass(frozen=True, slots=True)
class EvaluationDenseCacheResult:
    """synthetic Dense READY cache 的位置与不可变 provenance。"""

    cache_dir: Path
    chroma_dir: Path
    manifest_path: Path
    metadata_path: Path
    identity: EvaluationDenseCacheIdentity
    status: str
    elapsed_seconds: float


def evaluation_dense_cache_identity(
    *,
    source_manifest_sha256: str,
    chunk_manifest_sha256: str,
    embedding_model: str = EMBEDDING_MODEL_NAME,
    embedding_dimension: int = DENSE_DIMENSION,
    embedding_local_files_only: bool = DENSE_LOCAL_FILES_ONLY,
    normalize_embeddings: bool = DENSE_NORMALIZE_EMBEDDINGS,
    embedding_query_prompt: str = DENSE_QUERY_PROMPT_NAME,
    splitter_identity: str = SPLITTER_IDENTITY,
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
    collection_name: str = COLLECTION_NAME,
) -> EvaluationDenseCacheIdentity:
    """计算 synthetic Dense index 语义 identity；query-time / batch 参数不进入 key。"""
    payload = {
        "cache_schema_version": DENSE_CACHE_SCHEMA_VERSION,
        "corpus_id": CORPUS_ID,
        "source_manifest_sha256": source_manifest_sha256,
        "chunk_manifest_sha256": chunk_manifest_sha256,
        "embedding_model": embedding_model,
        "embedding_dimension": embedding_dimension,
        "embedding_local_files_only": embedding_local_files_only,
        "normalize_embeddings": normalize_embeddings,
        "embedding_query_prompt": embedding_query_prompt,
        "splitter_identity": splitter_identity,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "collection_name": collection_name,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return EvaluationDenseCacheIdentity(
        cache_key=hashlib.sha256(encoded).hexdigest(),
        source_manifest_sha256=source_manifest_sha256,
        chunk_manifest_sha256=chunk_manifest_sha256,
        embedding_dimension=embedding_dimension,
    )


def _expected_dense_metadata(
    identity: EvaluationDenseCacheIdentity,
    *,
    document_count: int,
    chunk_count: int,
    embedding_model: str,
    query_prompt: str,
) -> dict[str, Any]:
    return {
        "cache_schema_version": DENSE_CACHE_SCHEMA_VERSION,
        "cache_status": CACHE_READY,
        "cache_key": identity.cache_key,
        "corpus_id": CORPUS_ID,
        "source_manifest_sha256": identity.source_manifest_sha256,
        "chunk_manifest_sha256": identity.chunk_manifest_sha256,
        "document_count": document_count,
        "chunk_count": chunk_count,
        "embedding_model": embedding_model,
        "embedding_dimension": identity.embedding_dimension,
        "embedding_local_files_only": DENSE_LOCAL_FILES_ONLY,
        "normalize_embeddings": DENSE_NORMALIZE_EMBEDDINGS,
        "embedding_query_prompt": query_prompt,
        "splitter_identity": SPLITTER_IDENTITY,
        "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP,
        "collection_name": COLLECTION_NAME,
    }


def _assert_dense_metadata_matches(metadata: dict[str, Any], expected: dict[str, Any]) -> None:
    mismatches = [key for key, value in expected.items() if metadata.get(key) != value]
    if mismatches:
        raise ValueError("CACHE_INVALID: metadata mismatch: " + ",".join(sorted(mismatches)))


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _release_build_handles(manager: VectorDBManager) -> None:
    """释放 Chroma 本地 client，确保 Windows 可以原子发布 cache 目录。"""
    collection = getattr(manager.vector_store, "_collection", None)
    client = getattr(collection, "_client", None)
    system = getattr(client, "_system", None)
    stop = getattr(system, "stop", None)
    if callable(stop):
        stop()
    manager.vector_store = None  # type: ignore[assignment]
    gc.collect()


def _acquire_build_lock(cache_root: Path, cache_key: str) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    lock_path = cache_root / f"{cache_key}.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise RuntimeError("CACHE_BUILDING: another local build owns this cache identity") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    return lock_path


def _validate_ready_dense_cache(
    *,
    cache_dir: Path,
    identity: EvaluationDenseCacheIdentity,
    document_count: int,
    chunk_count: int,
    embedding_model: str,
    query_prompt: str,
) -> EvaluationDenseCacheResult:
    metadata_path = cache_dir / "cache_metadata.json"
    manifest_path = cache_dir / "manifest.json"
    chroma_dir = cache_dir / "chroma"
    if not metadata_path.is_file() or not manifest_path.is_file() or not chroma_dir.is_dir():
        raise ValueError("CACHE_INCOMPLETE: required completion artifacts are missing")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("CACHE_INVALID: metadata must be an object")
    _assert_dense_metadata_matches(
        metadata,
        _expected_dense_metadata(
            identity,
            document_count=document_count,
            chunk_count=chunk_count,
            embedding_model=embedding_model,
            query_prompt=query_prompt,
        ),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or _sha256_file(manifest_path) != metadata.get("manifest_file_sha256"):
        raise ValueError("CACHE_INVALID: manifest completion artifact mismatch")
    _assert_dense_manifest_semantics(manifest, identity, document_count, chunk_count)
    return EvaluationDenseCacheResult(
        cache_dir=cache_dir,
        chroma_dir=chroma_dir,
        manifest_path=manifest_path,
        metadata_path=metadata_path,
        identity=identity,
        status="CACHE_HIT",
        elapsed_seconds=0.0,
    )


def _assert_dense_manifest_semantics(
    manifest: dict[str, Any],
    identity: EvaluationDenseCacheIdentity,
    document_count: int,
    chunk_count: int,
) -> None:
    """从 manifest canonical content 重算 ordered chunk digest，并核对 semantic facts。

    Authority 来自 external expected identity（source/chunk digests）与 counts，
    不来自 metadata 或 manifest 的 self-described 值。
    """
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list):
        raise ValueError("CACHE_INVALID: manifest chunk list missing")
    chunk_ids = [str(item.get("chunk_id", "")) for item in chunks]
    if len(chunks) != chunk_count or len(set(chunk_ids)) != chunk_count:
        raise ValueError("CACHE_INVALID: manifest chunk count / unique identity mismatch")
    if _identity_list_digest(chunks) != identity.chunk_manifest_sha256:
        raise ValueError("CACHE_INVALID: manifest ordered chunk digest mismatch")
    if manifest.get("corpus_id") is not None and manifest.get("corpus_id") != CORPUS_ID:
        raise ValueError("CACHE_INVALID: manifest corpus ref mismatch")
    if manifest.get("document_count") is not None and int(manifest["document_count"]) != document_count:
        raise ValueError("CACHE_INVALID: manifest document count mismatch")
    if manifest.get("source_manifest_sha256") is not None and (
        manifest["source_manifest_sha256"] != identity.source_manifest_sha256
    ):
        raise ValueError("CACHE_INVALID: manifest source manifest digest mismatch")


def _assert_frozen_dense_query_config(
    *,
    embedding_model_path: Path,
    query_prompt_name: str | None,
) -> str:
    """cold build 与 warm load/query adapter 共用的 frozen model/prompt Authority。

    机械验证调用方实际使用的 model ref 与 query prompt 等于冻结事实：
    model == Qwen3-Embedding-0.6B、prompt == ""。不满足即 fail closed。
    返回 query_prompt（恒为 ""）。
    """
    model_ref = Path(embedding_model_path).resolve().name
    if model_ref != EMBEDDING_MODEL_NAME:
        raise RuntimeError(
            f"EMBEDDING_MODEL_REF_MISMATCH: expected {EMBEDDING_MODEL_NAME}, got {model_ref}"
        )
    query_prompt = query_prompt_name or ""
    if query_prompt:
        raise RuntimeError("DENSE_QUERY_PROMPT_MUST_BE_EMPTY")
    return query_prompt


def build_or_reuse_evaluation_dense_cache(
    *,
    corpus_dir: Path,
    cache_root: Path,
    embedding_model_path: Path,
    embedding_batch_size: int = 8,
    query_prompt_name: str | None = None,
) -> EvaluationDenseCacheResult:
    """构建一次或验证复用 synthetic Dense READY cache（staging → atomic publish）。

    identity 只绑定 semantic 输入；query-time / batch 不进入 key。构建时显式复核
    真实 model/config 符合 frozen facts（dimension、prompt 为空、model ref），
    不满足即 fail closed（STOP / ESCALATE_TO_CODEX）。
    """
    started = time.monotonic()
    # P1-01：在任何 cache lookup / identity 计算 / cold build 之前，先强制冻结
    # caller model ref 与 query prompt（与 warm load 同一 Authority）。
    model_dir = Path(embedding_model_path).resolve()
    query_prompt = _assert_frozen_dense_query_config(
        embedding_model_path=model_dir, query_prompt_name=query_prompt_name
    )
    root = cache_root.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    chunks, document_count = prepare_evaluation_chunks(corpus_dir)
    if not chunks:
        raise ValueError("evaluation corpus produced no chunks")
    source_digest = source_manifest_digest(corpus_dir)
    chunk_digest = ordered_chunk_manifest_digest(chunks)
    identity = evaluation_dense_cache_identity(
        source_manifest_sha256=source_digest,
        chunk_manifest_sha256=chunk_digest,
        embedding_model=EMBEDDING_MODEL_NAME,
        embedding_query_prompt=query_prompt,
    )
    cache_dir = root / identity.cache_key
    try:
        hit = _validate_ready_dense_cache(
            cache_dir=cache_dir,
            identity=identity,
            document_count=document_count,
            chunk_count=len(chunks),
            embedding_model=EMBEDDING_MODEL_NAME,
            query_prompt=query_prompt,
        )
    except ValueError as error:
        if cache_dir.exists() and not str(error).startswith("CACHE_INCOMPLETE"):
            raise
    else:
        return hit

    lock_path = _acquire_build_lock(root, identity.cache_key)
    try:
        if cache_dir.exists():
            return _validate_ready_dense_cache(
                cache_dir=cache_dir,
                identity=identity,
                document_count=document_count,
                chunk_count=len(chunks),
                embedding_model=EMBEDDING_MODEL_NAME,
                query_prompt=query_prompt,
            )
        build_dir = root / f".{identity.cache_key}.building-{os.getpid()}"
        if build_dir.exists():
            raise RuntimeError("CACHE_INCOMPLETE: stale local build directory requires operator inspection")
        build_dir.mkdir(parents=True)
        manager, manifest = build_evaluation_kb(
            persist_dir=build_dir / "chroma",
            embedding_model_path=model_dir,
            corpus_dir=corpus_dir,
            embedding_batch_size=embedding_batch_size,
            query_prompt_name=None,
        )
        actual_dimension = manifest.embedding_dimension
        if actual_dimension != DENSE_DIMENSION:
            raise RuntimeError(
                f"EMBEDDING_DIMENSION_MISMATCH: expected {DENSE_DIMENSION}, got {actual_dimension}"
            )
        if manager._query_prompt_name:  # frozen prompt 必须为空
            raise RuntimeError("EMBEDDING_QUERY_PROMPT_MUST_BE_EMPTY")
        if manifest.document_count != document_count or manifest.chunk_count != len(chunks):
            raise RuntimeError("CACHE_INVALID: built index count mismatch")
        metadata = _expected_dense_metadata(
            identity,
            document_count=document_count,
            chunk_count=len(chunks),
            embedding_model=EMBEDDING_MODEL_NAME,
            query_prompt=query_prompt,
        )
        metadata.update({"created_at": time.time(), "build_result": "BUILT"})
        manifest_payload = manifest.to_dict()
        _write_json_atomic(build_dir / "manifest.json", manifest_payload)
        metadata["manifest_file_sha256"] = _sha256_file(build_dir / "manifest.json")
        _write_json_atomic(build_dir / "cache_metadata.json", metadata)
        collection = manager.vector_store._collection
        collection_metadata = dict(collection.metadata or {})
        collection_metadata.update(
            {
                key: metadata[key]
                for key in (
                    "cache_schema_version",
                    "cache_key",
                    "source_manifest_sha256",
                    "chunk_manifest_sha256",
                )
            }
        )
        collection.modify(metadata=collection_metadata)
        _validate_ready_dense_cache(
            cache_dir=build_dir,
            identity=identity,
            document_count=document_count,
            chunk_count=len(chunks),
            embedding_model=EMBEDDING_MODEL_NAME,
            query_prompt=query_prompt,
        )
        _release_build_handles(manager)
        os.replace(build_dir, cache_dir)
        return EvaluationDenseCacheResult(
            cache_dir=cache_dir,
            chroma_dir=cache_dir / "chroma",
            manifest_path=cache_dir / "manifest.json",
            metadata_path=cache_dir / "cache_metadata.json",
            identity=identity,
            status="CACHE_BUILT",
            elapsed_seconds=time.monotonic() - started,
        )
    finally:
        lock_path.unlink(missing_ok=True)


def _recompute_dense_expected_identity(
    identity: EvaluationDenseCacheIdentity,
) -> EvaluationDenseCacheIdentity:
    """从 frozen expected inputs 机械重算 canonical identity。

    Authority 来自 frozen contract（model/dimension/local-only/normalize/prompt/
    splitter/chunk config/collection）+ 调用方提供的 source/chunk digests，不从
    待验证 metadata 反推。
    """
    return evaluation_dense_cache_identity(
        source_manifest_sha256=identity.source_manifest_sha256,
        chunk_manifest_sha256=identity.chunk_manifest_sha256,
        embedding_model=EMBEDDING_MODEL_NAME,
        embedding_dimension=DENSE_DIMENSION,
        embedding_local_files_only=DENSE_LOCAL_FILES_ONLY,
        normalize_embeddings=DENSE_NORMALIZE_EMBEDDINGS,
        embedding_query_prompt=DENSE_QUERY_PROMPT_NAME,
        splitter_identity=SPLITTER_IDENTITY,
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        collection_name=COLLECTION_NAME,
    )


def load_evaluation_dense_cache(
    *,
    cache_dir: Path,
    expected_identity: EvaluationDenseCacheIdentity,
    expected_document_count: int,
    expected_chunk_count: int,
    embedding_model_path: Path,
    embedding_batch_size: int = 8,
    query_prompt_name: str | None = None,
) -> tuple[VectorDBManager, EvaluationDenseCacheResult]:
    """按 external expected identity 加载 READY synthetic Dense cache（load-by-identity）。

    Authority 顺序：
    Expected Reality -> Validator -> Artifact Metadata / Manifest -> PASS/FAIL。
    要求 recomputed canonical identity == caller expected identity == directory identity
    == metadata cache identity，四者 exact match；不得由待验证 metadata 自证。
    并在创建 VectorDBManager（query adapter）之前，对调用方实际使用的 model ref 与
    query prompt 执行与 cold build 相同的 frozen fail-closed 校验。
    """
    # P1（Final）：caller query-adapter semantics 必须先于 VectorDBManager 创建强制。
    query_prompt = _assert_frozen_dense_query_config(
        embedding_model_path=embedding_model_path, query_prompt_name=query_prompt_name
    )
    resolved = Path(cache_dir).resolve(strict=True)
    if resolved.name != expected_identity.cache_key:
        raise ValueError("CACHE_INVALID: directory identity mismatch")
    recomputed = _recompute_dense_expected_identity(expected_identity)
    if recomputed.cache_key != expected_identity.cache_key:
        raise ValueError("CACHE_INVALID: expected identity is not frozen-canonical")
    if (
        expected_identity.embedding_dimension != DENSE_DIMENSION
        or expected_identity.source_manifest_sha256 != recomputed.source_manifest_sha256
        or expected_identity.chunk_manifest_sha256 != recomputed.chunk_manifest_sha256
    ):
        raise ValueError("CACHE_INVALID: expected identity semantic fields mismatch")
    metadata = json.loads((resolved / "cache_metadata.json").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("cache_status") != CACHE_READY:
        raise ValueError("CACHE_INCOMPLETE: cache is not READY")
    result = _validate_ready_dense_cache(
        cache_dir=resolved,
        identity=expected_identity,
        document_count=expected_document_count,
        chunk_count=expected_chunk_count,
        embedding_model=EMBEDDING_MODEL_NAME,
        query_prompt=DENSE_QUERY_PROMPT_NAME,
    )
    manager = VectorDBManager(
        db_persist_dir=str(result.chroma_dir),
        local_model_path=str(embedding_model_path),
        collection_name=COLLECTION_NAME,
        ingest_batch_size=32,
        embedding_batch_size=embedding_batch_size,
        query_prompt_name=query_prompt,
    )
    collection = manager.vector_store._collection
    collection_metadata = dict(collection.metadata or {})
    for key in (
        "cache_schema_version",
        "cache_key",
        "source_manifest_sha256",
        "chunk_manifest_sha256",
    ):
        if collection_metadata.get(key) != metadata.get(key):
            raise ValueError(f"CACHE_INVALID: collection metadata mismatch: {key}")
    if collection.count() != expected_chunk_count:
        raise ValueError("CACHE_INVALID: collection chunk count mismatch")
    return manager, result


__all__ = [
    "BM25_CACHE_SCHEMA_VERSION",
    "CACHE_BUILDING",
    "CACHE_READY",
    "CHUNK_OVERLAP",
    "CHUNK_SIZE",
    "COLLECTION_NAME",
    "CORPUS_ID",
    "DENSE_CACHE_SCHEMA_VERSION",
    "DENSE_DIMENSION",
    "DENSE_LOCAL_FILES_ONLY",
    "DENSE_NORMALIZE_EMBEDDINGS",
    "DENSE_QUERY_PROMPT_NAME",
    "EMBEDDING_MODEL_NAME",
    "SPLITTER_IDENTITY",
    "TRUTHFULNESS_LABEL",
    "EvaluationDenseCacheIdentity",
    "EvaluationDenseCacheResult",
    "EvaluationKbManifest",
    "build_evaluation_kb",
    "build_or_reuse_evaluation_dense_cache",
    "default_corpus_dir",
    "evaluation_dense_cache_identity",
    "load_evaluation_dense_cache",
    "manifest_json",
    "ordered_chunk_identities",
    "ordered_chunk_manifest_digest",
    "prepare_evaluation_chunks",
    "source_manifest_digest",
]
