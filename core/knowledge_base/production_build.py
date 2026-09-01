#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stage5-Phase6-WP1 生产 retrieval index build 协调器（frozen by Codex）。

Build 执行一次精确流程：

source 枚举
→ source digest
→ canonical split ONCE（生产使用 Settings chunk 值）
→ ordered chunk manifest
→ RetrievalIndexProvenance
→ staged generation 目录
→ fresh generation-specific Dense Chroma collection
→ 从同一 prepared chunk set 构建 BM25 artifact
→ marker/artifact 验证
→ active.json 原子发布

失败 build 保持旧 active descriptor 不变；部分构建的 generation 不可达。
development 构建必须在 manifest 记录 ``purpose=development`` 且禁止写
production active.json。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.knowledge_base import document_loader
from core.knowledge_base.production_bm25_artifact import (
    build_production_bm25_artifact,
)
from core.knowledge_base.retrieval_index_provenance import (
    GENERATIONS_DIR_NAME,
    PROVENANCE_CONTRACT_VERSION,
    ActiveGenerationDescriptor,
    RetrievalIndexProvenance,
    chunk_policy_digest,
    new_generation_id,
    ordered_chunk_manifest_digest,
    physical_dense_collection_name,
    publish_active_descriptor,
    retrieval_root,
    source_manifest_digest,
)
from core.knowledge_base.vector_db_manager import VectorDBManager

BUILD_PURPOSE_PRODUCTION = "production"
BUILD_PURPOSE_DEVELOPMENT = "development"


@dataclass(frozen=True, slots=True)
class ProductionBuildResult:
    """一次成功 build 的结果（development 或 production）。"""

    generation_id: str
    purpose: str
    generation_dir: Path
    dense_collection_name: str
    active_published: bool


def _prepare_chunks_once(
    *,
    source_dir: Path,
    chunk_size: int,
    chunk_overlap: int,
    ingest_batch_id: str,
) -> tuple[list[dict[str, Any]], int]:
    """按 canonical 顺序加载文档并切分一次（生产 chunk policy 唯一 authority）。"""
    files = sorted(
        document_loader.iter_supported_files(str(source_dir)),
        key=lambda path: path.relative_to(source_dir).as_posix(),
    )
    chunks: list[dict[str, Any]] = []
    document_count = 0
    for path in files:
        documents = document_loader.load_document_file(path, source_dir)
        if not documents:
            continue
        document_count += len(documents)
        chunks.extend(
            document_loader.split_documents(
                documents,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                ingest_batch_id=ingest_batch_id,
            )
        )
    if not chunks:
        raise ValueError("KB_BUILD_NO_CHUNKS: corpus produced no chunks")
    return chunks, document_count


def build_production_generation(
    *,
    source_dir: str | os.PathLike[str],
    logical_collection_name: str,
    chroma_dir: str | os.PathLike[str],
    embedding_model_path: str | os.PathLike[str],
    chunk_size: int,
    chunk_overlap: int,
    embedding_batch_size: int = 8,
    query_prompt_name: str | None = None,
    purpose: str = BUILD_PURPOSE_PRODUCTION,
    publish_active: bool = True,
) -> ProductionBuildResult:
    """执行一次生产/开发 generation build（frozen order）。

    - production：``publish_active=True`` 才写 active.json（调用方按 CLI 语义决定）。
    - development：必须 ``publish_active=False``，manifest 记录 purpose=development。
    """
    if purpose not in (BUILD_PURPOSE_PRODUCTION, BUILD_PURPOSE_DEVELOPMENT):
        raise ValueError("build purpose must be production or development")
    if purpose == BUILD_PURPOSE_DEVELOPMENT and publish_active:
        raise ValueError("development build must not publish active.json")

    root = Path(source_dir).resolve()
    if not root.is_dir():
        raise ValueError("source dir must be an existing directory")
    retrieval_root_path = retrieval_root(chroma_dir, logical_collection_name)
    generation_id = new_generation_id()
    generation_dir = retrieval_root_path / GENERATIONS_DIR_NAME / generation_id
    if generation_dir.exists():
        raise ValueError("KB_BUILD_GENERATION_EXISTS: generation directory already exists")
    generation_dir.mkdir(parents=True, exist_ok=False)

    staged = False
    try:
        source_digest = source_manifest_digest(root)
        chunks, document_count = _prepare_chunks_once(
            source_dir=root,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            ingest_batch_id=generation_id,
        )
        chunk_policy = document_loader.chunk_policy_from(
            chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )
        provenance = RetrievalIndexProvenance(
            generation_id=generation_id,
            corpus_id=logical_collection_name,
            source_manifest_sha256=source_digest,
            chunk_policy=chunk_policy,
            chunk_policy_sha256=chunk_policy_digest(chunk_policy),
            chunk_manifest_sha256=ordered_chunk_manifest_digest(chunks),
            document_count=document_count,
            chunk_count=len(chunks),
        )

        dense_collection_name = physical_dense_collection_name(
            logical_collection_name, generation_id
        )
        manager = VectorDBManager(
            db_persist_dir=str(Path(chroma_dir).resolve()),
            local_model_path=str(Path(embedding_model_path).resolve()),
            collection_name=dense_collection_name,
            ingest_batch_size=32,
            embedding_batch_size=embedding_batch_size,
            query_prompt_name=query_prompt_name,
        )
        written = manager.ingest_chunks(chunks)
        if written != len(chunks):
            raise ValueError("KB_BUILD_DENSE_COUNT_MISMATCH")

        bm25_artifact = build_production_bm25_artifact(
            generation_dir=generation_dir,
            provenance=provenance,
            chunks=chunks,
            document_count=document_count,
        )
        # BM25 artifact 已把 retrieval_index_manifest.json 写在 generation_dir 内，
        # 因此 Dense marker 与 active descriptor 直接引用该路径。

        v2_marker = manager.expected_v2_collection_marker(
            generation_id=generation_id,
            provenance_contract_version=PROVENANCE_CONTRACT_VERSION,
            provenance_sha256=provenance.provenance_sha256(),
            corpus_id=logical_collection_name,
            source_manifest_sha256=provenance.source_manifest_sha256,
            chunk_policy_sha256=provenance.chunk_policy_sha256,
            chunk_manifest_sha256=provenance.chunk_manifest_sha256,
            document_count=provenance.document_count,
            chunk_count=provenance.chunk_count,
            embedding_asset_tree_sha256=_embedding_asset_tree_digest(embedding_model_path),
        )
        manager.publish_v2_collection_marker(v2_marker)

        preflight = manager.collection_preflight(
            hybrid_required=True,
            expected_v2_marker=v2_marker,
        )
        if preflight.status.value != "CURRENT":
            raise ValueError(f"KB_BUILD_MARKER_PREFLIGHT_FAILED: {preflight.detected_version}")

        if purpose == BUILD_PURPOSE_DEVELOPMENT:
            _annotate_development_purpose(generation_dir)

        if publish_active:
            descriptor = _descriptor_for(
                generation_id=generation_id,
                provenance=provenance,
                dense_collection_name=dense_collection_name,
            )
            publish_active_descriptor(descriptor, retrieval_root_path)
            active_published = True
        else:
            active_published = False
        staged = True
        return ProductionBuildResult(
            generation_id=generation_id,
            purpose=purpose,
            generation_dir=generation_dir,
            dense_collection_name=dense_collection_name,
            active_published=active_published,
        )
    finally:
        if not staged:
            shutil.rmtree(generation_dir, ignore_errors=True)


def _embedding_asset_tree_digest(embedding_model_path) -> str:
    from core.knowledge_base.retrieval_index_provenance import embedding_asset_tree_digest

    return embedding_asset_tree_digest(embedding_model_path)


def _annotate_development_purpose(generation_dir: Path) -> None:
    """development manifest 必须记录 purpose=development（不可发布 active）。"""
    from core.knowledge_base.retrieval_index_provenance import (
        RETRIEVAL_INDEX_MANIFEST_FILE_NAME,
    )

    manifest_path = generation_dir / RETRIEVAL_INDEX_MANIFEST_FILE_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["purpose"] = BUILD_PURPOSE_DEVELOPMENT
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, manifest_path)


def _descriptor_for(
    *,
    generation_id: str,
    provenance: RetrievalIndexProvenance,
    dense_collection_name: str,
) -> ActiveGenerationDescriptor:
    generation_rel = f"{GENERATIONS_DIR_NAME}/{generation_id}"
    return ActiveGenerationDescriptor(
        generation_id=generation_id,
        provenance_contract_version=PROVENANCE_CONTRACT_VERSION,
        provenance_sha256=provenance.provenance_sha256(),
        corpus_id=provenance.corpus_id,
        dense_persist_dir_ref="LOCAL_AGENT_CHROMA_DIR",
        dense_collection_name=dense_collection_name,
        bm25_artifact_path=f"{generation_rel}/bm25_index.json",
        provenance_manifest_path=f"{generation_rel}/retrieval_index_manifest.json",
        artifact_metadata_path=f"{generation_rel}/artifact_metadata.json",
    )


__all__ = [
    "BUILD_PURPOSE_DEVELOPMENT",
    "BUILD_PURPOSE_PRODUCTION",
    "ProductionBuildResult",
    "build_production_generation",
]
