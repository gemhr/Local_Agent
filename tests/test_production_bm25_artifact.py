"""Stage5-Phase6-WP1 production BM25 artifact contract focused tests."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from core.knowledge_base.bm25_sparse_index import (
    BM25_ALGORITHM_REF,
    BM25_B,
    BM25_INDEX_SCHEMA_VERSION,
    BM25_K1,
    BM25_TOKENIZER_REF,
)
from core.knowledge_base.document_loader import chunk_policy_from
from core.knowledge_base.production_bm25_artifact import (
    BM25_ARTIFACT_SCHEMA_VERSION,
    BM25_ARTIFACT_STATUS_READY,
    ProductionBm25Artifact,
    build_production_bm25_artifact,
    validate_production_bm25_artifact,
)
from core.knowledge_base.retrieval_index_provenance import (
    PROVENANCE_CONTRACT_VERSION,
    RetrievalIndexProvenance,
    chunk_policy_digest,
    new_generation_id,
    ordered_chunk_manifest_digest,
)


def _chunk(document_id: str, chunk_id: str, source: str, text: str) -> dict:
    return {
        "page_content": text,
        "metadata": {
            "doc_id": document_id,
            "chunk_id": chunk_id,
            "source": source,
            "section_path": "S",
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "chunk_index": 0,
        },
    }


def _provenance(chunks) -> RetrievalIndexProvenance:
    chunk_policy = chunk_policy_from(chunk_size=1400, chunk_overlap=180)
    return RetrievalIndexProvenance(
        generation_id=new_generation_id(),
        corpus_id="kb",
        source_manifest_sha256="a" * 64,
        chunk_policy=chunk_policy,
        chunk_policy_sha256=chunk_policy_digest(chunk_policy),
        chunk_manifest_sha256=ordered_chunk_manifest_digest(chunks),
        document_count=1,
        chunk_count=len(chunks),
    )


def _build(tmp_path: Path):
    chunks = [_chunk("d1", "c1", "a.md", "CDT 字段映射 alpha"), _chunk("d1", "c2", "a.md", "CDT 字段映射 beta")]
    provenance = _provenance(chunks)
    generation_dir = tmp_path / "generations" / provenance.generation_id
    artifact = build_production_bm25_artifact(
        generation_dir=generation_dir,
        provenance=provenance,
        chunks=chunks,
        document_count=1,
    )
    return chunks, provenance, generation_dir, artifact


def test_valid_artifact_load(tmp_path) -> None:
    _, provenance, generation_dir, artifact = _build(tmp_path)
    assert artifact.generation_id == provenance.generation_id
    assert artifact.index.document_count == 2
    validated = validate_production_bm25_artifact(
        generation_dir=generation_dir,
        expected_provenance=provenance,
        expected_provenance_sha256=provenance.provenance_sha256(),
    )
    assert validated.provenance.generation_id == provenance.generation_id


def test_missing_file_rejected(tmp_path) -> None:
    _, _, generation_dir, _ = _build(tmp_path)
    (generation_dir / "bm25_index.json").unlink()
    with pytest.raises(ValueError, match="BM25_ARTIFACT_INCOMPLETE"):
        validate_production_bm25_artifact(generation_dir=generation_dir)


def test_corrupt_file_rejected(tmp_path) -> None:
    _, _, generation_dir, _ = _build(tmp_path)
    (generation_dir / "bm25_index.json").write_text("{corrupt", encoding="utf-8")
    with pytest.raises(ValueError):
        validate_production_bm25_artifact(generation_dir=generation_dir)


def test_file_digest_mismatch_rejected(tmp_path) -> None:
    _, _, generation_dir, _ = _build(tmp_path)
    metadata_path = generation_dir / "artifact_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["index_file_sha256"] = "f" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="index file digest mismatch"):
        validate_production_bm25_artifact(generation_dir=generation_dir)


def test_provenance_mismatch_rejected(tmp_path) -> None:
    chunks, _, generation_dir, _ = _build(tmp_path)
    other = _provenance(chunks)
    with pytest.raises(ValueError, match="PROVENANCE_MISMATCH"):
        validate_production_bm25_artifact(
            generation_dir=generation_dir,
            expected_provenance=other,
        )


def test_generation_mismatch_rejected(tmp_path) -> None:
    chunks, provenance, generation_dir, _ = _build(tmp_path)
    tampered = RetrievalIndexProvenance(
        generation_id=new_generation_id(),
        corpus_id=provenance.corpus_id,
        source_manifest_sha256=provenance.source_manifest_sha256,
        chunk_policy=provenance.chunk_policy,
        chunk_policy_sha256=provenance.chunk_policy_sha256,
        chunk_manifest_sha256=provenance.chunk_manifest_sha256,
        document_count=provenance.document_count,
        chunk_count=provenance.chunk_count,
    )
    with pytest.raises(ValueError, match="generation"):
        validate_production_bm25_artifact(
            generation_dir=generation_dir,
            expected_provenance=tampered,
        )


def test_frozen_bm25_contract_mismatch_rejected(tmp_path) -> None:
    _, _, generation_dir, _ = _build(tmp_path)
    metadata_path = generation_dir / "artifact_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["k1"] = 9.9
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen BM25 field mismatch"):
        validate_production_bm25_artifact(generation_dir=generation_dir)


def test_not_ready_rejected(tmp_path) -> None:
    _, _, generation_dir, _ = _build(tmp_path)
    metadata_path = generation_dir / "artifact_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["status"] = "BUILDING"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="not READY"):
        validate_production_bm25_artifact(generation_dir=generation_dir)


def test_artifact_metadata_records_frozen_fields(tmp_path) -> None:
    _, provenance, generation_dir, _ = _build(tmp_path)
    metadata = json.loads((generation_dir / "artifact_metadata.json").read_text(encoding="utf-8"))
    assert metadata["schema_version"] == BM25_ARTIFACT_SCHEMA_VERSION
    assert metadata["status"] == BM25_ARTIFACT_STATUS_READY
    assert metadata["provenance_contract_version"] == PROVENANCE_CONTRACT_VERSION
    assert metadata["generation_id"] == provenance.generation_id
    assert metadata["bm25_schema_version"] == BM25_INDEX_SCHEMA_VERSION
    assert metadata["algorithm_ref"] == BM25_ALGORITHM_REF
    assert metadata["tokenizer_ref"] == BM25_TOKENIZER_REF
    assert metadata["k1"] == BM25_K1
    assert metadata["b"] == BM25_B
