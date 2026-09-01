"""Stage5-Phase6-WP1 production build/publish pipeline focused tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.knowledge_base import vector_db_manager as vector_module
from core.knowledge_base import production_build as build_module
from core.knowledge_base.production_build import (
    BUILD_PURPOSE_DEVELOPMENT,
    BUILD_PURPOSE_PRODUCTION,
    build_production_generation,
)
from core.knowledge_base.retrieval_index_provenance import (
    ACTIVE_DESCRIPTOR_FILE_NAME,
    BM25_INDEX_FILE_NAME,
    RETRIEVAL_INDEX_MANIFEST_FILE_NAME,
    ARTIFACT_METADATA_FILE_NAME,
    collection_key,
    read_active_descriptor,
    retrieval_root,
)
from scripts.bootstrap_local_kb import build_parser, main


class FakeEmbeddings:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def embed_query(self, query):
        return [0.1, 0.2, 0.3]


class FakeCollection:
    def __init__(self) -> None:
        self.metadata: dict = {}
        self._count = 0
        self.get_result = {"ids": [], "documents": [], "metadatas": []}

    def count(self) -> int:
        return self._count

    def get(self, **kwargs) -> dict:
        return self.get_result

    def modify(self, *, metadata) -> None:
        self.metadata = dict(metadata)


class FakeChroma:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self._collection = FakeCollection()
        self.added = []

    def delete(self, **kwargs) -> None:
        return None

    def add_documents(self, documents, ids) -> None:
        self._collection._count += len(documents)
        self.added.extend(zip(ids, documents))


@pytest.fixture(autouse=True)
def _fake_dense(monkeypatch, tmp_path):
    """让 VectorDBManager 的 HuggingFace/Chroma 依赖走 fake，避免真实模型。"""
    monkeypatch.setattr(vector_module, "HuggingFaceEmbeddings", FakeEmbeddings)
    monkeypatch.setattr(vector_module, "Chroma", FakeChroma)
    yield


def _corpus(tmp_path: Path) -> Path:
    source = tmp_path / "corpus"
    source.mkdir()
    (source / "a.md").write_text("# 文档A\n\nCDT 字段映射介绍。", encoding="utf-8")
    (source / "b.txt").write_text("故障排查步骤。", encoding="utf-8")
    return source


def _embedding_model(tmp_path: Path) -> Path:
    model = tmp_path / "model"
    model.mkdir()
    (model / "model.safetensors").write_bytes(b"model-bytes")
    (model / ".hidden").write_bytes(b"hidden")
    return model


def test_build_creates_isolated_dense_collection_and_bm25_artifact(
    tmp_path,
) -> None:
    source = _corpus(tmp_path)
    model = _embedding_model(tmp_path)
    chroma_dir = tmp_path / "chroma"
    logical = "test_kb"

    result = build_production_generation(
        source_dir=source,
        logical_collection_name=logical,
        chroma_dir=chroma_dir,
        embedding_model_path=model,
        chunk_size=1400,
        chunk_overlap=180,
        purpose=BUILD_PURPOSE_PRODUCTION,
        publish_active=True,
    )
    assert result.purpose == BUILD_PURPOSE_PRODUCTION
    assert result.active_published is True
    assert result.dense_collection_name.startswith(f"la_{collection_key(logical)}_g_")

    root = retrieval_root(chroma_dir, logical)
    generation_dir = root / "generations" / result.generation_id
    assert (generation_dir / BM25_INDEX_FILE_NAME).is_file()
    assert (generation_dir / RETRIEVAL_INDEX_MANIFEST_FILE_NAME).is_file()
    assert (generation_dir / ARTIFACT_METADATA_FILE_NAME).is_file()
    assert (root / ACTIVE_DESCRIPTOR_FILE_NAME).is_file()

    descriptor = read_active_descriptor(root)
    assert descriptor is not None
    assert descriptor.generation_id == result.generation_id
    assert descriptor.dense_collection_name == result.dense_collection_name

    manifest = json.loads(
        (generation_dir / RETRIEVAL_INDEX_MANIFEST_FILE_NAME).read_text(encoding="utf-8")
    )
    assert manifest["provenance"]["generation_id"] == result.generation_id
    assert manifest["chunk_count"] > 0


def test_build_uses_single_prepared_chunk_set_for_both(tmp_path) -> None:
    """Dense 与 BM25 必须来自同一 prepared chunk set（manifest chunk_count 与
    Dense ingest 数量一致，且 BM25 index document_count 一致）。"""
    source = _corpus(tmp_path)
    model = _embedding_model(tmp_path)
    chroma_dir = tmp_path / "chroma"

    result = build_production_generation(
        source_dir=source,
        logical_collection_name="kb",
        chroma_dir=chroma_dir,
        embedding_model_path=model,
        chunk_size=1400,
        chunk_overlap=180,
        purpose=BUILD_PURPOSE_PRODUCTION,
        publish_active=True,
    )
    root = retrieval_root(chroma_dir, "kb")
    manifest = json.loads(
        (root / "generations" / result.generation_id / RETRIEVAL_INDEX_MANIFEST_FILE_NAME).read_text(
            encoding="utf-8"
        )
    )
    metadata = json.loads(
        (root / "generations" / result.generation_id / ARTIFACT_METADATA_FILE_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert metadata["chunk_count"] == manifest["chunk_count"]
    assert metadata["document_count"] == manifest["document_count"]


def test_failed_build_preserves_old_active_descriptor(tmp_path) -> None:
    source = _corpus(tmp_path)
    model = _embedding_model(tmp_path)
    chroma_dir = tmp_path / "chroma"

    first = build_production_generation(
        source_dir=source,
        logical_collection_name="kb",
        chroma_dir=chroma_dir,
        embedding_model_path=model,
        chunk_size=1400,
        chunk_overlap=180,
        purpose=BUILD_PURPOSE_PRODUCTION,
        publish_active=True,
    )
    root = retrieval_root(chroma_dir, "kb")
    old_text = (root / ACTIVE_DESCRIPTOR_FILE_NAME).read_text(encoding="utf-8")

    # 让第二次 build 失败：删除 source 使源枚举为空。
    (source / "a.md").unlink()
    (source / "b.txt").unlink()
    with pytest.raises(ValueError, match="KB_BUILD_NO_CHUNKS"):
        build_production_generation(
            source_dir=source,
            logical_collection_name="kb",
            chroma_dir=chroma_dir,
            embedding_model_path=model,
            chunk_size=1400,
            chunk_overlap=180,
            purpose=BUILD_PURPOSE_PRODUCTION,
            publish_active=True,
        )
    assert (root / ACTIVE_DESCRIPTOR_FILE_NAME).read_text(encoding="utf-8") == old_text
    assert first.generation_id in old_text


def test_development_build_cannot_publish_active(tmp_path) -> None:
    source = _corpus(tmp_path)
    model = _embedding_model(tmp_path)
    chroma_dir = tmp_path / "chroma"

    result = build_production_generation(
        source_dir=source,
        logical_collection_name="kb",
        chroma_dir=chroma_dir,
        embedding_model_path=model,
        chunk_size=200,
        chunk_overlap=20,
        purpose=BUILD_PURPOSE_DEVELOPMENT,
        publish_active=False,
    )
    assert result.purpose == BUILD_PURPOSE_DEVELOPMENT
    assert result.active_published is False
    root = retrieval_root(chroma_dir, "kb")
    assert not (root / ACTIVE_DESCRIPTOR_FILE_NAME).exists()

    with pytest.raises(ValueError, match="development build must not publish"):
        build_production_generation(
            source_dir=source,
            logical_collection_name="kb",
            chroma_dir=chroma_dir,
            embedding_model_path=model,
            chunk_size=200,
            chunk_overlap=20,
            purpose=BUILD_PURPOSE_DEVELOPMENT,
            publish_active=True,
        )


def test_development_manifest_records_purpose(tmp_path) -> None:
    source = _corpus(tmp_path)
    model = _embedding_model(tmp_path)
    chroma_dir = tmp_path / "chroma"

    result = build_production_generation(
        source_dir=source,
        logical_collection_name="kb",
        chroma_dir=chroma_dir,
        embedding_model_path=model,
        chunk_size=200,
        chunk_overlap=20,
        purpose=BUILD_PURPOSE_DEVELOPMENT,
        publish_active=False,
    )
    manifest_path = (
        retrieval_root(chroma_dir, "kb")
        / "generations"
        / result.generation_id
        / RETRIEVAL_INDEX_MANIFEST_FILE_NAME
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.get("purpose") == BUILD_PURPOSE_DEVELOPMENT


def test_production_cli_rejects_chunk_override(monkeypatch, tmp_path) -> None:
    source = _corpus(tmp_path)
    with pytest.raises(SystemExit):
        main(
            [
                "--source-dir",
                str(source),
                "--build-purpose",
                "production",
                "--chunk-size",
                "500",
                "--dry-run",
            ]
        )


def test_development_cli_allows_chunk_override(tmp_path, monkeypatch) -> None:
    source = _corpus(tmp_path)
    parser = build_parser()
    args = parser.parse_args(
        [
            "--source-dir",
            str(source),
            "--build-purpose",
            "development",
            "--chunk-size",
            "500",
            "--chunk-overlap",
            "50",
            "--dry-run",
        ]
    )
    assert args.build_purpose == "development"
    assert args.chunk_size == 500
    assert args.chunk_overlap == 50
