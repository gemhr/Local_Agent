"""Stage5 Phase3 WP0B BEIR SciFact public benchmark Evaluation KB 环境。

最薄 Public Benchmark Corpus Adapter：把外部 BEIR corpus.jsonl 物化为
LocalAgent 内部 Markdown document，再走既有 loader / structure-aware splitter v2 /
metadata pipeline / Qwen3 Embedding / fresh Chroma。不创建第二套 Chunker，
不改变既有 chunk identity contract（doc_id / chunk_id / content_hash 不变）；
BEIR document id 以 benchmark metadata 附加在每个 chunk 上。
"""

from __future__ import annotations

import json
import hashlib
import gc
import os
import re
import shutil
import tempfile
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

BEIR_BENCHMARK = "beir"
SCIFACT_DATASET = "scifact"
BEIR_SCIFACT_CORPUS_ID = "beir-scifact-corpus.v1"
BEIR_SCIFACT_COLLECTION_NAME = "beir_scifact_eval_v1"
TRUTHFULNESS_LABEL = "PUBLIC_BENCHMARK_BEIR_SCIFACT_CORPUS"
CHUNK_SIZE = 1400
CHUNK_OVERLAP = 180
INGEST_BATCH_ID = BEIR_SCIFACT_CORPUS_ID
EMBEDDING_MODEL_NAME = "Qwen3-Embedding-0.6B"
PURPOSE = "evaluation"
CACHE_SCHEMA_VERSION = "beir-scifact-dense-index-cache.v1"
CACHE_READY = "READY"
CACHE_BUILDING = "BUILDING"

# 与 loader/producer wire id 一致的 filename-safe 字符集；不满足即 fail closed。
_SAFE_DOCUMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]*$")


@dataclass(frozen=True, slots=True)
class BeirScifactKbManifest:
    corpus_id: str
    collection_name: str
    benchmark: str
    benchmark_dataset: str
    document_count: int
    chunk_count: int
    embedding_model_name: str
    embedding_dimension: int
    chunk_size: int
    chunk_overlap: int
    documents: tuple[dict[str, Any], ...]
    chunks: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_id": self.corpus_id,
            "collection_name": self.collection_name,
            "benchmark": self.benchmark,
            "benchmark_dataset": self.benchmark_dataset,
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
            "embedding_model_name": self.embedding_model_name,
            "embedding_dimension": self.embedding_dimension,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "documents": list(self.documents),
            "chunks": list(self.chunks),
        }


@dataclass(frozen=True, slots=True)
class BeirScifactCacheIdentity:
    """仅包含会改变 SciFact dense index 语义的 cache identity。"""

    cache_key: str
    corpus_sha256: str
    manifest_sha256: str
    embedding_dimension: int


@dataclass(frozen=True, slots=True)
class BeirScifactCacheResult:
    """可复用 index 的位置及其不可变 provenance。"""

    cache_dir: Path
    chroma_dir: Path
    manifest_path: Path
    metadata_path: Path
    identity: BeirScifactCacheIdentity
    status: str
    elapsed_seconds: float


def materialize_beir_corpus(corpus_jsonl: Path, target_dir: Path) -> int:
    """把 BEIR corpus.jsonl 物化为 LocalAgent Markdown documents。

    每个 BEIR document `_id` 写为 `<_id>.md`，内容为 `# title` + text，
    供既有 loader / structure-aware splitter 按原始配置处理。
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with Path(corpus_jsonl).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            entry = json.loads(line)
            document_id = entry.get("_id")
            title = entry.get("title", "")
            text = entry.get("text", "")
            if not isinstance(document_id, str) or not document_id:
                raise ValueError(f"corpus.jsonl line {line_number} has invalid _id")
            if not _SAFE_DOCUMENT_ID.match(document_id):
                raise ValueError(
                    f"BEIR document id is not filename-safe: {document_id!r}"
                )
            if not isinstance(title, str) or not isinstance(text, str):
                raise ValueError(f"corpus.jsonl line {line_number} has invalid title/text")
            content = f"# {title}\n\n{text}" if title else text
            (target_dir / f"{document_id}.md").write_text(content, encoding="utf-8")
            count += 1
    return count


def prepare_beir_scifact_chunks(
    materialized_dir: Path,
) -> tuple[list[dict[str, Any]], int, dict[str, str]]:
    """物化 corpus 走既有 loader/splitter，并附加 benchmark document identity metadata。

    返回 (chunks, materialized_file_count, benchmark_id_by_doc_id)。chunk identity
    （doc_id / chunk_id / content_hash）完全沿用既有算法，不受 benchmark metadata 影响。
    """
    root = materialized_dir.resolve(strict=True)
    files = list(iter_supported_files(str(root)))
    chunks: list[dict[str, Any]] = []
    benchmark_id_by_doc_id: dict[str, str] = {}
    for path in files:
        benchmark_document_id = path.stem
        documents = load_document_file(path, root)
        document_chunks = split_documents(
            documents,
            chunk_size=CHUNK_SIZE,
            chunk_overlap=CHUNK_OVERLAP,
            ingest_batch_id=INGEST_BATCH_ID,
        )
        for chunk in document_chunks:
            metadata = dict(chunk["metadata"])
            metadata["benchmark"] = BEIR_BENCHMARK
            metadata["benchmark_dataset"] = SCIFACT_DATASET
            metadata["benchmark_document_id"] = benchmark_document_id
            chunk["metadata"] = metadata
            benchmark_id_by_doc_id[metadata["doc_id"]] = benchmark_document_id
        chunks.extend(document_chunks)
    return chunks, len(files), benchmark_id_by_doc_id


def _assert_fresh_persistence(persist_dir: Path) -> None:
    resolved = persist_dir.resolve(strict=False)
    if resolved.exists() and any(resolved.iterdir()):
        raise ValueError("evaluation persistence must be new or empty")


def verify_beir_scifact_split_determinism(materialized_dir: Path) -> None:
    """验证同一 corpus + splitter config 产生完全一致的 chunk identity（A/B manifest）。"""
    first, first_files, first_mapping = prepare_beir_scifact_chunks(materialized_dir)
    second, second_files, second_mapping = prepare_beir_scifact_chunks(materialized_dir)
    first_identity = [
        (item["metadata"]["doc_id"], item["metadata"]["chunk_id"], item["metadata"]["content_hash"])
        for item in first
    ]
    second_identity = [
        (item["metadata"]["doc_id"], item["metadata"]["chunk_id"], item["metadata"]["content_hash"])
        for item in second
    ]
    if first_identity != second_identity:
        raise RuntimeError("BEIR SciFact split identity is not deterministic")
    if first_files != second_files or first_mapping != second_mapping:
        raise RuntimeError("BEIR SciFact split document mapping is not deterministic")


def build_beir_scifact_kb(
    *,
    persist_dir: Path,
    embedding_model_path: Path,
    materialized_dir: Path,
    embedding_batch_size: int = 8,
    query_prompt_name: str | None = None,
    verify_split_determinism: bool = True,
) -> tuple[VectorDBManager, BeirScifactKbManifest]:
    """只在 fresh persistence 中构建 BEIR SciFact Evaluation collection。"""
    _assert_fresh_persistence(persist_dir)
    if verify_split_determinism:
        verify_beir_scifact_split_determinism(materialized_dir)
    chunks, document_count, benchmark_id_by_doc_id = prepare_beir_scifact_chunks(
        materialized_dir
    )
    if not chunks:
        raise ValueError("BEIR SciFact corpus produced no chunks")
    manager = VectorDBManager(
        db_persist_dir=str(persist_dir),
        local_model_path=str(embedding_model_path),
        collection_name=BEIR_SCIFACT_COLLECTION_NAME,
        ingest_batch_size=32,
        embedding_batch_size=embedding_batch_size,
        query_prompt_name=query_prompt_name,
    )
    written = manager.ingest_chunks(chunks)
    if written != len(chunks):
        raise RuntimeError("BEIR SciFact KB ingest count mismatch")
    manager.publish_collection_marker()
    collection = manager.vector_store._collection
    metadata = dict(collection.metadata or {})
    dimension = manager.embedding_dimension()
    metadata.update(
        {
            "purpose": PURPOSE,
            "corpus_id": BEIR_SCIFACT_CORPUS_ID,
            "truthfulness_label": TRUTHFULNESS_LABEL,
            "embedding_model": EMBEDDING_MODEL_NAME,
            "embedding_dimension": dimension,
            "chunk_size": CHUNK_SIZE,
            "chunk_overlap": CHUNK_OVERLAP,
            "benchmark": BEIR_BENCHMARK,
            "benchmark_dataset": SCIFACT_DATASET,
        }
    )
    collection.modify(metadata=metadata)
    documents = tuple(
        {
            "document_id": doc_id,
            "benchmark_document_id": benchmark_id,
            "source": f"{benchmark_id}.md",
        }
        for doc_id, benchmark_id in sorted(
            benchmark_id_by_doc_id.items(), key=lambda item: item[1]
        )
    )
    chunk_identities = tuple(
        {
            "document_id": str(item["metadata"]["doc_id"]),
            "chunk_id": str(item["metadata"]["chunk_id"]),
            "benchmark_document_id": str(item["metadata"]["benchmark_document_id"]),
            "source": str(item["metadata"]["source"]),
            "section_path": str(item["metadata"].get("section_path") or ""),
            "content_hash": str(item["metadata"]["content_hash"]),
        }
        for item in chunks
    )
    return manager, BeirScifactKbManifest(
        corpus_id=BEIR_SCIFACT_CORPUS_ID,
        collection_name=BEIR_SCIFACT_COLLECTION_NAME,
        benchmark=BEIR_BENCHMARK,
        benchmark_dataset=SCIFACT_DATASET,
        document_count=document_count,
        chunk_count=len(chunks),
        embedding_model_name=EMBEDDING_MODEL_NAME,
        embedding_dimension=dimension,
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        documents=documents,
        chunks=chunk_identities,
    )


def manifest_json(manifest: BeirScifactKbManifest) -> str:
    return json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True)


def default_beir_scifact_cache_root(corpus_jsonl: Path) -> Path:
    """从外部 BEIR dataset 路径推导本机 evaluation cache 根目录。"""
    corpus = Path(corpus_jsonl).resolve(strict=True)
    # <beir>/datasets/scifact/corpus.jsonl -> <beir>/cache/scifact
    return corpus.parents[2] / "cache" / SCIFACT_DATASET


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _chunk_manifest_sha256(chunks: list[dict[str, Any]]) -> str:
    identities = [
        {
            "document_id": str(item["metadata"]["doc_id"]),
            "chunk_id": str(item["metadata"]["chunk_id"]),
            "benchmark_document_id": str(item["metadata"]["benchmark_document_id"]),
            "content_hash": str(item["metadata"]["content_hash"]),
        }
        for item in chunks
    ]
    value = json.dumps(identities, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def beir_scifact_cache_identity(
    *,
    corpus_sha256: str,
    manifest_sha256: str,
    embedding_model: str = EMBEDDING_MODEL_NAME,
    embedding_dimension: int = 1024,
    embedding_query_prompt: str = "",
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> BeirScifactCacheIdentity:
    """计算 dense index 语义 identity；query-time 参数刻意不在此处出现。"""
    payload = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "benchmark": BEIR_BENCHMARK,
        "dataset": SCIFACT_DATASET,
        "split": "test",
        "corpus_sha256": corpus_sha256,
        "manifest_sha256": manifest_sha256,
        "embedding_model": embedding_model,
        "embedding_dimension": embedding_dimension,
        "embedding_local_files_only": True,
        "embedding_query_prompt": embedding_query_prompt,
        "splitter_identity": "structure-aware-splitter.v2",
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return BeirScifactCacheIdentity(
        cache_key=hashlib.sha256(encoded).hexdigest(),
        corpus_sha256=corpus_sha256,
        manifest_sha256=manifest_sha256,
        embedding_dimension=embedding_dimension,
    )


def _expected_metadata(identity: BeirScifactCacheIdentity, *, document_count: int, chunk_count: int) -> dict[str, Any]:
    return {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "cache_status": CACHE_READY,
        "cache_key": identity.cache_key,
        "benchmark": BEIR_BENCHMARK,
        "dataset": SCIFACT_DATASET,
        "split": "test",
        "corpus_sha256": identity.corpus_sha256,
        "manifest_sha256": identity.manifest_sha256,
        "document_count": document_count,
        "chunk_count": chunk_count,
        "embedding_model": EMBEDDING_MODEL_NAME,
        "embedding_dimension": identity.embedding_dimension,
        "embedding_local_files_only": True,
        "embedding_query_prompt": "",
        "splitter_identity": "structure-aware-splitter.v2",
        "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP,
        "collection_name": BEIR_SCIFACT_COLLECTION_NAME,
        "corpus_id": BEIR_SCIFACT_CORPUS_ID,
    }


def _assert_metadata_matches(metadata: dict[str, Any], expected: dict[str, Any]) -> None:
    mismatches = [key for key, value in expected.items() if metadata.get(key) != value]
    if mismatches:
        raise ValueError("CACHE_INVALID: metadata mismatch: " + ",".join(sorted(mismatches)))


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


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


def _validate_ready_cache(
    *,
    cache_dir: Path,
    identity: BeirScifactCacheIdentity,
    document_count: int,
    chunk_count: int,
) -> BeirScifactCacheResult:
    metadata_path = cache_dir / "cache_metadata.json"
    manifest_path = cache_dir / "manifest.json"
    chroma_dir = cache_dir / "chroma"
    if not metadata_path.is_file() or not manifest_path.is_file() or not chroma_dir.is_dir():
        raise ValueError("CACHE_INCOMPLETE: required completion artifacts are missing")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("CACHE_INVALID: metadata must be an object")
    _assert_metadata_matches(metadata, _expected_metadata(identity, document_count=document_count, chunk_count=chunk_count))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or _sha256_file(manifest_path) != metadata.get("manifest_file_sha256"):
        raise ValueError("CACHE_INVALID: manifest completion artifact mismatch")
    return BeirScifactCacheResult(
        cache_dir=cache_dir,
        chroma_dir=chroma_dir,
        manifest_path=manifest_path,
        metadata_path=metadata_path,
        identity=identity,
        status="CACHE_HIT",
        elapsed_seconds=0.0,
    )


def build_or_reuse_beir_scifact_cache(
    *,
    corpus_jsonl: Path,
    cache_root: Path | None,
    embedding_model_path: Path,
    embedding_batch_size: int = 8,
    query_prompt_name: str | None = None,
) -> BeirScifactCacheResult:
    """构建一次或验证复用一个本机 BEIR SciFact dense evaluation index。

    Chunk 与 metadata 先在 staging 中准备；只有完整 index、manifest 和 READY metadata
    均已原子发布时才允许后续复用。query-time retrieval 配置不参与 identity。
    """
    started = time.monotonic()
    corpus = Path(corpus_jsonl).resolve(strict=True)
    root = (cache_root or default_beir_scifact_cache_root(corpus)).resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=".beir-scifact-cache-", dir=root))
    try:
        materialized = staging_root / "materialized"
        document_count = materialize_beir_corpus(corpus, materialized)
        verify_beir_scifact_split_determinism(materialized)
        chunks, prepared_document_count, _mapping = prepare_beir_scifact_chunks(materialized)
        if document_count != prepared_document_count or not chunks:
            raise RuntimeError("BEIR SciFact cache preparation produced an invalid corpus")
        identity = beir_scifact_cache_identity(
            corpus_sha256=_sha256_file(corpus), manifest_sha256=_chunk_manifest_sha256(chunks)
        )
        cache_dir = root / identity.cache_key
        try:
            hit = _validate_ready_cache(
                cache_dir=cache_dir,
                identity=identity,
                document_count=document_count,
                chunk_count=len(chunks),
            )
        except ValueError as error:
            if cache_dir.exists() and not str(error).startswith("CACHE_INCOMPLETE"):
                raise
        else:
            return BeirScifactCacheResult(
                cache_dir=hit.cache_dir,
                chroma_dir=hit.chroma_dir,
                manifest_path=hit.manifest_path,
                metadata_path=hit.metadata_path,
                identity=hit.identity,
                status=hit.status,
                elapsed_seconds=time.monotonic() - started,
            )

        lock_path = _acquire_build_lock(root, identity.cache_key)
        try:
            # A second process may have finished while this process waited to acquire its lock.
            if cache_dir.exists():
                return _validate_ready_cache(
                    cache_dir=cache_dir,
                    identity=identity,
                    document_count=document_count,
                    chunk_count=len(chunks),
                )
            build_dir = root / f".{identity.cache_key}.building-{os.getpid()}"
            if build_dir.exists():
                raise RuntimeError("CACHE_INCOMPLETE: stale local build directory requires operator inspection")
            build_dir.mkdir(parents=True)
            manager, manifest = build_beir_scifact_kb(
                persist_dir=build_dir / "chroma",
                embedding_model_path=embedding_model_path,
                materialized_dir=materialized,
                embedding_batch_size=embedding_batch_size,
                query_prompt_name=query_prompt_name,
                verify_split_determinism=False,
            )
            if manifest.document_count != document_count or manifest.chunk_count != len(chunks):
                raise RuntimeError("CACHE_INVALID: built index count mismatch")
            metadata = _expected_metadata(identity, document_count=document_count, chunk_count=len(chunks))
            metadata.update({"created_at": time.time(), "build_result": "BUILT"})
            manifest_payload = manifest.to_dict()
            _write_json_atomic(build_dir / "manifest.json", manifest_payload)
            metadata["manifest_file_sha256"] = _sha256_file(build_dir / "manifest.json")
            _write_json_atomic(build_dir / "cache_metadata.json", metadata)
            collection = manager.vector_store._collection
            collection_metadata = dict(collection.metadata or {})
            collection_metadata.update({key: metadata[key] for key in ("cache_schema_version", "cache_key", "manifest_sha256", "corpus_sha256")})
            collection.modify(metadata=collection_metadata)
            _validate_ready_cache(cache_dir=build_dir, identity=identity, document_count=document_count, chunk_count=len(chunks))
            _release_build_handles(manager)
            os.replace(build_dir, cache_dir)
            return BeirScifactCacheResult(
                cache_dir=cache_dir, chroma_dir=cache_dir / "chroma", manifest_path=cache_dir / "manifest.json",
                metadata_path=cache_dir / "cache_metadata.json", identity=identity, status="CACHE_BUILT",
                elapsed_seconds=time.monotonic() - started,
            )
        finally:
            lock_path.unlink(missing_ok=True)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def load_beir_scifact_cache(
    *,
    cache_dir: Path,
    embedding_model_path: Path,
    embedding_batch_size: int = 8,
    query_prompt_name: str | None = None,
) -> tuple[VectorDBManager, BeirScifactCacheResult]:
    """加载 READY cache，并同时验证 metadata 与 Chroma collection metadata。"""
    resolved = Path(cache_dir).resolve(strict=True)
    metadata = json.loads((resolved / "cache_metadata.json").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("cache_status") != CACHE_READY:
        raise ValueError("CACHE_INCOMPLETE: cache is not READY")
    identity = BeirScifactCacheIdentity(
        cache_key=str(metadata.get("cache_key", "")),
        corpus_sha256=str(metadata.get("corpus_sha256", "")),
        manifest_sha256=str(metadata.get("manifest_sha256", "")),
        embedding_dimension=int(metadata.get("embedding_dimension", -1)),
    )
    result = _validate_ready_cache(
        cache_dir=resolved,
        identity=identity,
        document_count=int(metadata.get("document_count", -1)),
        chunk_count=int(metadata.get("chunk_count", -1)),
    )
    manager = VectorDBManager(
        db_persist_dir=str(result.chroma_dir),
        local_model_path=str(embedding_model_path),
        collection_name=BEIR_SCIFACT_COLLECTION_NAME,
        ingest_batch_size=32,
        embedding_batch_size=embedding_batch_size,
        query_prompt_name=query_prompt_name,
    )
    collection = manager.vector_store._collection
    collection_metadata = dict(collection.metadata or {})
    for key in ("cache_schema_version", "cache_key", "manifest_sha256", "corpus_sha256"):
        if collection_metadata.get(key) != metadata.get(key):
            raise ValueError(f"CACHE_INVALID: collection metadata mismatch: {key}")
    if collection.count() != int(metadata["chunk_count"]):
        raise ValueError("CACHE_INVALID: collection chunk count mismatch")
    return manager, result


__all__ = [
    "BEIR_BENCHMARK",
    "BEIR_SCIFACT_COLLECTION_NAME",
    "BEIR_SCIFACT_CORPUS_ID",
    "CACHE_BUILDING",
    "CACHE_READY",
    "CACHE_SCHEMA_VERSION",
    "BeirScifactCacheIdentity",
    "BeirScifactCacheResult",
    "BeirScifactKbManifest",
    "CHUNK_OVERLAP",
    "CHUNK_SIZE",
    "EMBEDDING_MODEL_NAME",
    "SCIFACT_DATASET",
    "TRUTHFULNESS_LABEL",
    "build_beir_scifact_kb",
    "build_or_reuse_beir_scifact_cache",
    "beir_scifact_cache_identity",
    "default_beir_scifact_cache_root",
    "load_beir_scifact_cache",
    "manifest_json",
    "materialize_beir_corpus",
    "prepare_beir_scifact_chunks",
    "verify_beir_scifact_split_determinism",
]
