"""向量知识库管理模块。"""

import os
from typing import Any, Dict, List

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings


class VectorDBManager:
    """封装 Chroma 向量库的入库与检索操作。"""

    def __init__(self, db_persist_dir: str, local_model_path: str | None = None) -> None:
        """初始化向量数据库管理器。

        Args:
            db_persist_dir: Chroma 数据持久化目录。
            local_model_path: 本地 embedding 模型目录；为空时使用默认模型名。
        """
        self.db_persist_dir = db_persist_dir
        os.makedirs(self.db_persist_dir, exist_ok=True)

        model_kwargs = {"device": "cpu"}
        encode_kwargs = {"normalize_embeddings": True}

        self.embeddings = HuggingFaceEmbeddings(
            model_name=local_model_path or "BAAI/bge-large-zh-v1.5",
            model_kwargs=model_kwargs,
            encode_kwargs=encode_kwargs,
        )
        self.vector_store = Chroma(
            collection_name="huawei_wiki_collection",
            embedding_function=self.embeddings,
            persist_directory=self.db_persist_dir,
        )

    def ingest_chunks(self, chunks: List[Dict[str, Any]]) -> None:
        """批量写入语义切片。

        Args:
            chunks: 包含 ``page_content`` 和 ``metadata`` 的切片列表。
        """
        if not chunks:
            return
        docs = [Document(page_content=chunk["page_content"], metadata=chunk["metadata"]) for chunk in chunks]
        ids = [str(chunk.get("metadata", {}).get("chunk_id", f"chunk-{index}")) for index, chunk in enumerate(chunks)]
        try:
            self.vector_store.delete(ids=ids)
        except Exception:
            pass
        self.vector_store.add_documents(docs, ids=ids)

    def similarity_search(self, query: str, top_k: int = 4) -> List[Document]:
        """执行相似度检索。

        Args:
            query: 用户查询文本。
            top_k: 返回的候选片段数。

        Returns:
            List[Document]: 检索结果列表。
        """
        return self.vector_store.similarity_search(query, k=top_k)

    def similarity_search_with_scores(
        self,
        query: str,
        top_k: int = 4,
    ) -> List[tuple[Document, float]]:
        """执行带相关度分数的相似度检索。"""
        return self.vector_store.similarity_search_with_relevance_scores(query, k=top_k)

    def search(self, query: str, k: int = 4) -> List[Document]:
        """提供与业务层一致的统一检索入口。"""
        return self.similarity_search(query=query, top_k=k)

    def search_with_scores(self, query: str, k: int = 4) -> List[tuple[Document, float]]:
        """提供带分数的统一检索入口。"""
        return self.similarity_search_with_scores(query=query, top_k=k)
