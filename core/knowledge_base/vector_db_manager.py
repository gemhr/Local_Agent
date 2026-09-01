"""向量知识库管理模块。"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List

from core.knowledge_base.document_loader import SCHEMA_VERSION as KB_CHUNK_SCHEMA_VERSION
from core.knowledge_base.vector_scores import (
    VectorScoreSemantics,
    normalize_vector_score,
)
from core.persistence_migration import (
    PERSISTENCE_PREFLIGHT_FAILED,
    MigrationAction,
    PersistencePreflightResult,
    PreflightStatus,
    StoreId,
)
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings


MARKER_COLLECTION_CONTRACT_VERSION = 2
MARKER_CONTRACT_V1 = 1
_MARKER_KEYS = (
    "localagent_collection_contract_version",
    "chunk_schema_version",
    "embedding_compatibility_digest",
    "embedding_dimension",
)
_V2_MARKER_KEYS = (
    "generation_id",
    "provenance_contract_version",
    "provenance_sha256",
    "corpus_id",
    "source_manifest_sha256",
    "chunk_policy_sha256",
    "chunk_manifest_sha256",
    "document_count",
    "chunk_count",
    "embedding_asset_tree_sha256",
    "normalize_embeddings",
    "query_prompt_name",
)
_EMBEDDING_DIMENSION_PROBE_TEXT = "localagent-embedding-dimension-probe"


class VectorDBManager:
    """封装 Chroma 向量库的入库与检索操作。"""

    vector_score_semantics = VectorScoreSemantics.NORMALIZED_RELEVANCE
    chroma_by_vector_score_semantics = VectorScoreSemantics.RAW_DISTANCE

    def __init__(
        self,
        db_persist_dir: str,
        local_model_path: str,
        *,
        collection_name: str = "huawei_wiki_collection",
        ingest_batch_size: int = 32,
        embedding_batch_size: int = 8,
        query_prompt_name: str | None = None,
    ) -> None:
        """初始化向量数据库管理器。

        Args:
            db_persist_dir: Chroma 数据持久化目录。
            local_model_path: 已解析的本地 embedding 模型目录。
            collection_name: Chroma Collection 名称。
            ingest_batch_size: 应用层单次写入的 Chunk 数量。
            embedding_batch_size: Embedding 编码批次大小。
            query_prompt_name: 查询编码使用的 Prompt 名称。
        """
        self.db_persist_dir = db_persist_dir
        self.collection_name = collection_name
        self.ingest_batch_size = max(1, int(ingest_batch_size))

        model_path = Path(local_model_path)
        if not model_path.is_dir():
            raise FileNotFoundError(
                "EMBEDDING_MODEL_ASSET_INVALID: configured local embedding "
                "model directory missing"
            )
        self.embedding_model_id = str(model_path.resolve(strict=True))
        os.makedirs(self.db_persist_dir, exist_ok=True)

        model_kwargs = {"device": "cpu", "local_files_only": True}
        encode_kwargs = {
            "normalize_embeddings": True,
            "batch_size": max(1, int(embedding_batch_size)),
        }
        query_encode_kwargs = dict(encode_kwargs)
        if query_prompt_name:
            query_encode_kwargs["prompt_name"] = query_prompt_name

        self._query_prompt_name = query_prompt_name
        self.embeddings = HuggingFaceEmbeddings(
            model_name=self.embedding_model_id,
            model_kwargs=model_kwargs,
            encode_kwargs=encode_kwargs,
            query_encode_kwargs=query_encode_kwargs,
        )
        self.vector_store = Chroma(
            collection_name=self.collection_name,
            embedding_function=self.embeddings,
            persist_directory=self.db_persist_dir,
        )
        # Sentence Transformer 与本地 Chroma 都是同步依赖；显式串行化单实例
        # Query 调用，避免未知模型线程安全行为和并发查询状态交叉。
        self._embedding_query_lock = threading.Lock()
        self._vector_query_lock = threading.Lock()

    # ------------------------------------------------------------------
    # WP1-D Chroma LocalAgent collection marker（third-party boundary）
    # ------------------------------------------------------------------

    def _embedding_compatibility_descriptor(self) -> Dict[str, Any]:
        """确定性的 canonical safe descriptor；只用于计算 digest，不持久化。

        覆盖当前真实影响 embedding vector semantics 的配置事实：
        configured embedding identity、normalization、query/document prompt。
        """
        return {
            "embedding_identity": self.embedding_model_id,
            "normalize_embeddings": True,
            "query_prompt_name": self._query_prompt_name or None,
        }

    def embedding_compatibility_digest(self) -> str:
        """sha256(canonical JSON descriptor)。这是 configured compatibility
        descriptor digest，不是 model artifact 的 cryptographic attestation。"""
        canonical = json.dumps(
            self._embedding_compatibility_descriptor(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def embedding_dimension(self) -> int:
        """固定内部 probe 文本探测 embedding 维度；不写入 durable documents，
        不引入远程网络依赖。"""
        with self._embedding_query_lock:
            vector = self.embeddings.embed_query(_EMBEDDING_DIMENSION_PROBE_TEXT)
        dimension = len(vector)
        if not isinstance(dimension, int) or dimension <= 0:
            raise ValueError("embedding dimension must be a positive integer")
        return dimension

    def expected_collection_marker(self) -> Dict[str, Any]:
        return {
            "localagent_collection_contract_version": (
                MARKER_COLLECTION_CONTRACT_VERSION
            ),
            "chunk_schema_version": KB_CHUNK_SCHEMA_VERSION,
            "embedding_compatibility_digest": self.embedding_compatibility_digest(),
            "embedding_dimension": self.embedding_dimension(),
        }

    # ------------------------------------------------------------------
    # WP1 v2 Hybrid-compatible generation marker
    # ------------------------------------------------------------------

    def expected_v2_collection_marker(
        self,
        *,
        generation_id: str,
        provenance_contract_version: str,
        provenance_sha256: str,
        corpus_id: str,
        source_manifest_sha256: str,
        chunk_policy_sha256: str,
        chunk_manifest_sha256: str,
        document_count: int,
        chunk_count: int,
        embedding_asset_tree_sha256: str,
    ) -> Dict[str, Any]:
        """构造完整 v2 marker（v1 字段 + 冻结的 v2 provenance 字段）。"""
        marker = self.expected_collection_marker()
        marker.update(
            {
                "generation_id": generation_id,
                "provenance_contract_version": provenance_contract_version,
                "provenance_sha256": provenance_sha256,
                "corpus_id": corpus_id,
                "source_manifest_sha256": source_manifest_sha256,
                "chunk_policy_sha256": chunk_policy_sha256,
                "chunk_manifest_sha256": chunk_manifest_sha256,
                "document_count": int(document_count),
                "chunk_count": int(chunk_count),
                "embedding_asset_tree_sha256": embedding_asset_tree_sha256,
                "normalize_embeddings": True,
                "query_prompt_name": self._query_prompt_name or "",
            }
        )
        return marker

    def read_v2_collection_marker(self) -> Dict[str, Any]:
        """读取 v2 marker 字段；缺失或非 dict 返回空 dict。"""
        collection = getattr(self.vector_store, "_collection", None)
        if collection is None:
            return {}
        metadata = dict(collection.metadata or {})
        return {key: metadata[key] for key in _V2_MARKER_KEYS if key in metadata}

    def marker_contract_version(self) -> int | None:
        """当前 collection marker 的契约版本；无 marker 返回 None。"""
        collection = getattr(self.vector_store, "_collection", None)
        if collection is None:
            return None
        metadata = dict(collection.metadata or {})
        value = metadata.get("localagent_collection_contract_version")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def _v2_marker_matches(
        self,
        expected: Dict[str, Any],
    ) -> tuple[bool, list[str]]:
        """v2 marker 全等比较；返回 (ok, mismatched keys)。

        只比较 v2 冻结字段（含 v1 基础字段），忽略非 LocalAgent 元数据。
        """
        collection = getattr(self.vector_store, "_collection", None)
        if collection is None:
            return False, list(expected)
        metadata = dict(collection.metadata or {})
        all_marker_keys = _MARKER_KEYS + _V2_MARKER_KEYS
        missing = [key for key in all_marker_keys if key not in metadata]
        if missing:
            return False, missing
        mismatched = [
            key
            for key in all_marker_keys
            if key in expected and metadata.get(key) != expected[key]
        ]
        return not mismatched, mismatched

    def read_collection_marker(self) -> Dict[str, Any]:
        collection = getattr(self.vector_store, "_collection", None)
        if collection is None:
            return {}
        metadata = dict(collection.metadata or {})
        return {
            key: metadata[key] for key in _MARKER_KEYS if key in metadata
        }

    def publish_collection_marker(self) -> None:
        """整体 metadata replace（read-modify-merge）：保留非 LocalAgent 元数据，
        只写入 v1 基础 marker 字段。"""
        collection = getattr(self.vector_store, "_collection", None)
        if collection is None:
            raise RuntimeError("collection unavailable")
        existing = dict(collection.metadata or {})
        merged = {
            key: value
            for key, value in existing.items()
            if key not in _MARKER_KEYS and key not in _V2_MARKER_KEYS
        }
        merged.update(self.expected_collection_marker())
        collection.modify(metadata=merged)

    def publish_v2_collection_marker(self, expected: Dict[str, Any]) -> None:
        """发布完整 v2 marker（read-modify-merge，保留非 LocalAgent 元数据）。"""
        collection = getattr(self.vector_store, "_collection", None)
        if collection is None:
            raise RuntimeError("collection unavailable")
        existing = dict(collection.metadata or {})
        merged = {
            key: value
            for key, value in existing.items()
            if key not in _MARKER_KEYS and key not in _V2_MARKER_KEYS
        }
        merged.update(expected)
        collection.modify(metadata=merged)

    def remove_collection_marker(self) -> None:
        """只移除 LocalAgent marker 字段（v1+v2），保留其他元数据。

        Chroma 不允许空 metadata dict；若移除后 metadata 为空，写入 sentinel
        invalid marker，保证 collection 不再“看起来有效”（后续 preflight 判定
        mismatch → REBUILD_REQUIRED，绝不当作 CURRENT）。
        """
        collection = getattr(self.vector_store, "_collection", None)
        if collection is None:
            return
        existing = dict(collection.metadata or {})
        merged = {
            key: value
            for key, value in existing.items()
            if key not in _MARKER_KEYS and key not in _V2_MARKER_KEYS
        }
        if not merged:
            collection.modify(
                metadata={"localagent_collection_contract_version": -1}
            )
            return
        collection.modify(metadata=merged)

    def collection_preflight(
        self,
        *,
        hybrid_required: bool = False,
        expected_v2_marker: Dict[str, Any] | None = None,
    ) -> PersistencePreflightResult:
        """Startup marker validation（WP1 strategy-aware）。

        - 空 collection：NEW/INITIALIZE（仍允许初始化 v1 marker）。
        - 非空 collection：
          - baseline（``hybrid_required=False``）：v1 或 v2 marker 都维持 CURRENT；
            v1 是 legacy baseline 合法，v2 是已验证 generation 合法；缺 marker /
            mismatch → REBUILD_REQUIRED。
          - hybrid（``hybrid_required=True``）：只接受完整 v2 marker 且与
            ``expected_v2_marker`` 全等；v1 collection 一律 REBUILD_REQUIRED，
            绝不从 chunk rows 推断 provenance（无自动迁移/重建）。
        """
        collection = getattr(self.vector_store, "_collection", None)
        if collection is None:
            return PersistencePreflightResult(
                store_id=StoreId.CHROMA,
                status=PreflightStatus.FAILED,
                action=MigrationAction.NONE,
                detected_version="unknown",
                safe_error_code=PERSISTENCE_PREFLIGHT_FAILED,
            )
        count = collection.count()
        if count == 0:
            return PersistencePreflightResult(
                store_id=StoreId.CHROMA,
                status=PreflightStatus.NEW,
                action=MigrationAction.INITIALIZE,
                detected_version="empty",
                target_version=str(MARKER_COLLECTION_CONTRACT_VERSION),
            )
        marker = self.read_collection_marker()
        if not marker:
            return PersistencePreflightResult(
                store_id=StoreId.CHROMA,
                status=PreflightStatus.REBUILD_REQUIRED,
                action=MigrationAction.REBUILD,
                detected_version="unmarked",
                target_version=str(MARKER_COLLECTION_CONTRACT_VERSION),
            )
        if hybrid_required:
            if self.marker_contract_version() != MARKER_COLLECTION_CONTRACT_VERSION:
                return PersistencePreflightResult(
                    store_id=StoreId.CHROMA,
                    status=PreflightStatus.REBUILD_REQUIRED,
                    action=MigrationAction.REBUILD,
                    detected_version="v1-incompatible",
                    target_version=str(MARKER_COLLECTION_CONTRACT_VERSION),
                )
            if expected_v2_marker is None:
                raise ValueError("hybrid preflight requires expected_v2_marker")
            ok, mismatched = self._v2_marker_matches(expected_v2_marker)
            if not ok:
                return PersistencePreflightResult(
                    store_id=StoreId.CHROMA,
                    status=PreflightStatus.REBUILD_REQUIRED,
                    action=MigrationAction.REBUILD,
                    detected_version="v2-mismatch",
                    target_version=str(MARKER_COLLECTION_CONTRACT_VERSION),
                )
            return PersistencePreflightResult(
                store_id=StoreId.CHROMA,
                status=PreflightStatus.CURRENT,
                action=MigrationAction.NONE,
                detected_version=str(MARKER_COLLECTION_CONTRACT_VERSION),
                target_version=str(MARKER_COLLECTION_CONTRACT_VERSION),
            )
        expected = self.expected_collection_marker()
        if marker != expected:
            # baseline：v1 marker 合法（legacy baseline）；v2 marker 已是
            # 已验证 generation（v1 digest 字段与 expected 不一致是预期）。
            if self.marker_contract_version() in (MARKER_CONTRACT_V1, MARKER_COLLECTION_CONTRACT_VERSION):
                return PersistencePreflightResult(
                    store_id=StoreId.CHROMA,
                    status=PreflightStatus.CURRENT,
                    action=MigrationAction.NONE,
                    detected_version=str(self.marker_contract_version()),
                    target_version=str(MARKER_COLLECTION_CONTRACT_VERSION),
                )
            return PersistencePreflightResult(
                store_id=StoreId.CHROMA,
                status=PreflightStatus.REBUILD_REQUIRED,
                action=MigrationAction.REBUILD,
                detected_version="mismatch",
                target_version=str(MARKER_COLLECTION_CONTRACT_VERSION),
            )
        return PersistencePreflightResult(
            store_id=StoreId.CHROMA,
            status=PreflightStatus.CURRENT,
            action=MigrationAction.NONE,
            detected_version=str(MARKER_COLLECTION_CONTRACT_VERSION),
            target_version=str(MARKER_COLLECTION_CONTRACT_VERSION),
        )

    @staticmethod
    def _sanitize_metadata_value(value: Any) -> Any:
        """将 Metadata 值转换为 Chroma 支持的标量。"""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        item_method = getattr(value, "item", None)
        if callable(item_method):
            try:
                scalar = item_method()
                if isinstance(scalar, (str, int, float, bool)):
                    return scalar
            except Exception:
                pass
        if isinstance(value, (dict, list, tuple, set)):
            return json.dumps(value, ensure_ascii=False, default=str)
        return str(value)

    @classmethod
    def _sanitize_metadata(cls, metadata: Dict[str, Any]) -> Dict[str, Any]:
        sanitized = {}
        for key, value in metadata.items():
            converted = cls._sanitize_metadata_value(value)
            if converted is not None:
                sanitized[str(key)] = converted
        return sanitized

    def ingest_chunks(self, chunks: List[Dict[str, Any]]) -> int:
        """批量写入语义切片。

        Args:
            chunks: 包含 ``page_content`` 和 ``metadata`` 的切片列表。
        """
        if not chunks:
            return 0
        batch_size = self._resolve_batch_size()
        written = 0

        for start in range(0, len(chunks), batch_size):
            batch_docs = []
            batch_ids = []
            for index, chunk in enumerate(
                chunks[start : start + batch_size], start=start
            ):
                metadata = self._sanitize_metadata(chunk.get("metadata", {}))
                chunk_id = str(metadata.get("chunk_id", f"chunk-{index}"))
                batch_docs.append(
                    Document(
                        page_content=str(chunk.get("page_content", "")),
                        metadata=metadata,
                    )
                )
                batch_ids.append(chunk_id)
            try:
                self.vector_store.delete(ids=batch_ids)
            except Exception:
                pass
            self.vector_store.add_documents(batch_docs, ids=batch_ids)
            written += len(batch_docs)
        return written

    def delete_source(self, source: str) -> None:
        """删除某一来源文件已有的全部 Chunk。"""
        collection = getattr(self.vector_store, "_collection", None)
        if collection is None:
            return
        try:
            collection.delete(where={"source": source})
            return
        except Exception:
            pass
        try:
            result = collection.get(where={"source": source}, include=[])
            ids = result.get("ids", []) if result else []
        except Exception:
            ids = []
        if ids:
            self.vector_store.delete(ids=list(ids))

    def clear_collection(self) -> int:
        """清空当前 Collection，并返回删除数量。"""
        collection = getattr(self.vector_store, "_collection", None)
        if collection is None:
            return 0
        try:
            result = collection.get(include=[])
        except Exception:
            result = collection.get()
        ids = list(result.get("ids", [])) if result else []
        batch_size = self._resolve_batch_size()
        for start in range(0, len(ids), batch_size):
            self.vector_store.delete(ids=ids[start : start + batch_size])
        return len(ids)

    def count(self) -> int:
        """返回当前 Collection 的 Chunk 数量。"""
        collection = getattr(self.vector_store, "_collection", None)
        if collection is None:
            return 0
        return int(collection.count())

    def _resolve_batch_size(self) -> int:
        """解析当前 Chroma 允许的最大批次大小。"""
        chroma_batch_size: int | None = None
        collection = getattr(self.vector_store, "_collection", None)
        client = getattr(collection, "_client", None)
        if client is not None:
            for attr_name in ("max_batch_size", "get_max_batch_size"):
                attr = getattr(client, attr_name, None)
                if attr is None:
                    continue
                try:
                    value = attr() if callable(attr) else attr
                except Exception:
                    continue
                if isinstance(value, int) and value > 0:
                    chroma_batch_size = value
                    break
        if chroma_batch_size is None:
            return self.ingest_batch_size
        return min(self.ingest_batch_size, chroma_batch_size)

    def similarity_search(
        self,
        query: str,
        top_k: int = 4,
        metadata_filter: Dict[str, Any] | None = None,
    ) -> List[Document]:
        """执行相似度检索。

        Args:
            query: 用户查询文本。
            top_k: 返回的候选片段数。

        Returns:
            List[Document]: 检索结果列表。
        """
        kwargs: Dict[str, Any] = {"query": query, "k": top_k}
        if metadata_filter:
            kwargs["filter"] = metadata_filter
        return self.vector_store.similarity_search(**kwargs)

    def similarity_search_with_scores(
        self,
        query: str,
        top_k: int = 4,
        metadata_filter: Dict[str, Any] | None = None,
    ) -> List[tuple[Document, float]]:
        """执行带归一化分数的相似度检索，分数越高越相关。"""
        kwargs: Dict[str, Any] = {"query": query, "k": top_k}
        if metadata_filter:
            kwargs["filter"] = metadata_filter
        results = self.vector_store.similarity_search_with_score(**kwargs)
        return [
            (
                document,
                normalize_vector_score(
                    raw_distance, VectorScoreSemantics.RAW_DISTANCE
                ),
            )
            for document, raw_distance in results
        ]

    def embed_query(self, query: str) -> List[float]:
        """显式执行既有 HuggingFace Query Embedding，供 Runtime 分阶段控制。"""
        with self._embedding_query_lock:
            return [float(value) for value in self.embeddings.embed_query(query)]

    def search_by_vector_with_scores(
        self,
        embedding: List[float],
        k: int = 4,
        metadata_filter: Dict[str, Any] | None = None,
    ) -> List[tuple[Document, float]]:
        """使用已生成向量查询 Chroma；底层 raw distance 在此只转换一次。"""
        kwargs: Dict[str, Any] = {"embedding": embedding, "k": k}
        if metadata_filter:
            kwargs["filter"] = metadata_filter
        with self._vector_query_lock:
            results = self.vector_store.similarity_search_by_vector_with_relevance_scores(
                **kwargs
            )
        return [
            (
                document,
                normalize_vector_score(
                    raw_distance, self.chroma_by_vector_score_semantics
                ),
            )
            for document, raw_distance in results
        ]

    def keyword_search(self, terms: List[str], k: int = 8) -> List[Document]:
        """使用 Chroma 文本索引补充精确术语召回。"""
        collection = getattr(self.vector_store, "_collection", None)
        if collection is None or k < 1:
            return []

        documents: List[Document] = []
        seen_ids: set[str] = set()
        for term in terms:
            normalized_term = str(term).strip()
            if not normalized_term:
                continue
            variants = [normalized_term]
            if normalized_term.isascii() and normalized_term.isalnum():
                variants.extend([normalized_term.lower(), normalized_term.upper()])
            for variant in dict.fromkeys(variants):
                try:
                    result = collection.get(
                        where_document={"$contains": variant},
                        include=["documents", "metadatas"],
                        limit=k,
                    )
                except Exception:
                    continue
                ids = result.get("ids", []) if result else []
                texts = result.get("documents", []) if result else []
                metadatas = result.get("metadatas", []) if result else []
                for index, text in enumerate(texts):
                    document_id = str(ids[index]) if index < len(ids) else ""
                    metadata = metadatas[index] if index < len(metadatas) else {}
                    dedup_key = document_id or str(metadata.get("chunk_id", "")) or str(text)
                    if not text or dedup_key in seen_ids:
                        continue
                    seen_ids.add(dedup_key)
                    documents.append(Document(page_content=str(text), metadata=metadata or {}))
                    if len(documents) >= k:
                        return documents
        return documents

    def get_chunk_by_identity(self, document_id: str, chunk_id: str) -> Document | None:
        """按 active Dense generation 的语义身份获取完整 Chroma 元数据。

        只使用公开 Chroma collection API；返回 None 表示该 identity 在当前
        generation 中不存在。调用方（Hybrid materialization mapping）负责把
        identity 不匹配/metadata 无效转换为 RRF_FUSION_FAILED。
        """
        if not document_id or not chunk_id:
            return None
        getter = getattr(self.vector_store, "get", None)
        if not callable(getter):
            return None
        with self._vector_query_lock:
            result = getter(ids=[chunk_id], include=["documents", "metadatas"])
        ids = result.get("ids", []) if result else []
        texts = result.get("documents", []) if result else []
        metadatas = result.get("metadatas", []) if result else []
        if len(ids) != 1 or len(texts) != 1 or len(metadatas) != 1:
            return None
        metadata = metadatas[0] or {}
        if (
            str(ids[0]) != chunk_id
            or str(metadata.get("chunk_id", "")) != chunk_id
            or str(metadata.get("doc_id", "")) != document_id
        ):
            return None
        return Document(page_content=str(texts[0]), metadata=metadata)

    def search(
        self,
        query: str,
        k: int = 4,
        metadata_filter: Dict[str, Any] | None = None,
    ) -> List[Document]:
        """提供与业务层一致的统一检索入口。"""
        return self.similarity_search(
            query=query, top_k=k, metadata_filter=metadata_filter
        )

    def search_with_scores(
        self,
        query: str,
        k: int = 4,
        metadata_filter: Dict[str, Any] | None = None,
    ) -> List[tuple[Document, float]]:
        """提供带分数的统一检索入口。"""
        return self.similarity_search_with_scores(
            query=query,
            top_k=k,
            metadata_filter=metadata_filter,
        )
