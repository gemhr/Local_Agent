"""Stage5 Phase3 可重建 RAG Evaluation KB 环境。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.knowledge_base.document_loader import (
    iter_supported_files,
    load_document_file,
    split_documents,
)
from core.knowledge_base.vector_db_manager import VectorDBManager

CORPUS_ID = "rag-evaluation-corpus.v1"
COLLECTION_NAME = "rag_evaluation_kb_v1"
TRUTHFULNESS_LABEL = "SYNTHETIC_RAG_EVALUATION_CORPUS"
CHUNK_SIZE = 1400
CHUNK_OVERLAP = 180
INGEST_BATCH_ID = CORPUS_ID
EMBEDDING_MODEL_NAME = "Qwen3-Embedding-0.6B"
PURPOSE = "evaluation"


@dataclass(frozen=True, slots=True)
class EvaluationKbManifest:
    corpus_id: str
    collection_name: str
    document_count: int
    chunk_count: int
    embedding_model_name: str
    embedding_dimension: int
    chunk_size: int
    chunk_overlap: int
    chunks: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_id": self.corpus_id,
            "collection_name": self.collection_name,
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
            "embedding_model_name": self.embedding_model_name,
            "embedding_dimension": self.embedding_dimension,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "chunks": list(self.chunks),
        }


def default_corpus_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "evaluation_assets" / "rag_kb_v1" / "documents"


def prepare_evaluation_chunks(corpus_dir: Path) -> tuple[list[dict[str, Any]], int]:
    root = corpus_dir.resolve(strict=True)
    files = list(iter_supported_files(str(root)))
    chunks: list[dict[str, Any]] = []
    for path in files:
        documents = load_document_file(path, root)
        chunks.extend(
            split_documents(
                documents,
                chunk_size=CHUNK_SIZE,
                chunk_overlap=CHUNK_OVERLAP,
                ingest_batch_id=INGEST_BATCH_ID,
            )
        )
    return chunks, len(files)


def _assert_fresh_persistence(persist_dir: Path) -> None:
    resolved = persist_dir.resolve(strict=False)
    if resolved.exists() and any(resolved.iterdir()):
        raise ValueError("evaluation persistence must be new or empty")


def build_evaluation_kb(
    *,
    persist_dir: Path,
    embedding_model_path: Path,
    corpus_dir: Path | None = None,
    embedding_batch_size: int = 8,
    query_prompt_name: str | None = None,
) -> tuple[VectorDBManager, EvaluationKbManifest]:
    """只在 fresh persistence 中构建专用 Evaluation collection。"""
    _assert_fresh_persistence(persist_dir)
    chunks, document_count = prepare_evaluation_chunks(corpus_dir or default_corpus_dir())
    if not chunks:
        raise ValueError("evaluation corpus produced no chunks")
    manager = VectorDBManager(
        db_persist_dir=str(persist_dir),
        local_model_path=str(embedding_model_path),
        collection_name=COLLECTION_NAME,
        ingest_batch_size=32,
        embedding_batch_size=embedding_batch_size,
        query_prompt_name=query_prompt_name,
    )
    written = manager.ingest_chunks(chunks)
    if written != len(chunks):
        raise RuntimeError("evaluation KB ingest count mismatch")
    manager.publish_collection_marker()
    collection = manager.vector_store._collection
    metadata = dict(collection.metadata or {})
    dimension = manager.embedding_dimension()
    metadata.update(
        {
            "purpose": PURPOSE,
            "corpus_id": CORPUS_ID,
            "truthfulness_label": TRUTHFULNESS_LABEL,
            "embedding_model": EMBEDDING_MODEL_NAME,
            "embedding_dimension": dimension,
            "chunk_size": CHUNK_SIZE,
            "chunk_overlap": CHUNK_OVERLAP,
        }
    )
    collection.modify(metadata=metadata)
    identities = tuple(
        {
            "document_id": str(item["metadata"]["doc_id"]),
            "chunk_id": str(item["metadata"]["chunk_id"]),
            "source": str(item["metadata"]["source"]),
            "section_path": str(item["metadata"].get("section_path") or ""),
            "content_hash": str(item["metadata"]["content_hash"]),
        }
        for item in chunks
    )
    return manager, EvaluationKbManifest(
        corpus_id=CORPUS_ID,
        collection_name=COLLECTION_NAME,
        document_count=document_count,
        chunk_count=len(chunks),
        embedding_model_name=EMBEDDING_MODEL_NAME,
        embedding_dimension=dimension,
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        chunks=identities,
    )


def manifest_json(manifest: EvaluationKbManifest) -> str:
    return json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True)


__all__ = [
    "CHUNK_OVERLAP",
    "CHUNK_SIZE",
    "COLLECTION_NAME",
    "CORPUS_ID",
    "EMBEDDING_MODEL_NAME",
    "EvaluationKbManifest",
    "TRUTHFULNESS_LABEL",
    "build_evaluation_kb",
    "default_corpus_dir",
    "manifest_json",
    "prepare_evaluation_chunks",
]
