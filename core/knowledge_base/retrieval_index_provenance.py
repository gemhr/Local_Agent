#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stage5-Phase6-WP1 生产检索索引 provenance / build 契约（frozen by Codex）。

本模块是 retrieval-specific 的窄契约，不是通用 artifact registry/framework。
冻结事实来自 ``.ai/handoff/stage5-phase6-wp1/20_codex_decision.md``：

- canonical corpus proof = ``source_manifest_sha256``
- canonical chunk proof = ``chunk_policy_sha256`` + ordered ``chunk_manifest_sha256``
- Dense/BM25 融合要求同一 ``generation_id`` 且共享 provenance 全等
- 共享契约版本 = ``retrieval-index-provenance.v1``
- 物理 Dense collection = ``la_{collection_key}_g_{uuidhex}``
- retrieval root = ``Path(LOCAL_AGENT_CHROMA_DIR) / localagent_retrieval / collection_key``
- active.json 使用 ``localagent-active-generation.v1`` 与原子 ``os.replace`` 发布

所有正文（source 内容、chunk 正文、模型文件内容）只进入 digest，绝不进入事件、
日志或 descriptor 文本。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

PROVENANCE_CONTRACT_VERSION = "retrieval-index-provenance.v1"
ACTIVE_DESCRIPTOR_SCHEMA_VERSION = "localagent-active-generation.v1"
EMBEDDING_ASSET_INVALID_ERROR = "EMBEDDING_MODEL_ASSET_INVALID"
RETRIEVAL_ROOT_DIR_NAME = "localagent_retrieval"
GENERATIONS_DIR_NAME = "generations"
ACTIVE_DESCRIPTOR_FILE_NAME = "active.json"
RETRIEVAL_INDEX_MANIFEST_FILE_NAME = "retrieval_index_manifest.json"
BM25_INDEX_FILE_NAME = "bm25_index.json"
ARTIFACT_METADATA_FILE_NAME = "artifact_metadata.json"
GENERATION_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DENSE_PERSIST_DIR_REF = "LOCAL_AGENT_CHROMA_DIR"


def _canonical_json(value: object) -> str:
    """canonical UTF-8 JSON：sort_keys、compact separators、无 NaN/Inf。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_hex(value: bytes) -> str:
    """计算 bytes 的 SHA-256（64 位小写 hex）。"""
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: object) -> str:
    """计算 canonical JSON 对象的 SHA-256。"""
    return sha256_hex(_canonical_json(value).encode("utf-8"))


def is_canonical_generation_id(value: str) -> bool:
    """校验 canonical lowercase UUID（无花括号、无大写）。"""
    return bool(GENERATION_ID_PATTERN.fullmatch(value))


def new_generation_id() -> str:
    """生成 canonical lowercase UUID generation id。"""
    return str(uuid.uuid4())


def collection_key(logical_collection_name: str) -> str:
    """collection_key = sha256(UTF-8 logical collection name).hexdigest()[:16]。

    collection_key 是 opaque，防止配置的 collection 文本变成文件系统路径段。
    """
    if not isinstance(logical_collection_name, str) or not logical_collection_name.strip():
        raise ValueError("logical collection name must be non-empty")
    return sha256_hex(logical_collection_name.encode("utf-8"))[:16]


def physical_dense_collection_name(logical_collection_name: str, generation_id: str) -> str:
    """物理 Dense collection 名：``la_{collection_key}_g_{uuidhex}``。

    generation_id 去连字符后拼接；只允许 canonical lowercase UUID。
    """
    if not is_canonical_generation_id(generation_id):
        raise ValueError("generation_id must be a canonical lowercase UUID")
    uuid_hex = generation_id.replace("-", "")
    return f"la_{collection_key(logical_collection_name)}_g_{uuid_hex}"


def retrieval_root(chroma_dir: str | os.PathLike[str], logical_collection_name: str) -> Path:
    """retrieval root = ``Path(LOCAL_AGENT_CHROMA_DIR) / localagent_retrieval / collection_key``。"""
    return (
        Path(chroma_dir).resolve()
        / RETRIEVAL_ROOT_DIR_NAME
        / collection_key(logical_collection_name)
    )


def _safe_digest_path(path: Path) -> bytes:
    """读取文件 bytes；任何 unreadable / symlink / special node 直接 fail。"""
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{EMBEDDING_ASSET_INVALID_ERROR}: unreadable entry") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"{EMBEDDING_ASSET_INVALID_ERROR}: symlink rejected")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{EMBEDDING_ASSET_INVALID_ERROR}: special filesystem node rejected")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{EMBEDDING_ASSET_INVALID_ERROR}: unreadable entry") from exc


# ---------------------------------------------------------------------------
# Source manifest
# ---------------------------------------------------------------------------


def source_manifest_items(corpus_dir: str | os.PathLike[str]) -> tuple[dict[str, str], ...]:
    """按 relative POSIX source path 稳定排序的 source manifest。

    使用与 ``document_loader.iter_supported_files`` 相同的 supported-file 规则；
    digest 为文件 bytes 的 SHA-256；不依赖 absolute path / mtime / 偶然顺序。
    目录内特殊/symlink 项按文件规则处理（常规文件才参与）。
    """
    root = Path(corpus_dir).resolve()
    if not root.is_dir():
        raise ValueError("corpus dir must be an existing directory")
    from core.knowledge_base.document_loader import iter_supported_files

    files = sorted(
        iter_supported_files(str(root)),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    items = [
        {
            "source": path.relative_to(root).as_posix(),
            "content_sha256": sha256_hex(_safe_digest_path(path)),
        }
        for path in files
    ]
    return tuple(items)


def source_manifest_digest(corpus_dir: str | os.PathLike[str]) -> str:
    """source_manifest_sha256 = SHA-256 of canonical JSON array。"""
    return canonical_sha256(list(source_manifest_items(corpus_dir)))


# ---------------------------------------------------------------------------
# Chunk policy
# ---------------------------------------------------------------------------


def build_chunk_policy_descriptor(
    *,
    chunk_schema_version: str,
    splitter_ref: str,
    chunk_size: int,
    chunk_overlap: int,
    chunk_content_format_ref: str,
) -> dict[str, object]:
    """构造 canonical chunk policy descriptor（不切分，只描述策略）。"""
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    if chunk_overlap < 0:
        raise ValueError("chunk_overlap must be >= 0")
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be < chunk_size")
    if not chunk_schema_version or not splitter_ref or not chunk_content_format_ref:
        raise ValueError("chunk policy refs must be non-empty")
    return {
        "chunk_schema_version": chunk_schema_version,
        "splitter_ref": splitter_ref,
        "chunk_size": int(chunk_size),
        "chunk_overlap": int(chunk_overlap),
        "chunk_content_format_ref": chunk_content_format_ref,
    }


def chunk_policy_digest(policy: Mapping[str, object]) -> str:
    """chunk_policy_sha256 = canonical JSON SHA-256。"""
    required = (
        "chunk_schema_version",
        "splitter_ref",
        "chunk_size",
        "chunk_overlap",
        "chunk_content_format_ref",
    )
    if any(key not in policy for key in required):
        raise ValueError("chunk policy descriptor is incomplete")
    return canonical_sha256(dict(policy))


# ---------------------------------------------------------------------------
# Ordered chunk manifest
# ---------------------------------------------------------------------------


def ordered_chunk_manifest(
    chunks: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, str], ...]:
    """从单次 prepared chunk set 构造有序 chunk manifest。

    每个 item：``ordinal, document_id, chunk_id, source, section_path, content_hash``。
    ordinal 是 canonical per-source 顺序下的稳定输出顺序（1-based）；
    ordering 是有意义的，chunk_manifest_sha256 绑定该序列。
    """
    items: list[dict[str, str]] = []
    for ordinal, chunk in enumerate(chunks, start=1):
        metadata = dict(chunk.get("metadata", {}))
        document_id = str(metadata.get("doc_id") or "")
        chunk_id = str(metadata.get("chunk_id") or "")
        source = str(metadata.get("source") or "")
        section_path = str(metadata.get("section_path") or "")
        content_hash = str(metadata.get("content_hash") or "")
        if not document_id or not chunk_id or not source or not content_hash:
            raise ValueError("chunk manifest item requires document_id/chunk_id/source/content_hash")
        items.append(
            {
                "ordinal": str(ordinal),
                "document_id": document_id,
                "chunk_id": chunk_id,
                "source": source,
                "section_path": section_path,
                "content_hash": content_hash,
            }
        )
    return tuple(items)


def ordered_chunk_manifest_digest(chunks: Iterable[Mapping[str, Any]]) -> str:
    """chunk_manifest_sha256 = canonical JSON of ordered array。"""
    return canonical_sha256(list(ordered_chunk_manifest(chunks)))


# ---------------------------------------------------------------------------
# RetrievalIndexProvenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetrievalIndexProvenance:
    """一个 generation 的共享不可变 provenance 值对象。"""

    generation_id: str
    corpus_id: str
    source_manifest_sha256: str
    chunk_policy: Mapping[str, object]
    chunk_policy_sha256: str
    chunk_manifest_sha256: str
    document_count: int
    chunk_count: int
    contract_version: str = PROVENANCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != PROVENANCE_CONTRACT_VERSION:
            raise ValueError("unsupported provenance contract version")
        if not is_canonical_generation_id(self.generation_id):
            raise ValueError("generation_id must be a canonical lowercase UUID")
        if not self.corpus_id or not self.source_manifest_sha256 or not self.chunk_manifest_sha256:
            raise ValueError("provenance identity fields must be non-empty")
        expected_policy = chunk_policy_digest(self.chunk_policy)
        if self.chunk_policy_sha256 != expected_policy:
            raise ValueError("chunk_policy_sha256 does not match chunk policy descriptor")
        if not isinstance(self.document_count, int) or self.document_count < 0:
            raise ValueError("document_count must be a non-negative integer")
        if not isinstance(self.chunk_count, int) or self.chunk_count < 1:
            raise ValueError("chunk_count must be a positive integer")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "generation_id": self.generation_id,
            "corpus_id": self.corpus_id,
            "source_manifest_sha256": self.source_manifest_sha256,
            "chunk_policy": dict(self.chunk_policy),
            "chunk_policy_sha256": self.chunk_policy_sha256,
            "chunk_manifest_sha256": self.chunk_manifest_sha256,
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
        }

    def provenance_sha256(self) -> str:
        """full provenance digest = SHA-256 of canonical serialization。

        覆盖所有共享字段（含 chunk_policy 展开），排除契约外的 Dense 专用字段。
        """
        return canonical_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RetrievalIndexProvenance":
        if not isinstance(payload, Mapping):
            raise ValueError("provenance payload must be an object")
        contract_version = payload.get("contract_version")
        if contract_version != PROVENANCE_CONTRACT_VERSION:
            raise ValueError(f"unsupported provenance contract version: {contract_version!r}")
        chunk_policy = payload.get("chunk_policy")
        if not isinstance(chunk_policy, Mapping):
            raise ValueError("chunk_policy must be an object")
        return cls(
            contract_version=contract_version,
            generation_id=str(payload["generation_id"]),
            corpus_id=str(payload["corpus_id"]),
            source_manifest_sha256=str(payload["source_manifest_sha256"]),
            chunk_policy=dict(chunk_policy),
            chunk_policy_sha256=str(payload["chunk_policy_sha256"]),
            chunk_manifest_sha256=str(payload["chunk_manifest_sha256"]),
            document_count=int(payload["document_count"]),
            chunk_count=int(payload["chunk_count"]),
        )


# ---------------------------------------------------------------------------
# Retrieval index manifest（一个 generation 的完整共享 manifest 文件）
# ---------------------------------------------------------------------------


def build_retrieval_index_manifest(provenance: RetrievalIndexProvenance, chunks) -> dict[str, object]:
    """构造完整 shared provenance/chunk manifest（每个 generation 恰好一份）。"""
    return {
        "schema_version": PROVENANCE_CONTRACT_VERSION,
        "provenance": provenance.to_dict(),
        "provenance_sha256": provenance.provenance_sha256(),
        "document_count": provenance.document_count,
        "chunk_count": provenance.chunk_count,
        "chunks": list(ordered_chunk_manifest(chunks)),
    }


def validate_retrieval_index_manifest(payload: Mapping[str, Any]) -> RetrievalIndexProvenance:
    """从 manifest 重算 provenance digest 与 chunk digest 并验证（fail closed）。"""
    if payload.get("schema_version") != PROVENANCE_CONTRACT_VERSION:
        raise ValueError("retrieval index manifest schema mismatch")
    provenance = RetrievalIndexProvenance.from_dict(payload["provenance"])
    if payload.get("provenance_sha256") != provenance.provenance_sha256():
        raise ValueError("provenance_sha256 mismatch in retrieval index manifest")
    if int(payload.get("document_count")) != provenance.document_count:
        raise ValueError("document_count mismatch in retrieval index manifest")
    if int(payload.get("chunk_count")) != provenance.chunk_count:
        raise ValueError("chunk_count mismatch in retrieval index manifest")
    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or len(chunks) != provenance.chunk_count:
        raise ValueError("chunk list count mismatch in retrieval index manifest")
    recomputed = canonical_sha256(chunks)
    if recomputed != provenance.chunk_manifest_sha256:
        raise ValueError("chunk_manifest_sha256 mismatch in retrieval index manifest")
    return provenance


# ---------------------------------------------------------------------------
# Active generation descriptor + containment + atomic publication
# ---------------------------------------------------------------------------


def _validate_relative_path(value: str, *, field_name: str, retrieval_root_path: Path) -> Path:
    """严格校验 relative locator：拒绝绝对/空段/./.. / drive / symlink / escape。"""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty relative path")
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"{field_name} must be relative and not drive-qualified")
    if "\x00" in value:
        raise ValueError(f"{field_name} contains NUL")
    segments = [segment for segment in value.replace("\\", "/").split("/") if segment]
    if any(segment in {".", ".."} or not segment for segment in segments):
        raise ValueError(f"{field_name} contains invalid path segments")
    candidate = retrieval_root_path.joinpath(*segments)
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise ValueError(f"{field_name} cannot be resolved") from exc
    if resolved != retrieval_root_path and retrieval_root_path not in resolved.parents:
        raise ValueError(f"{field_name} escapes retrieval root")
    # symlink traversal：已存在路径经 resolve 后必须仍在 root 内（上面已校验）。
    return candidate


class ActiveGenerationDescriptor:
    """``localagent-active-generation.v1`` 的模型与校验。"""

    def __init__(
        self,
        *,
        generation_id: str,
        provenance_contract_version: str,
        provenance_sha256: str,
        corpus_id: str,
        dense_persist_dir_ref: str,
        dense_collection_name: str,
        bm25_artifact_path: str,
        provenance_manifest_path: str,
        artifact_metadata_path: str,
    ) -> None:
        self.generation_id = generation_id
        self.provenance_contract_version = provenance_contract_version
        self.provenance_sha256 = provenance_sha256
        self.corpus_id = corpus_id
        self.dense_persist_dir_ref = dense_persist_dir_ref
        self.dense_collection_name = dense_collection_name
        self.bm25_artifact_path = bm25_artifact_path
        self.provenance_manifest_path = provenance_manifest_path
        self.artifact_metadata_path = artifact_metadata_path

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ActiveGenerationDescriptor":
        if not isinstance(payload, Mapping):
            raise ValueError("active descriptor payload must be an object")
        if payload.get("schema_version") != ACTIVE_DESCRIPTOR_SCHEMA_VERSION:
            raise ValueError("active descriptor schema mismatch")
        generation_id = str(payload["generation_id"])
        if not is_canonical_generation_id(generation_id):
            raise ValueError("active descriptor generation_id is not a canonical UUID")
        dense = payload.get("dense")
        if not isinstance(dense, Mapping):
            raise ValueError("active descriptor dense locator missing")
        if dense.get("persist_dir_ref") != _DENSE_PERSIST_DIR_REF:
            raise ValueError("active descriptor dense.persist_dir_ref must be LOCAL_AGENT_CHROMA_DIR")
        return cls(
            generation_id=generation_id,
            provenance_contract_version=str(payload["provenance_contract_version"]),
            provenance_sha256=str(payload["provenance_sha256"]),
            corpus_id=str(payload["corpus_id"]),
            dense_persist_dir_ref=str(dense["persist_dir_ref"]),
            dense_collection_name=str(dense["collection_name"]),
            bm25_artifact_path=str(payload["bm25_artifact_path"]),
            provenance_manifest_path=str(payload["provenance_manifest_path"]),
            artifact_metadata_path=str(payload["artifact_metadata_path"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ACTIVE_DESCRIPTOR_SCHEMA_VERSION,
            "generation_id": self.generation_id,
            "provenance_contract_version": self.provenance_contract_version,
            "provenance_sha256": self.provenance_sha256,
            "corpus_id": self.corpus_id,
            "dense": {
                "persist_dir_ref": self.dense_persist_dir_ref,
                "collection_name": self.dense_collection_name,
            },
            "bm25_artifact_path": self.bm25_artifact_path,
            "provenance_manifest_path": self.provenance_manifest_path,
            "artifact_metadata_path": self.artifact_metadata_path,
        }

    def resolve_locators(self, retrieval_root_path: Path) -> dict[str, Path]:
        """resolve 并 containment-validate 全部 relative locators。"""
        return {
            "bm25_artifact_path": _validate_relative_path(
                self.bm25_artifact_path, field_name="bm25_artifact_path", retrieval_root_path=retrieval_root_path
            ),
            "provenance_manifest_path": _validate_relative_path(
                self.provenance_manifest_path, field_name="provenance_manifest_path", retrieval_root_path=retrieval_root_path
            ),
            "artifact_metadata_path": _validate_relative_path(
                self.artifact_metadata_path, field_name="artifact_metadata_path", retrieval_root_path=retrieval_root_path
            ),
        }


def read_active_descriptor(retrieval_root_path: Path) -> ActiveGenerationDescriptor | None:
    """读取 active.json；文件缺失返回 None，损坏则 fail closed。"""
    active_path = retrieval_root_path / ACTIVE_DESCRIPTOR_FILE_NAME
    if not active_path.is_file():
        return None
    try:
        payload = json.loads(active_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("ACTIVE_DESCRIPTOR_INVALID: unreadable or malformed") from exc
    return ActiveGenerationDescriptor.from_dict(payload)


def publish_active_descriptor(
    descriptor: ActiveGenerationDescriptor,
    retrieval_root_path: Path,
) -> None:
    """原子发布 active.json：唯一临时 sibling → canonical JSON+\n → flush → fsync → replace。

    失败不替换旧 active；调用方负责在发布前完成 generation 全量构建与验证。
    """
    retrieval_root_path.mkdir(parents=True, exist_ok=True)
    active_path = retrieval_root_path / ACTIVE_DESCRIPTOR_FILE_NAME
    descriptor.resolve_locators(retrieval_root_path)  # 发布前先验证 locator
    payload = _canonical_json(descriptor.to_dict()) + "\n"
    import tempfile

    fd, temp_name = tempfile.mkstemp(
        prefix=".active-", suffix=".tmp", dir=str(retrieval_root_path)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, active_path)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Embedding asset tree digest
# ---------------------------------------------------------------------------


def embedding_asset_tree_manifest(embedding_model_path: str | os.PathLike[str]) -> tuple[dict[str, str], ...]:
    """递归遍历 DIGEST_ROOT 下全部常规文件（含隐藏/缓存/生成文件）。

    无排除、无 ignore list、无 allowlist。拒绝 symlink（file/dir）、broken link、
    unreadable、socket/device/FIFO 等特殊节点（fail closed，不静默跳过）。
    """
    root = Path(embedding_model_path).resolve()
    if not root.is_dir():
        raise ValueError(f"{EMBEDDING_ASSET_INVALID_ERROR}: model path is not a directory")
    entries: list[dict[str, str]] = []

    def walk(directory: Path) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise ValueError(f"{EMBEDDING_ASSET_INVALID_ERROR}: unreadable directory") from exc
        for child in children:
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"{EMBEDDING_ASSET_INVALID_ERROR}: symlink rejected: {child.name}")
            if stat.S_ISDIR(info.st_mode):
                walk(child)
            elif stat.S_ISREG(info.st_mode):
                try:
                    digest = sha256_hex(child.read_bytes())
                except OSError as exc:
                    raise ValueError(f"{EMBEDDING_ASSET_INVALID_ERROR}: unreadable entry") from exc
                relative = child.relative_to(root).as_posix()
                entries.append({"path": relative, "content_sha256": digest})
            else:
                raise ValueError(f"{EMBEDDING_ASSET_INVALID_ERROR}: special filesystem node rejected")

    walk(root)
    entries.sort(key=lambda item: item["path"])
    return tuple(entries)


def embedding_asset_tree_digest(embedding_model_path: str | os.PathLike[str]) -> str:
    """embedding_asset_tree_sha256 = SHA-256 of canonical JSON array（sorted）。"""
    return canonical_sha256(list(embedding_asset_tree_manifest(embedding_model_path)))


__all__ = [
    "ACTIVE_DESCRIPTOR_FILE_NAME",
    "ACTIVE_DESCRIPTOR_SCHEMA_VERSION",
    "ARTIFACT_METADATA_FILE_NAME",
    "ActiveGenerationDescriptor",
    "BM25_INDEX_FILE_NAME",
    "EMBEDDING_ASSET_INVALID_ERROR",
    "GENERATIONS_DIR_NAME",
    "PROVENANCE_CONTRACT_VERSION",
    "RETRIEVAL_INDEX_MANIFEST_FILE_NAME",
    "RETRIEVAL_ROOT_DIR_NAME",
    "RetrievalIndexProvenance",
    "build_chunk_policy_descriptor",
    "build_retrieval_index_manifest",
    "canonical_sha256",
    "chunk_policy_digest",
    "collection_key",
    "embedding_asset_tree_digest",
    "embedding_asset_tree_manifest",
    "is_canonical_generation_id",
    "new_generation_id",
    "ordered_chunk_manifest",
    "ordered_chunk_manifest_digest",
    "physical_dense_collection_name",
    "publish_active_descriptor",
    "read_active_descriptor",
    "retrieval_root",
    "sha256_hex",
    "source_manifest_digest",
    "source_manifest_items",
    "validate_retrieval_index_manifest",
]
