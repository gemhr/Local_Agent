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
        collection_name: str = "huawei_wiki_collection",
        ingest_batch_size: int = 32,
        embedding_batch_size: int = 8,
        query_prompt_name: str | None = "query",
    ) -> None:
        """初始化向量数据库管理器。

        Args:
            db_persist_dir:
                Chroma 数据持久化目录。
            local_model_path:
                本地 Embedding（向量化）模型目录。
            collection_name:
                Chroma Collection（集合）名称。
            ingest_batch_size:
                应用层单次入库批次大小。
        """
        self.db_persist_dir = db_persist_dir
        self.collection_name = collection_name
        self.ingest_batch_size = max(1, ingest_batch_size)

        os.makedirs(self.db_persist_dir, exist_ok=True)

        embedding_batch_size = max(
            1,
            int(embedding_batch_size),
        )

        model_kwargs: dict[str, Any] = {
            "device": "cpu",
        }

        if local_model_path:
            model_path = Path(local_model_path).resolve()

            self._validate_local_embedding_model(
                model_path,
            )

            model_name = str(model_path)

            # 本地模型模式下禁止运行时联网补文件。
            model_kwargs["local_files_only"] = True
        else:
            model_name = "Qwen/Qwen3-Embedding-0.6B"

        document_encode_kwargs: dict[str, Any] = {
            "normalize_embeddings": True,
            "batch_size": embedding_batch_size,
        }

        query_encode_kwargs: dict[str, Any] = {
            "normalize_embeddings": True,
            "batch_size": embedding_batch_size,
        }

        if query_prompt_name:
            query_encode_kwargs["prompt_name"] = query_prompt_name

        self.embeddings = HuggingFaceEmbeddings(
            model_name=model_name,
            model_kwargs=model_kwargs,
            encode_kwargs=document_encode_kwargs,
            query_encode_kwargs=query_encode_kwargs,
        )

        self.vector_store = Chroma(
            collection_name=self.collection_name,
            embedding_function=self.embeddings,
            persist_directory=self.db_persist_dir,
        )

    @staticmethod
    def _validate_local_embedding_model(
        model_path: Path,
    ) -> None:
        """检查本地 Embedding 模型制品是否完整。"""
        if not model_path.is_dir():
            raise FileNotFoundError(f"Embedding 模型目录不存在：{model_path}")

        config_candidates = [
            model_path / "config.json",
            model_path / "0_Transformer" / "config.json",
        ]

        valid_config_path: Path | None = None

        for config_path in config_candidates:
            if not config_path.is_file():
                continue

            try:
                config_data = json.loads(
                    config_path.read_text(
                        encoding="utf-8",
                    )
                )
            except Exception as exc:
                raise ValueError(f"无法读取模型配置：{config_path}") from exc

            if config_data.get("model_type"):
                valid_config_path = config_path
                break

        if valid_config_path is None:
            raise ValueError(
                "Embedding 模型缺少有效 config.json，"
                "或 config.json 中没有 model_type："
                f"{model_path}"
            )

        weight_roots = [
            model_path,
            model_path / "0_Transformer",
        ]

        has_weights = False

        for root in weight_roots:
            if not root.is_dir():
                continue

            if any(root.glob("*.safetensors")):
                has_weights = True
                break

            if any(root.glob("pytorch_model*.bin")):
                has_weights = True
                break

        if not has_weights:
            raise ValueError(
                "Embedding 模型目录缺少 "
                "model.safetensors 或 pytorch_model.bin："
                f"{model_path}"
            )

    @staticmethod
    def _sanitize_metadata_value(value: Any) -> Any:
        """将 metadata 值转换成 Chroma 可接受的基础类型。"""
        if value is None:
            return None

        if isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, Path):
            return str(value)

        if isinstance(value, (datetime, date)):
            return value.isoformat()

        # 兼容 numpy 等标量。
        item_method = getattr(value, "item", None)
        if callable(item_method):
            try:
                item_value = item_method()
                if isinstance(
                    item_value,
                    (str, int, float, bool),
                ):
                    return item_value
            except Exception:
                pass

        if isinstance(value, (list, tuple, set, dict)):
            return json.dumps(
                value,
                ensure_ascii=False,
                default=str,
            )

        return str(value)

    @classmethod
    def _sanitize_metadata(
        cls,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        """删除 None，并清洗复杂 metadata 类型。"""
        sanitized: Dict[str, Any] = {}

        for key, value in metadata.items():
            sanitized_value = cls._sanitize_metadata_value(value)

            if sanitized_value is None:
                continue

            sanitized[str(key)] = sanitized_value

        return sanitized

    def ingest_chunks(
        self,
        chunks: List[Dict[str, Any]],
    ) -> int:
        """批量写入语义切片。

        Returns:
            实际写入的 Chunk 数量。
        """
        if not chunks:
            return 0

        batch_size = self._resolve_batch_size()
        written = 0

        for start in range(0, len(chunks), batch_size):
            current_chunks = chunks[start : start + batch_size]

            batch_docs: list[Document] = []
            batch_ids: list[str] = []

            for index, chunk in enumerate(current_chunks):
                metadata = self._sanitize_metadata(
                    chunk.get("metadata", {})
                )

                chunk_id = str(
                    metadata.get(
                        "chunk_id",
                        f"chunk-{start + index}",
                    )
                )

                batch_docs.append(
                    Document(
                        page_content=str(
                            chunk.get("page_content", "")
                        ),
                        metadata=metadata,
                    )
                )
                batch_ids.append(chunk_id)

            # 相同 ID 先删除再写入，保证重复运行不会产生重复项。
            try:
                self.vector_store.delete(ids=batch_ids)
            except Exception:
                pass

            self.vector_store.add_documents(
                batch_docs,
                ids=batch_ids,
            )

            written += len(batch_docs)

        return written

    def delete_source(self, source: str) -> None:
        """删除某个源文件已有的全部 Chunk。"""
        collection = getattr(
            self.vector_store,
            "_collection",
            None,
        )

        if collection is None:
            return

        try:
            collection.delete(where={"source": source})
            return
        except Exception:
            pass

        # 部分 Chroma 版本不支持直接 where delete，使用查询 ID 兜底。
        try:
            result = collection.get(
                where={"source": source},
                include=[],
            )
            ids = result.get("ids", []) if result else []
        except Exception:
            ids = []

        if ids:
            self.vector_store.delete(ids=list(ids))

    def replace_source_chunks(
        self,
        source: str,
        chunks: List[Dict[str, Any]],
    ) -> int:
        """用当前切片完整替换某个源文件的旧切片。"""
        self.delete_source(source)

        if not chunks:
            return 0

        return self.ingest_chunks(chunks)

    def clear_collection(self) -> int:
        """清空当前 Collection 中的全部向量。"""
        collection = getattr(
            self.vector_store,
            "_collection",
            None,
        )

        if collection is None:
            return 0

        try:
            result = collection.get(include=[])
        except Exception:
            result = collection.get()

        ids = result.get("ids", []) if result else []

        if not ids:
            return 0

        batch_size = self._resolve_batch_size()

        for start in range(0, len(ids), batch_size):
            self.vector_store.delete(
                ids=list(ids[start : start + batch_size])
            )

        return len(ids)

    def count(self) -> int:
        """返回当前 Collection 中的向量数量。"""
        collection = getattr(
            self.vector_store,
            "_collection",
            None,
        )

        if collection is None:
            return 0

        try:
            return int(collection.count())
        except Exception:
            return 0

    def _resolve_batch_size(self) -> int:
        """计算实际使用的写入批次大小。"""
        chroma_limit: int | None = None

        collection = getattr(
            self.vector_store,
            "_collection",
            None,
        )
        client = getattr(collection, "_client", None)

        if client is not None:
            for attr_name in (
                "max_batch_size",
                "get_max_batch_size",
            ):
                attr = getattr(client, attr_name, None)

                if attr is None:
                    continue

                try:
                    value = attr() if callable(attr) else attr
                except Exception:
                    continue

                if isinstance(value, int) and value > 0:
                    chroma_limit = value
                    break

        if chroma_limit is None:
            return self.ingest_batch_size

        return min(
            self.ingest_batch_size,
            chroma_limit,
        )

    def similarity_search(
        self,
        query: str,
        top_k: int = 4,
        metadata_filter: Dict[str, Any] | None = None,
    ) -> List[Document]:
        """执行相似度检索。"""
        kwargs: Dict[str, Any] = {
            "query": query,
            "k": top_k,
        }

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
        kwargs: Dict[str, Any] = {
            "query": query,
            "k": top_k,
        }

        if metadata_filter:
            kwargs["filter"] = metadata_filter

        return (
            self.vector_store
            .similarity_search_with_relevance_scores(**kwargs)
        )

    def search(
        self,
        query: str,
        k: int = 4,
        metadata_filter: Dict[str, Any] | None = None,
    ) -> List[Document]:
        """提供与业务层一致的统一检索入口。"""
        return self.similarity_search(
            query=query,
            top_k=k,
            metadata_filter=metadata_filter,
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
