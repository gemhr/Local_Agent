#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stage5-Phase6-WP1 生产 BM25 generation artifact（frozen by Codex）。

一个 generation 的不可变目录包含：

- ``bm25_index.json``：既有 ``Bm25SparseIndex`` serializer payload
- ``retrieval_index_manifest.json``：完整 shared provenance + ordered chunk manifest
- ``artifact_metadata.json``：schema/status、provenance digest、冻结 BM25 字段、
  document/chunk counts、两个文件的 SHA-256

加载要求：文件存在、READY、文件 digest 校验、冻结 BM25 校验、descriptor/manifest
相等、与 active Dense marker 的共享 provenance 精确相等。缺失/损坏/不兼容一律
fail closed；启动时绝不重建。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.knowledge_base.bm25_sparse_index import (
    BM25_ALGORITHM_REF,
    BM25_B,
    BM25_INDEX_SCHEMA_VERSION,
    BM25_K1,
    BM25_TOKENIZER_REF,
    Bm25Document,
    Bm25SparseIndex,
)
from core.knowledge_base.retrieval_index_provenance import (
    ARTIFACT_METADATA_FILE_NAME,
    BM25_INDEX_FILE_NAME,
    PROVENANCE_CONTRACT_VERSION,
    RETRIEVAL_INDEX_MANIFEST_FILE_NAME,
    RetrievalIndexProvenance,
    ordered_chunk_manifest,
    validate_retrieval_index_manifest,
)

BM25_ARTIFACT_SCHEMA_VERSION = "localagent-bm25-artifact.v1"
BM25_ARTIFACT_STATUS_READY = "READY"
BM25_ARTIFACT_STATUS_BUILDING = "BUILDING"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _bm25_documents(chunks: list[dict[str, Any]]) -> tuple[Bm25Document, ...]:
    """从 prepared chunk set 构造 Bm25Document（同一 chunk space）。"""
    documents = []
    for chunk in chunks:
        metadata = dict(chunk.get("metadata", {}))
        document_id = str(metadata.get("doc_id") or "")
        chunk_id = str(metadata.get("chunk_id") or "")
        if not document_id or not chunk_id:
            raise ValueError("BM25 artifact chunk requires document_id/chunk_id")
        documents.append(
            Bm25Document(
                document_id=document_id,
                chunk_id=chunk_id,
                text=str(chunk.get("page_content", "")),
                metadata={
                    "content_hash": str(metadata.get("content_hash") or ""),
                    "source": str(metadata.get("source") or ""),
                    "section_path": str(metadata.get("section_path") or ""),
                    "chunk_index": int(metadata.get("chunk_index", 0)),
                },
            )
        )
    return tuple(documents)


def _expected_artifact_metadata(
    *,
    provenance: RetrievalIndexProvenance,
    index_file_sha256: str,
    manifest_file_sha256: str,
    document_count: int,
    chunk_count: int,
    build_elapsed_seconds: float,
) -> dict[str, object]:
    return {
        "schema_version": BM25_ARTIFACT_SCHEMA_VERSION,
        "status": BM25_ARTIFACT_STATUS_READY,
        "provenance_contract_version": PROVENANCE_CONTRACT_VERSION,
        "generation_id": provenance.generation_id,
        "provenance_sha256": provenance.provenance_sha256(),
        "corpus_id": provenance.corpus_id,
        "source_manifest_sha256": provenance.source_manifest_sha256,
        "chunk_policy_sha256": provenance.chunk_policy_sha256,
        "chunk_manifest_sha256": provenance.chunk_manifest_sha256,
        "bm25_schema_version": BM25_INDEX_SCHEMA_VERSION,
        "algorithm_ref": BM25_ALGORITHM_REF,
        "tokenizer_ref": BM25_TOKENIZER_REF,
        "k1": BM25_K1,
        "b": BM25_B,
        "document_count": document_count,
        "chunk_count": chunk_count,
        "index_file_sha256": index_file_sha256,
        "manifest_file_sha256": manifest_file_sha256,
        "build_elapsed_seconds": build_elapsed_seconds,
    }


def _write_json_atomic(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


@dataclass(frozen=True, slots=True)
class ProductionBm25Artifact:
    """已验证的生产 BM25 generation artifact 的只读视图。"""

    generation_dir: Path
    index: Bm25SparseIndex
    provenance: RetrievalIndexProvenance
    provenance_sha256: str
    metadata: Mapping[str, object]

    @property
    def generation_id(self) -> str:
        return self.provenance.generation_id


def build_production_bm25_artifact(
    *,
    generation_dir: Path,
    provenance: RetrievalIndexProvenance,
    chunks: list[dict[str, Any]],
    document_count: int,
) -> ProductionBm25Artifact:
    """在 staging generation 目录内构建并验证 BM25 artifact。

    目录由 build coordinator 创建；此处允许已存在（``exist_ok=True``）。
    """
    generation_dir.mkdir(parents=True, exist_ok=True)
    index = Bm25SparseIndex.build(_bm25_documents(chunks))
    index_path = generation_dir / BM25_INDEX_FILE_NAME
    manifest_path = generation_dir / RETRIEVAL_INDEX_MANIFEST_FILE_NAME
    metadata_path = generation_dir / ARTIFACT_METADATA_FILE_NAME

    manifest = {
        "schema_version": PROVENANCE_CONTRACT_VERSION,
        "provenance": provenance.to_dict(),
        "provenance_sha256": provenance.provenance_sha256(),
        "document_count": provenance.document_count,
        "chunk_count": provenance.chunk_count,
        "chunks": list(ordered_chunk_manifest(chunks)),
    }
    index.save(index_path)
    _write_json_atomic(manifest_path, manifest)
    index_file_sha256 = _sha256_file(index_path)
    manifest_file_sha256 = _sha256_file(manifest_path)
    metadata = _expected_artifact_metadata(
        provenance=provenance,
        index_file_sha256=index_file_sha256,
        manifest_file_sha256=manifest_file_sha256,
        document_count=document_count,
        chunk_count=len(chunks),
        build_elapsed_seconds=0.0,
    )
    _write_json_atomic(metadata_path, metadata)
    return validate_production_bm25_artifact(generation_dir=generation_dir)


def validate_production_bm25_artifact(
    *,
    generation_dir: Path,
    expected_provenance: RetrievalIndexProvenance | None = None,
    expected_provenance_sha256: str | None = None,
) -> ProductionBm25Artifact:
    """完整校验一个 generation 的 BM25 artifact（fail closed）。

    - 文件存在 + READY + 文件 digest
    - 冻结 BM25 serializer 契约（复用 ``Bm25SparseIndex.load``）
    - manifest 重算 provenance/chunk digest
    - 与外部 expected provenance 精确相等（含 generation_id）
    """
    resolved = Path(generation_dir).resolve(strict=False)
    index_path = resolved / BM25_INDEX_FILE_NAME
    manifest_path = resolved / RETRIEVAL_INDEX_MANIFEST_FILE_NAME
    metadata_path = resolved / ARTIFACT_METADATA_FILE_NAME
    for path in (index_path, manifest_path, metadata_path):
        if not path.is_file():
            raise ValueError(f"BM25_ARTIFACT_INCOMPLETE: missing {path.name}")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("BM25_ARTIFACT_INVALID: metadata must be an object")
    if metadata.get("schema_version") != BM25_ARTIFACT_SCHEMA_VERSION:
        raise ValueError("BM25_ARTIFACT_INVALID: artifact schema mismatch")
    if metadata.get("status") != BM25_ARTIFACT_STATUS_READY:
        raise ValueError("BM25_ARTIFACT_INCOMPLETE: artifact not READY")

    index = Bm25SparseIndex.load(index_path)
    if int(metadata.get("chunk_count")) != index.document_count:
        raise ValueError("BM25_ARTIFACT_INVALID: chunk count mismatch with index")
    for key in (
        "bm25_schema_version",
        "algorithm_ref",
        "tokenizer_ref",
        "k1",
        "b",
    ):
        expected_value = {
            "bm25_schema_version": BM25_INDEX_SCHEMA_VERSION,
            "algorithm_ref": BM25_ALGORITHM_REF,
            "tokenizer_ref": BM25_TOKENIZER_REF,
            "k1": BM25_K1,
            "b": BM25_B,
        }[key]
        if metadata.get(key) != expected_value:
            raise ValueError(f"BM25_ARTIFACT_INVALID: frozen BM25 field mismatch: {key}")

    if _sha256_file(index_path) != metadata.get("index_file_sha256"):
        raise ValueError("BM25_ARTIFACT_INVALID: index file digest mismatch")
    if _sha256_file(manifest_path) != metadata.get("manifest_file_sha256"):
        raise ValueError("BM25_ARTIFACT_INVALID: manifest file digest mismatch")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("BM25_ARTIFACT_INVALID: manifest must be an object")
    provenance = validate_retrieval_index_manifest(manifest)
    metadata_generation = str(metadata.get("generation_id") or "")
    if metadata_generation != provenance.generation_id:
        raise ValueError("BM25_ARTIFACT_INVALID: generation mismatch between metadata and manifest")
    if metadata.get("provenance_sha256") != provenance.provenance_sha256():
        raise ValueError("BM25_ARTIFACT_INVALID: provenance digest mismatch in metadata")

    if expected_provenance is not None:
        _assert_exact_provenance_equality(provenance, expected_provenance)
    if expected_provenance_sha256 is not None:
        if expected_provenance_sha256 != provenance.provenance_sha256():
            raise ValueError("BM25_ARTIFACT_INVALID: provenance digest mismatch with expected")
    if int(metadata.get("chunk_count")) != provenance.chunk_count:
        raise ValueError("BM25_ARTIFACT_INVALID: chunk count mismatch in metadata")

    return ProductionBm25Artifact(
        generation_dir=resolved,
        index=index,
        provenance=provenance,
        provenance_sha256=provenance.provenance_sha256(),
        metadata=metadata,
    )


def _assert_exact_provenance_equality(
    actual: RetrievalIndexProvenance, expected: RetrievalIndexProvenance
) -> None:
    """HybridFuseAllowed 所需的 exact shared provenance 相等。"""
    if actual.contract_version != expected.contract_version:
        raise ValueError("PROVENANCE_MISMATCH: contract version")
    if actual.generation_id != expected.generation_id:
        raise ValueError("PROVENANCE_MISMATCH: generation_id")
    if actual.source_manifest_sha256 != expected.source_manifest_sha256:
        raise ValueError("PROVENANCE_MISMATCH: source_manifest_sha256")
    if actual.chunk_policy_sha256 != expected.chunk_policy_sha256:
        raise ValueError("PROVENANCE_MISMATCH: chunk_policy_sha256")
    if actual.chunk_manifest_sha256 != expected.chunk_manifest_sha256:
        raise ValueError("PROVENANCE_MISMATCH: chunk_manifest_sha256")
    if actual.document_count != expected.document_count:
        raise ValueError("PROVENANCE_MISMATCH: document_count")
    if actual.chunk_count != expected.chunk_count:
        raise ValueError("PROVENANCE_MISMATCH: chunk_count")


__all__ = [
    "BM25_ARTIFACT_SCHEMA_VERSION",
    "BM25_ARTIFACT_STATUS_BUILDING",
    "BM25_ARTIFACT_STATUS_READY",
    "ProductionBm25Artifact",
    "build_production_bm25_artifact",
    "validate_production_bm25_artifact",
]
