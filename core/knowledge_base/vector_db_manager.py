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


MARKER_COLLECTION_CONTRACT_VERSION = 1
_MARKER_KEYS = (
    "localagent_collection_contract_version",
    "chunk_schema_version",
    "embedding_compatibility_digest",
    "embedding_dimension",
)
_EMBEDDING_DIMENSION_PROBE_TEXT = "localagent-embedding-dimension-probe"


class VectorDBManager:
    """封装 Chroma 向量库的入库与检索操作。"""

    vector_score_semantics = VectorScoreSemantics.NORMALIZED_RELEVANCE
    chroma_by_vector_score_semantics = VectorScoreSemantics.RAW_DISTANCE

    def __init__(
        self,
        db_persist_dir: str,
        local_model_path: str | None = None,
        *,
        collection_name: str = "huawei_wiki_collection",
        ingest_batch_size: int = 32,
        embedding_batch_size: int = 8,
        query_prompt_name: str | None = None,
    ) -> None:
        """初始化向量数据库管理器。

        Args:
            db_persist_dir: Chroma 数据持久化目录。
            local_model_path: 本地 embedding 模型目录；为空时使用默认模型名。
            collection_name: Chroma Collection 名称。
            ingest_batch_size: 应用层单次写入的 Chunk 数量。
            embedding_batch_size: Embedding 编码批次大小。
            query_prompt_name: 查询编码使用的 Prompt 名称。
        """
        self.db_persist_dir = db_persist_dir
        self.collection_name = collection_name
        self.ingest_batch_size = max(1, int(ingest_batch_size))
        os.makedirs(self.db_persist_dir, exist_ok=True)

        model_kwargs = {"device": "cpu"}
        encode_kwargs = {
            "normalize_embeddings": True,
            "batch_size": max(1, int(embedding_batch_size)),
        }
        query_encode_kwargs = dict(encode_kwargs)
        if query_prompt_name:
            query_encode_kwargs["prompt_name"] = query_prompt_name

        self._query_prompt_name = query_prompt_name
        self.embeddings = HuggingFaceEmbeddings(
            model_name=local_model_path or "BAAI/bge-large-zh-v1.5",
            model_kwargs=model_kwargs,
            encode_kwargs=encode_kwargs,
            query_encode_kwargs=query_encode_kwargs,
        )
        self.vector_store = Chroma(
            collection_name=self.collection_name,
            embedding_function=self.embeddings,
            persist_directory=self.db_persist_dir,
        )
        self.embedding_model_id = local_model_path or "BAAI/bge-large-zh-v1.5"
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
        只写入本 WP 的 marker 字段。"""
        collection = getattr(self.vector_store, "_collection", None)
        if collection is None:
            raise RuntimeError("collection unavailable")
        existing = dict(collection.metadata or {})
        merged = {key: value for key, value in existing.items() if key not in _MARKER_KEYS}
        merged.update(self.expected_collection_marker())
        collection.modify(metadata=merged)

    def remove_collection_marker(self) -> None:
        """只移除 LocalAgent marker 字段，保留其他元数据。

        Chroma 不允许空 metadata dict；若移除后 metadata 为空，写入 sentinel
        invalid marker，保证 collection 不再“看起来有效”（后续 preflight 判定
        mismatch → REBUILD_REQUIRED，绝不当作 CURRENT）。
        """
        collection = getattr(self.vector_store, "_collection", None)
        if collection is None:
            return
        existing = dict(collection.metadata or {})
        merged = {
            key: value for key, value in existing.items() if key not in _MARKER_KEYS
        }
        if not merged:
            collection.modify(
                metadata={"localagent_collection_contract_version": -1}
            )
            return
        collection.modify(metadata=merged)

    def collection_preflight(self) -> PersistencePreflightResult:
        """Startup marker validation。空 collection 允许初始化 marker；
        非空缺 marker / mismatch → REBUILD_REQUIRED；匹配 → CURRENT。"""
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
        expected = self.expected_collection_marker()
        if marker != expected:
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
