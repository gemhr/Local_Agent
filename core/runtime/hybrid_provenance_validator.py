#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stage5-Phase6-WP2 生产 provenance / active-generation 启动校验器。

由 ``server.py::lifespan`` 在 Router 构造前调用；是唯一允许判断 Dense/BM25
兼容性的组件。WP2 起，校验成功即构造 application-scoped Hybrid 依赖：
保留已加载并验证的 ``ProductionBm25Artifact``（含 ``.index``）供 Hybrid
adapter 注入；degraded 场景由 Router 请求路径 fail closed（不回退 baseline）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.knowledge_base.production_bm25_artifact import (
    ProductionBm25Artifact,
    validate_production_bm25_artifact,
)
from core.knowledge_base.retrieval_index_provenance import (
    PROVENANCE_CONTRACT_VERSION,
    ActiveGenerationDescriptor,
    RetrievalIndexProvenance,
    physical_dense_collection_name,
    read_active_descriptor,
    retrieval_root,
    validate_retrieval_index_manifest,
    embedding_asset_tree_digest,
)
from core.knowledge_base.vector_db_manager import VectorDBManager


@dataclass(frozen=True, slots=True)
class ValidatedHybridGeneration:
    """通过完整校验的 active generation 的只读视图（WP2 将消费这些依赖）。"""

    generation_id: str
    provenance_sha256: str
    corpus_id: str
    dense_collection_name: str
    dense_persist_dir_ref: str
    manifest_path: Path
    bm25_artifact_path: Path
    artifact_metadata_path: Path
    provenance: RetrievalIndexProvenance
    expected_v2_marker: dict[str, Any]
    bm25_artifact: ProductionBm25Artifact


class HybridProvenanceValidationError(RuntimeError):
    """HYBRID_RRF 校验失败；携带 safe reason code。"""

    def __init__(self, safe_error_code: str, safe_message: str) -> None:
        self.safe_error_code = safe_error_code
        self.safe_message = safe_message
        super().__init__(safe_message)


def load_active_hybrid_descriptor(
    *, chroma_dir: str | Path, logical_collection_name: str
) -> ActiveGenerationDescriptor:
    """读取并校验 active descriptor 的 Dense 物理 locator。

    ``server.py`` 必须先使用这个 locator 打开 generation collection，随后才可
    调用完整 validator；绝不能用逻辑 baseline collection 代替 active Dense。
    """
    descriptor = read_active_descriptor(
        retrieval_root(chroma_dir, logical_collection_name)
    )
    if descriptor is None:
        raise HybridProvenanceValidationError(
            "ACTIVE_GENERATION_DESCRIPTOR_MISSING",
            "HYBRID_RRF 需要已发布的 active generation descriptor",
        )
    if descriptor.provenance_contract_version != PROVENANCE_CONTRACT_VERSION:
        raise HybridProvenanceValidationError(
            "ACTIVE_GENERATION_PROVENANCE_CONTRACT_MISMATCH",
            "active descriptor provenance contract mismatch",
        )
    expected_collection_name = physical_dense_collection_name(
        logical_collection_name, descriptor.generation_id
    )
    if descriptor.dense_collection_name != expected_collection_name:
        raise HybridProvenanceValidationError(
            "ACTIVE_GENERATION_DENSE_LOCATOR_INVALID",
            "active descriptor dense collection locator mismatch",
        )
    return descriptor


def validate_active_hybrid_generation(
    *,
    db_manager: VectorDBManager,
    chroma_dir: str | Path,
    logical_collection_name: str,
    embedding_model_path: str | Path,
    descriptor: ActiveGenerationDescriptor | None = None,
) -> ValidatedHybridGeneration:
    """执行冻结的 HYBRID_RRF startup 校验顺序。

    使用 lifespan 已构造的 ``db_manager``（应用级 Dense store 单实例），
    避免重新打开 collection（本地 Chroma 是持久化 store，fake/真实均一致）。

    顺序（frozen by Codex §9.4）：
    read+schema-validate active.json → resolve+containment-validate relative
    locators → read full manifest + recompute provenance_sha256 → open
    descriptor-named Dense collection + validate v2 marker → recompute
    embedding asset-tree digest → validate BM25 files/digests/frozen contract
    → exact-compare 全部共享 provenance 字段与 generation_id。
    """
    retrieval_root_path = retrieval_root(chroma_dir, logical_collection_name)
    descriptor = descriptor or load_active_hybrid_descriptor(
        chroma_dir=chroma_dir, logical_collection_name=logical_collection_name
    )
    if db_manager.collection_name != descriptor.dense_collection_name:
        raise HybridProvenanceValidationError(
            "DENSE_GENERATION_COLLECTION_MISMATCH",
            "db manager does not target active Dense generation",
        )
    locators = descriptor.resolve_locators(retrieval_root_path)
    manifest_path = locators["provenance_manifest_path"]
    artifact_metadata_path = locators["artifact_metadata_path"]
    bm25_artifact_path = locators["bm25_artifact_path"]

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HybridProvenanceValidationError(
            "ACTIVE_GENERATION_MANIFEST_INVALID", "provenance manifest unreadable or malformed"
        ) from exc
    provenance = validate_retrieval_index_manifest(manifest)
    if provenance.generation_id != descriptor.generation_id:
        raise HybridProvenanceValidationError(
            "ACTIVE_GENERATION_GENERATION_MISMATCH",
            "active descriptor generation_id mismatch with manifest",
        )
    if provenance.provenance_sha256() != descriptor.provenance_sha256:
        raise HybridProvenanceValidationError(
            "ACTIVE_GENERATION_PROVENANCE_DIGEST_MISMATCH",
            "active descriptor provenance digest mismatch with manifest",
        )

    expected_v2_marker = db_manager.expected_v2_collection_marker(
        generation_id=provenance.generation_id,
        provenance_contract_version=provenance.contract_version,
        provenance_sha256=provenance.provenance_sha256(),
        corpus_id=provenance.corpus_id,
        source_manifest_sha256=provenance.source_manifest_sha256,
        chunk_policy_sha256=provenance.chunk_policy_sha256,
        chunk_manifest_sha256=provenance.chunk_manifest_sha256,
        document_count=provenance.document_count,
        chunk_count=provenance.chunk_count,
        embedding_asset_tree_sha256=embedding_asset_tree_digest(embedding_model_path),
    )
    preflight = db_manager.collection_preflight(
        hybrid_required=True,
        expected_v2_marker=expected_v2_marker,
    )
    if preflight.status.value != "CURRENT":
        raise HybridProvenanceValidationError(
            "DENSE_GENERATION_V2_MARKER_INVALID",
            f"dense v2 marker preflight failed: {preflight.detected_version}",
        )

    bm25 = validate_production_bm25_artifact(
        generation_dir=bm25_artifact_path.parent,
        expected_provenance=provenance,
        expected_provenance_sha256=provenance.provenance_sha256(),
    )
    if bm25.provenance.generation_id != provenance.generation_id:
        raise HybridProvenanceValidationError(
            "BM25_GENERATION_MISMATCH",
            "bm25 generation_id mismatch with dense provenance",
        )

    return ValidatedHybridGeneration(
        generation_id=provenance.generation_id,
        provenance_sha256=provenance.provenance_sha256(),
        corpus_id=provenance.corpus_id,
        dense_collection_name=descriptor.dense_collection_name,
        dense_persist_dir_ref=descriptor.dense_persist_dir_ref,
        manifest_path=manifest_path,
        bm25_artifact_path=bm25_artifact_path,
        artifact_metadata_path=artifact_metadata_path,
        provenance=provenance,
        expected_v2_marker=expected_v2_marker,
        bm25_artifact=bm25,
    )


__all__ = [
    "HybridProvenanceValidationError",
    "ValidatedHybridGeneration",
    "load_active_hybrid_descriptor",
    "validate_active_hybrid_generation",
]
