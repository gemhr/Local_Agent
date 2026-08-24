"""Synthetic WP4 Dense cache identity 与 READY lifecycle（fail-closed + load-by-identity）。"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from core.knowledge_base import evaluation_environment as ee

MODEL_DIR_NAME = ee.EMBEDDING_MODEL_NAME


def _identity(**overrides) -> ee.EvaluationDenseCacheIdentity:
    base = {"source_manifest_sha256": "src-a", "chunk_manifest_sha256": "chunks-a"}
    base.update(overrides)
    return ee.evaluation_dense_cache_identity(**base)


def _write_ready_cache(
    tmp_path: Path,
    *,
    document_count: int = 1,
    chunk_count: int = 3,
    source_manifest_sha256: str = "src-a",
) -> tuple[Path, ee.EvaluationDenseCacheIdentity]:
    """写一个 self-consistent READY cache：manifest chunks digest == identity chunk digest。"""
    manifest_chunks = [
        {"document_id": "d", "chunk_id": f"c{i}", "source": "s", "section_path": "", "content_hash": "h"}
        for i in range(chunk_count)
    ]
    chunk_digest = ee._identity_list_digest(manifest_chunks)
    identity = ee.evaluation_dense_cache_identity(
        source_manifest_sha256=source_manifest_sha256,
        chunk_manifest_sha256=chunk_digest,
    )
    cache_dir = tmp_path / identity.cache_key
    chroma_dir = cache_dir / "chroma"
    chroma_dir.mkdir(parents=True)
    manifest_path = cache_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "corpus_id": ee.CORPUS_ID,
                "document_count": document_count,
                "chunk_count": chunk_count,
                "chunks": manifest_chunks,
            }
        ),
        encoding="utf-8",
    )
    metadata: dict[str, object] = {
        "cache_schema_version": ee.DENSE_CACHE_SCHEMA_VERSION,
        "cache_status": ee.CACHE_READY,
        "cache_key": identity.cache_key,
        "corpus_id": ee.CORPUS_ID,
        "source_manifest_sha256": identity.source_manifest_sha256,
        "chunk_manifest_sha256": identity.chunk_manifest_sha256,
        "document_count": document_count,
        "chunk_count": chunk_count,
        "embedding_model": ee.EMBEDDING_MODEL_NAME,
        "embedding_dimension": identity.embedding_dimension,
        "embedding_local_files_only": True,
        "normalize_embeddings": True,
        "embedding_query_prompt": "",
        "splitter_identity": ee.SPLITTER_IDENTITY,
        "chunk_size": ee.CHUNK_SIZE,
        "chunk_overlap": ee.CHUNK_OVERLAP,
        "collection_name": ee.COLLECTION_NAME,
        "manifest_file_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    (cache_dir / "cache_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return cache_dir, identity


def _validate(cache_dir: Path, identity, *, doc=1, chunk=3) -> None:
    ee._validate_ready_dense_cache(
        cache_dir=cache_dir,
        identity=identity,
        document_count=doc,
        chunk_count=chunk,
        embedding_model=ee.EMBEDDING_MODEL_NAME,
        query_prompt="",
    )


# ---------------------------------------------------------------------------
# Manifest digests
# ---------------------------------------------------------------------------


def test_source_manifest_digest_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    d1 = tmp_path / "corpus-a"
    d1.mkdir()
    (d1 / "b.md").write_text("beta", encoding="utf-8")
    (d1 / "a.md").write_text("alpha", encoding="utf-8")
    assert ee.source_manifest_digest(d1) == ee.source_manifest_digest(d1)
    d2 = tmp_path / "corpus-b"
    d2.mkdir()
    (d2 / "a.md").write_text("ALPHA", encoding="utf-8")
    (d2 / "b.md").write_text("beta", encoding="utf-8")
    assert ee.source_manifest_digest(d1) != ee.source_manifest_digest(d2)


def test_ordered_chunk_manifest_digest_is_stable_and_chunk_sensitive(tmp_path: Path) -> None:
    chunks = ee.prepare_evaluation_chunks(ee.default_corpus_dir())[0]
    assert ee.ordered_chunk_manifest_digest(chunks) == ee.ordered_chunk_manifest_digest(chunks)
    assert len(ee.ordered_chunk_identities(chunks)) == 60
    altered = json.loads(json.dumps(chunks))
    altered[0]["metadata"]["content_hash"] = "changed"
    assert ee.ordered_chunk_manifest_digest(altered) != ee.ordered_chunk_manifest_digest(chunks)


def test_dense_cache_identity_is_deterministic_and_excludes_query_time_config(tmp_path: Path) -> None:
    baseline = _identity()
    assert baseline == _identity()
    assert baseline.cache_key == _identity().cache_key


@pytest.mark.parametrize(
    "changes",
    [
        {"source_manifest_sha256": "src-b"},
        {"chunk_manifest_sha256": "chunks-b"},
        {"embedding_model": "other"},
        {"embedding_dimension": 768},
        {"embedding_local_files_only": False},
        {"normalize_embeddings": False},
        {"embedding_query_prompt": "p"},
        {"splitter_identity": "other"},
        {"chunk_size": 1200},
        {"chunk_overlap": 100},
        {"collection_name": "other"},
    ],
)
def test_dense_cache_identity_changes_when_semantics_change(changes: dict[str, object]) -> None:
    baseline = _identity()
    changed = _identity(**changes)
    assert changed.cache_key != baseline.cache_key


# ---------------------------------------------------------------------------
# Load-by-identity：external expected identity 作为 Authority
# ---------------------------------------------------------------------------


def test_dense_ready_load_is_cache_hit_and_validates_collection_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir, identity = _write_ready_cache(tmp_path, chunk_count=3)
    collection = _FakeCollection(
        {
            "cache_schema_version": ee.DENSE_CACHE_SCHEMA_VERSION,
            "cache_key": identity.cache_key,
            "source_manifest_sha256": identity.source_manifest_sha256,
            "chunk_manifest_sha256": identity.chunk_manifest_sha256,
        },
        count=3,
    )

    def _manager(**kwargs):
        return _FakeManager(collection)

    monkeypatch.setattr(ee, "VectorDBManager", _manager)
    manager, result = ee.load_evaluation_dense_cache(
        cache_dir=cache_dir,
        expected_identity=identity,
        expected_document_count=1,
        expected_chunk_count=3,
        embedding_model_path=tmp_path / MODEL_DIR_NAME,
    )
    assert result.status == "CACHE_HIT"
    assert manager.ingest_calls == 0  # no re-embedding on warm load

    collection.metadata["cache_key"] = "wrong"
    with pytest.raises(ValueError, match="collection metadata mismatch"):
        ee.load_evaluation_dense_cache(
            cache_dir=cache_dir,
            expected_identity=identity,
            expected_document_count=1,
            expected_chunk_count=3,
            embedding_model_path=tmp_path / MODEL_DIR_NAME,
        )


def test_dense_missing_ready_fails_closed(tmp_path: Path) -> None:
    identity = _identity()
    cache_dir = tmp_path / identity.cache_key
    cache_dir.mkdir()
    with pytest.raises(ValueError, match="CACHE_INCOMPLETE"):
        _validate(cache_dir, identity)


def test_dense_partial_cache_fails_closed(tmp_path: Path) -> None:
    cache_dir, identity = _write_ready_cache(tmp_path, chunk_count=3)
    (cache_dir / "manifest.json").unlink()
    with pytest.raises(ValueError, match="CACHE_INCOMPLETE"):
        _validate(cache_dir, identity)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda m: m.update({"cache_key": "wrong"}),
        lambda m: m.update({"corpus_id": "other"}),
        lambda m: m.update({"source_manifest_sha256": "wrong"}),
        lambda m: m.update({"chunk_manifest_sha256": "wrong"}),
        lambda m: m.update({"document_count": 99}),
        lambda m: m.update({"chunk_count": 99}),
        lambda m: m.update({"embedding_model": "other"}),
        lambda m: m.update({"embedding_dimension": 768}),
        lambda m: m.update({"normalize_embeddings": False}),
        lambda m: m.update({"embedding_query_prompt": "p"}),
        lambda m: m.update({"splitter_identity": "other"}),
        lambda m: m.update({"chunk_size": 1200}),
    ],
)
def test_dense_stale_or_wrong_metadata_fails_closed(tmp_path: Path, tamper) -> None:
    cache_dir, identity = _write_ready_cache(tmp_path, chunk_count=3)
    metadata_path = cache_dir / "cache_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    tamper(metadata)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata mismatch"):
        _validate(cache_dir, identity)


def test_dense_manifest_file_digest_mismatch_fails_closed(tmp_path: Path) -> None:
    cache_dir, identity = _write_ready_cache(tmp_path, chunk_count=3)
    manifest = json.loads((cache_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["chunks"][0]["chunk_id"] = "c-changed"
    (cache_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest completion artifact mismatch"):
        _validate(cache_dir, identity)


# ---------------------------------------------------------------------------
# P1-02 Forgery regressions（load-by-identity 必须 reject）
# ---------------------------------------------------------------------------


def test_dense_forged_self_described_metadata_rejected(tmp_path: Path) -> None:
    """metadata 改成 self-consistent fake（cache_key/source/chunk 同步），
    但 external expected identity 不变 → reject。"""
    cache_dir, identity = _write_ready_cache(tmp_path, chunk_count=3)
    forged = ee.evaluation_dense_cache_identity(
        source_manifest_sha256="forged-src", chunk_manifest_sha256="forged-chunk"
    )
    metadata = json.loads((cache_dir / "cache_metadata.json").read_text(encoding="utf-8"))
    metadata["cache_key"] = forged.cache_key
    metadata["source_manifest_sha256"] = forged.source_manifest_sha256
    metadata["chunk_manifest_sha256"] = forged.chunk_manifest_sha256
    (cache_dir / "cache_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata mismatch"):
        ee.load_evaluation_dense_cache(
            cache_dir=cache_dir,
            expected_identity=identity,
            expected_document_count=1,
            expected_chunk_count=3,
            embedding_model_path=tmp_path / MODEL_DIR_NAME,
        )


def test_dense_wrong_caller_expected_identity_rejected(tmp_path: Path) -> None:
    cache_dir, _identity_actual = _write_ready_cache(tmp_path, chunk_count=3)
    wrong = ee.evaluation_dense_cache_identity(
        source_manifest_sha256="src-b", chunk_manifest_sha256="chunks-b"
    )
    with pytest.raises(ValueError, match="directory identity mismatch"):
        ee.load_evaluation_dense_cache(
            cache_dir=cache_dir,
            expected_identity=wrong,
            expected_document_count=1,
            expected_chunk_count=3,
            embedding_model_path=tmp_path / MODEL_DIR_NAME,
        )


def test_dense_directory_identity_mismatch_rejected(tmp_path: Path) -> None:
    cache_dir, identity = _write_ready_cache(tmp_path, chunk_count=3)
    moved = tmp_path / "renamed-dir"
    shutil.move(str(cache_dir), str(moved))
    with pytest.raises(ValueError, match="directory identity mismatch"):
        ee.load_evaluation_dense_cache(
            cache_dir=moved,
            expected_identity=identity,
            expected_document_count=1,
            expected_chunk_count=3,
            embedding_model_path=tmp_path / MODEL_DIR_NAME,
        )


def test_dense_wrong_metadata_model_rejected(tmp_path: Path) -> None:
    cache_dir, identity = _write_ready_cache(tmp_path, chunk_count=3)
    metadata = json.loads((cache_dir / "cache_metadata.json").read_text(encoding="utf-8"))
    metadata["embedding_model"] = "other-model"
    (cache_dir / "cache_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata mismatch"):
        _validate(cache_dir, identity)


def test_dense_wrong_metadata_prompt_rejected(tmp_path: Path) -> None:
    cache_dir, identity = _write_ready_cache(tmp_path, chunk_count=3)
    metadata = json.loads((cache_dir / "cache_metadata.json").read_text(encoding="utf-8"))
    metadata["embedding_query_prompt"] = "p"
    (cache_dir / "cache_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata mismatch"):
        _validate(cache_dir, identity)


def test_dense_changed_manifest_with_old_chunk_digest_rejected(tmp_path: Path) -> None:
    """manifest 内容改变 + 同步更新 manifest file SHA + 保留旧 chunk digest → reject。"""
    cache_dir, identity = _write_ready_cache(tmp_path, chunk_count=3)
    manifest = json.loads((cache_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["chunks"][0]["chunk_id"] = "c-changed"
    (cache_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    metadata = json.loads((cache_dir / "cache_metadata.json").read_text(encoding="utf-8"))
    metadata["manifest_file_sha256"] = hashlib.sha256((cache_dir / "manifest.json").read_bytes()).hexdigest()
    (cache_dir / "cache_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest ordered chunk digest mismatch"):
        _validate(cache_dir, identity)


def test_dense_forged_semantic_digest_with_metadata_rejected(tmp_path: Path) -> None:
    """manifest semantic digest 改变 + metadata 同步改，但 external expected digest 不变 → reject。"""
    cache_dir, identity = _write_ready_cache(tmp_path, chunk_count=3)
    manifest = json.loads((cache_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["chunks"][0]["chunk_id"] = "c-forged"
    (cache_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    metadata = json.loads((cache_dir / "cache_metadata.json").read_text(encoding="utf-8"))
    metadata["chunk_manifest_sha256"] = ee._identity_list_digest(manifest["chunks"])
    metadata["manifest_file_sha256"] = hashlib.sha256((cache_dir / "manifest.json").read_bytes()).hexdigest()
    (cache_dir / "cache_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata mismatch"):
        _validate(cache_dir, identity)


# ---------------------------------------------------------------------------
# P1-01：Dense frozen model ref 必须在 cold build 前强制
# ---------------------------------------------------------------------------


def test_dense_cold_build_publishes_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_digest = ee.source_manifest_digest(ee.default_corpus_dir())
    chunks, document_count = ee.prepare_evaluation_chunks(ee.default_corpus_dir())
    chunk_digest = ee.ordered_chunk_manifest_digest(chunks)
    identity = ee.evaluation_dense_cache_identity(
        source_manifest_sha256=source_digest,
        chunk_manifest_sha256=chunk_digest,
        embedding_model=ee.EMBEDDING_MODEL_NAME,
    )
    collection = _FakeCollection({}, count=len(chunks))

    def _fake_build_evaluation_kb(*, persist_dir, embedding_model_path, corpus_dir, embedding_batch_size, query_prompt_name):
        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        manifest = ee.EvaluationKbManifest(
            corpus_id=ee.CORPUS_ID,
            collection_name=ee.COLLECTION_NAME,
            document_count=document_count,
            chunk_count=len(chunks),
            embedding_model_name=ee.EMBEDDING_MODEL_NAME,
            embedding_dimension=ee.DENSE_DIMENSION,
            chunk_size=ee.CHUNK_SIZE,
            chunk_overlap=ee.CHUNK_OVERLAP,
            chunks=tuple(ee.ordered_chunk_identities(chunks)),
        )
        return _FakeManager(collection, query_prompt_name=None), manifest

    monkeypatch.setattr(ee, "build_evaluation_kb", _fake_build_evaluation_kb)
    model_dir = tmp_path / MODEL_DIR_NAME
    model_dir.mkdir()
    result = ee.build_or_reuse_evaluation_dense_cache(
        corpus_dir=ee.default_corpus_dir(),
        cache_root=tmp_path / "dense-cache",
        embedding_model_path=model_dir,
    )
    assert result.status == "CACHE_BUILT"
    assert result.cache_dir.name == identity.cache_key
    reused = ee.build_or_reuse_evaluation_dense_cache(
        corpus_dir=ee.default_corpus_dir(),
        cache_root=tmp_path / "dense-cache",
        embedding_model_path=model_dir,
    )
    assert reused.status == "CACHE_HIT"
    assert reused.identity == identity
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["cache_status"] == ee.CACHE_READY
    assert metadata["cache_schema_version"] == ee.DENSE_CACHE_SCHEMA_VERSION


def test_dense_wrong_model_basename_fails_closed_no_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wrong model basename + dimension 仍 1024 → FAIL CLOSED，不得创建 READY artifact。"""
    chunks, document_count = ee.prepare_evaluation_chunks(ee.default_corpus_dir())
    collection = _FakeCollection({}, count=len(chunks))

    def _fake_build_evaluation_kb(*, persist_dir, embedding_model_path, corpus_dir, embedding_batch_size, query_prompt_name):
        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        manifest = ee.EvaluationKbManifest(
            corpus_id=ee.CORPUS_ID,
            collection_name=ee.COLLECTION_NAME,
            document_count=document_count,
            chunk_count=len(chunks),
            embedding_model_name=ee.EMBEDDING_MODEL_NAME,
            embedding_dimension=ee.DENSE_DIMENSION,  # dimension 仍 1024
            chunk_size=ee.CHUNK_SIZE,
            chunk_overlap=ee.CHUNK_OVERLAP,
            chunks=tuple(ee.ordered_chunk_identities(chunks)),
        )
        return _FakeManager(collection, query_prompt_name=None), manifest

    monkeypatch.setattr(ee, "build_evaluation_kb", _fake_build_evaluation_kb)
    wrong_model_dir = tmp_path / "wrong-model"
    wrong_model_dir.mkdir()
    cache_root = tmp_path / "dense-cache"
    with pytest.raises(RuntimeError, match="EMBEDDING_MODEL_REF_MISMATCH"):
        ee.build_or_reuse_evaluation_dense_cache(
            corpus_dir=ee.default_corpus_dir(),
            cache_root=cache_root,
            embedding_model_path=wrong_model_dir,
        )
    assert not cache_root.exists() or not any(cache_root.iterdir())


def test_dense_dimension_mismatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunks, document_count = ee.prepare_evaluation_chunks(ee.default_corpus_dir())
    collection = _FakeCollection({}, count=len(chunks))

    def _fake_build_evaluation_kb(*, persist_dir, embedding_model_path, corpus_dir, embedding_batch_size, query_prompt_name):
        manifest = ee.EvaluationKbManifest(
            corpus_id=ee.CORPUS_ID,
            collection_name=ee.COLLECTION_NAME,
            document_count=document_count,
            chunk_count=len(chunks),
            embedding_model_name=ee.EMBEDDING_MODEL_NAME,
            embedding_dimension=768,  # wrong vs frozen 1024
            chunk_size=ee.CHUNK_SIZE,
            chunk_overlap=ee.CHUNK_OVERLAP,
            chunks=tuple(ee.ordered_chunk_identities(chunks)),
        )
        return _FakeManager(collection, query_prompt_name=None), manifest

    monkeypatch.setattr(ee, "build_evaluation_kb", _fake_build_evaluation_kb)
    model_dir = tmp_path / MODEL_DIR_NAME
    model_dir.mkdir()
    with pytest.raises(RuntimeError, match="EMBEDDING_DIMENSION_MISMATCH"):
        ee.build_or_reuse_evaluation_dense_cache(
            corpus_dir=ee.default_corpus_dir(),
            cache_root=tmp_path / "dense-cache",
            embedding_model_path=model_dir,
        )


# ---------------------------------------------------------------------------
# Final P1：caller query-adapter model/prompt 必须在 VectorDBManager 创建前强制
# （artifact 完全合法，唯一错误是 caller query-adapter config）
# ---------------------------------------------------------------------------


def test_dense_load_rejects_wrong_query_model_ref(tmp_path: Path) -> None:
    """READY cache 完全正确；caller embedding_model_path basename = WrongEmbeddingModel → reject。"""
    cache_dir, identity = _write_ready_cache(tmp_path, chunk_count=3)
    wrong_model = tmp_path / "WrongEmbeddingModel"
    wrong_model.mkdir()
    with pytest.raises(RuntimeError, match="EMBEDDING_MODEL_REF_MISMATCH"):
        ee.load_evaluation_dense_cache(
            cache_dir=cache_dir,
            expected_identity=identity,
            expected_document_count=1,
            expected_chunk_count=3,
            embedding_model_path=wrong_model,
            query_prompt_name="",
        )


def test_dense_load_rejects_nonempty_query_prompt(tmp_path: Path) -> None:
    """READY cache 完全正确；caller query_prompt_name = 'query' → reject。"""
    cache_dir, identity = _write_ready_cache(tmp_path, chunk_count=3)
    model_dir = tmp_path / MODEL_DIR_NAME
    model_dir.mkdir()
    with pytest.raises(RuntimeError, match="DENSE_QUERY_PROMPT_MUST_BE_EMPTY"):
        ee.load_evaluation_dense_cache(
            cache_dir=cache_dir,
            expected_identity=identity,
            expected_document_count=1,
            expected_chunk_count=3,
            embedding_model_path=model_dir,
            query_prompt_name="query",
        )


def test_dense_load_rejects_wrong_query_adapter_model_and_prompt(tmp_path: Path) -> None:
    """READY cache 完全正确；wrong model + non-empty prompt → reject（Codex exploit）。"""
    cache_dir, identity = _write_ready_cache(tmp_path, chunk_count=3)
    wrong_model = tmp_path / "WrongEmbeddingModel"
    wrong_model.mkdir()
    with pytest.raises(RuntimeError, match="EMBEDDING_MODEL_REF_MISMATCH"):
        ee.load_evaluation_dense_cache(
            cache_dir=cache_dir,
            expected_identity=identity,
            expected_document_count=1,
            expected_chunk_count=3,
            embedding_model_path=wrong_model,
            query_prompt_name="retrieval",
        )


class _FakeCollection:
    def __init__(self, metadata, count):
        self.metadata = dict(metadata)
        self._count = count

    def count(self):
        return self._count

    def modify(self, metadata=None):
        if metadata is not None:
            self.metadata = dict(metadata)


class _FakeStore:
    def __init__(self, collection):
        self._collection = collection


class _FakeManager:
    def __init__(self, collection, *, query_prompt_name=None):
        self.vector_store = _FakeStore(collection)
        self._query_prompt_name = query_prompt_name
        self.ingest_calls = 0

    def ingest_chunks(self, chunks):
        self.ingest_calls += 1
        return len(chunks)
