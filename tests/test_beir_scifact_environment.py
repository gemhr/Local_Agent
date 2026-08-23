from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.knowledge_base.beir_scifact_environment import (
    BEIR_BENCHMARK,
    BEIR_SCIFACT_COLLECTION_NAME,
    BEIR_SCIFACT_CORPUS_ID,
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    SCIFACT_DATASET,
    beir_scifact_cache_identity,
    load_beir_scifact_cache,
    materialize_beir_corpus,
    prepare_beir_scifact_chunks,
    verify_beir_scifact_split_determinism,
)
import core.knowledge_base.beir_scifact_environment as beir_environment


def _write_tiny_corpus(path: Path) -> None:
    documents = [
        {"_id": "1", "title": "Study A", "text": "Finding one about retrieval."},
        {"_id": "2", "title": "Study B", "text": "Finding two about ranking."},
        {"_id": "3", "title": "Study C", "text": "Finding three about evaluation."},
    ]
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in documents) + "\n",
        encoding="utf-8",
    )


def test_materialize_writes_markdown_documents(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.jsonl"
    _write_tiny_corpus(corpus_path)
    target = tmp_path / "materialized"

    count = materialize_beir_corpus(corpus_path, target)

    assert count == 3
    assert sorted(item.name for item in target.iterdir()) == ["1.md", "2.md", "3.md"]
    content = (target / "1.md").read_text(encoding="utf-8")
    assert content == "# Study A\n\nFinding one about retrieval."


def test_materialize_fails_closed_on_unsafe_document_id(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.jsonl"
    corpus_path.write_text(
        json.dumps({"_id": "../escape", "title": "t", "text": "x"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="filename-safe"):
        materialize_beir_corpus(corpus_path, tmp_path / "materialized")


def test_prepare_chunks_attach_benchmark_document_identity(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.jsonl"
    _write_tiny_corpus(corpus_path)
    materialized = tmp_path / "materialized"
    materialize_beir_corpus(corpus_path, materialized)

    chunks, file_count, mapping = prepare_beir_scifact_chunks(materialized)

    assert file_count == 3
    assert len(chunks) >= 3
    assert len(mapping) == 3
    benchmark_ids = {mapping[doc_id] for doc_id in mapping}
    assert benchmark_ids == {"1", "2", "3"}
    for chunk in chunks:
        metadata = chunk["metadata"]
        assert metadata["benchmark"] == BEIR_BENCHMARK
        assert metadata["benchmark_dataset"] == SCIFACT_DATASET
        assert metadata["benchmark_document_id"] in {"1", "2", "3"}
        assert metadata["ingest_batch_id"] == BEIR_SCIFACT_CORPUS_ID
        assert (
            metadata["benchmark_document_id"]
            == mapping[metadata["doc_id"]]
        )


def test_prepare_chunks_are_deterministic(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.jsonl"
    _write_tiny_corpus(corpus_path)
    materialized = tmp_path / "materialized"
    materialize_beir_corpus(corpus_path, materialized)

    first, first_files, first_mapping = prepare_beir_scifact_chunks(materialized)
    second, second_files, second_mapping = prepare_beir_scifact_chunks(materialized)

    first_identity = [
        (item["metadata"]["doc_id"], item["metadata"]["chunk_id"], item["metadata"]["content_hash"])
        for item in first
    ]
    second_identity = [
        (item["metadata"]["doc_id"], item["metadata"]["chunk_id"], item["metadata"]["content_hash"])
        for item in second
    ]
    assert first_identity == second_identity
    assert len(first_identity) == len(set(first_identity))
    assert first_files == second_files
    assert first_mapping == second_mapping
    verify_beir_scifact_split_determinism(materialized)


def test_frozen_environment_constants() -> None:
    assert BEIR_SCIFACT_CORPUS_ID == "beir-scifact-corpus.v1"
    assert BEIR_SCIFACT_COLLECTION_NAME == "beir_scifact_eval_v1"
    assert CHUNK_SIZE == 1400
    assert CHUNK_OVERLAP == 180


def test_dense_cache_identity_is_deterministic_and_excludes_query_time_configuration() -> None:
    baseline = beir_scifact_cache_identity(
        corpus_sha256="corpus-a", manifest_sha256="manifest-a"
    )
    assert baseline == beir_scifact_cache_identity(
        corpus_sha256="corpus-a", manifest_sha256="manifest-a"
    )
    # candidate_limit / rerank / selection are query-time behavior, not index semantics.
    assert baseline.cache_key == beir_scifact_cache_identity(
        corpus_sha256="corpus-a", manifest_sha256="manifest-a"
    ).cache_key


@pytest.mark.parametrize(
    "changes",
    [
        {"embedding_model": "other"},
        {"corpus_sha256": "corpus-b"},
        {"manifest_sha256": "manifest-b"},
        {"chunk_size": 1200},
    ],
)
def test_dense_cache_identity_changes_when_index_semantics_change(changes: dict[str, object]) -> None:
    baseline = beir_scifact_cache_identity(
        corpus_sha256="corpus-a", manifest_sha256="manifest-a"
    )
    inputs: dict[str, object] = {
        "corpus_sha256": "corpus-a",
        "manifest_sha256": "manifest-a",
    }
    inputs.update(changes)
    changed = beir_scifact_cache_identity(**inputs)  # type: ignore[arg-type]
    assert changed.cache_key != baseline.cache_key


def test_ready_cache_reuse_validates_collection_metadata_without_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = beir_scifact_cache_identity(corpus_sha256="corpus", manifest_sha256="chunks")
    cache_dir = tmp_path / identity.cache_key
    chroma_dir = cache_dir / "chroma"
    chroma_dir.mkdir(parents=True)
    manifest_path = cache_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"documents": [{"document_id": "local-1"}]}), encoding="utf-8")
    metadata: dict[str, object] = {
        "cache_schema_version": "beir-scifact-dense-index-cache.v1", "cache_status": "READY",
        "cache_key": identity.cache_key, "benchmark": "beir", "dataset": "scifact", "split": "test",
        "corpus_sha256": "corpus", "manifest_sha256": "chunks", "document_count": 1, "chunk_count": 1,
        "embedding_model": "Qwen3-Embedding-0.6B", "embedding_dimension": 1024,
        "embedding_local_files_only": True, "embedding_query_prompt": "",
        "splitter_identity": "structure-aware-splitter.v2", "chunk_size": 1400, "chunk_overlap": 180,
        "collection_name": "beir_scifact_eval_v1", "corpus_id": "beir-scifact-corpus.v1",
        "manifest_file_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    (cache_dir / "cache_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    collection_metadata = dict(metadata)

    class _Collection:
        @staticmethod
        def count() -> int:
            return 1

    _Collection.metadata = collection_metadata

    class _Manager:
        def __init__(self, **_kwargs: object) -> None:
            self.vector_store = type("Store", (), {"_collection": _Collection()})()

    monkeypatch.setattr(beir_environment, "VectorDBManager", _Manager)
    _manager, result = load_beir_scifact_cache(
        cache_dir=cache_dir, embedding_model_path=tmp_path / "model"
    )
    assert result.status == "CACHE_HIT"

    collection_metadata["cache_key"] = "wrong"
    with pytest.raises(ValueError, match="collection metadata mismatch"):
        load_beir_scifact_cache(cache_dir=cache_dir, embedding_model_path=tmp_path / "model")
