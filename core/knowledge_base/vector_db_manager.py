"""向量知识库管理模块。"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings


class VectorDBManager:
    """封装 Chroma 向量库的入库与检索操作。"""

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
        """执行带相关度分数的相似度检索。"""
        kwargs: Dict[str, Any] = {"query": query, "k": top_k}
        if metadata_filter:
            kwargs["filter"] = metadata_filter
        return self.vector_store.similarity_search_with_relevance_scores(**kwargs)

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
