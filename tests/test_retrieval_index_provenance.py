"""Stage5-Phase6-WP1 production retrieval provenance contract focused tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.knowledge_base.document_loader import (
    CHUNK_CONTENT_FORMAT_REF,
    SCHEMA_VERSION,
    SPLITTER_REF,
    chunk_policy_from,
)
from core.knowledge_base.retrieval_index_provenance import (
    ACTIVE_DESCRIPTOR_SCHEMA_VERSION,
    PROVENANCE_CONTRACT_VERSION,
    ActiveGenerationDescriptor,
    RetrievalIndexProvenance,
    build_chunk_policy_descriptor,
    canonical_sha256,
    chunk_policy_digest,
    collection_key,
    embedding_asset_tree_digest,
    embedding_asset_tree_manifest,
    is_canonical_generation_id,
    new_generation_id,
    ordered_chunk_manifest,
    ordered_chunk_manifest_digest,
    physical_dense_collection_name,
    publish_active_descriptor,
    read_active_descriptor,
    retrieval_root,
    sha256_hex,
    source_manifest_digest,
    source_manifest_items,
    validate_retrieval_index_manifest,
    build_retrieval_index_manifest,
)
from core.settings import (
    SETTINGS_VALIDATION_ERROR,
    RetrievalStrategy,
    Settings,
    SettingsValidationError,
)


# ---------------------------------------------------------------------------
# source manifest
# ---------------------------------------------------------------------------


def _write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_source_manifest_canonical_ordering(tmp_path) -> None:
    _write_files(tmp_path, {"b.txt": "beta", "a.txt": "alpha", "c/d.txt": "charlie"})
    items = source_manifest_items(tmp_path)
    paths = [item["source"] for item in items]
    assert paths == sorted(paths)
    assert paths == ["a.txt", "b.txt", "c/d.txt"]


def test_source_manifest_content_change_changes_digest(tmp_path) -> None:
    _write_files(tmp_path, {"a.txt": "v1"})
    before = source_manifest_digest(tmp_path)
    _write_files(tmp_path, {"a.txt": "v2"})
    after = source_manifest_digest(tmp_path)
    assert before != after


def test_source_manifest_absolute_root_change_does_not_alter_identity(tmp_path) -> None:
    _write_files(tmp_path, {"a.txt": "same"})
    first = source_manifest_digest(tmp_path)
    other = tmp_path / "other-root"
    other.mkdir()
    _write_files(other, {"a.txt": "same"})
    second = source_manifest_digest(other)
    assert first == second


def test_source_manifest_excludes_artifacts_by_supported_rules(tmp_path) -> None:
    _write_files(
        tmp_path,
        {
            "a.md": "# doc",
            "chroma_db/ignored.sqlite3": "x",
            "00_metadata/skip.md": "y",
        },
    )
    items = source_manifest_items(tmp_path)
    sources = [item["source"] for item in items]
    assert sources == ["a.md"]


def test_source_manifest_sha256_is_content_digest(tmp_path) -> None:
    _write_files(tmp_path, {"a.txt": "hello"})
    items = source_manifest_items(tmp_path)
    assert items[0]["content_sha256"] == sha256_hex(b"hello")


# ---------------------------------------------------------------------------
# chunk policy
# ---------------------------------------------------------------------------


def test_chunk_policy_digest_changes_on_splitter_ref(tmp_path) -> None:
    base = chunk_policy_from(chunk_size=1400, chunk_overlap=180)
    changed = chunk_policy_from(chunk_size=1400, chunk_overlap=180, splitter_ref="other.v3")
    assert chunk_policy_digest(base) != chunk_policy_digest(changed)


def test_chunk_policy_digest_changes_on_chunk_size(tmp_path) -> None:
    assert chunk_policy_digest(chunk_policy_from(chunk_size=1000, chunk_overlap=180)) != chunk_policy_digest(
        chunk_policy_from(chunk_size=1400, chunk_overlap=180)
    )


def test_chunk_policy_digest_changes_on_overlap(tmp_path) -> None:
    assert chunk_policy_digest(chunk_policy_from(chunk_size=1400, chunk_overlap=100)) != chunk_policy_digest(
        chunk_policy_from(chunk_size=1400, chunk_overlap=180)
    )


def test_chunk_policy_digest_changes_on_content_format(tmp_path) -> None:
    changed = chunk_policy_from(
        chunk_size=1400, chunk_overlap=180, chunk_content_format_ref="other-format.v2"
    )
    assert chunk_policy_digest(changed) != chunk_policy_digest(
        chunk_policy_from(chunk_size=1400, chunk_overlap=180)
    )


def test_chunk_policy_refs_are_frozen_exports() -> None:
    assert SPLITTER_REF == "structure-aware-splitter.v2"
    assert SCHEMA_VERSION == "kb_chunk_schema_v2"
    assert CHUNK_CONTENT_FORMAT_REF


def test_chunk_policy_rejects_invalid_ranges() -> None:
    with pytest.raises(ValueError):
        build_chunk_policy_descriptor(
            chunk_schema_version=SCHEMA_VERSION,
            splitter_ref=SPLITTER_REF,
            chunk_size=0,
            chunk_overlap=0,
            chunk_content_format_ref=CHUNK_CONTENT_FORMAT_REF,
        )
    with pytest.raises(ValueError):
        build_chunk_policy_descriptor(
            chunk_schema_version=SCHEMA_VERSION,
            splitter_ref=SPLITTER_REF,
            chunk_size=100,
            chunk_overlap=100,
            chunk_content_format_ref=CHUNK_CONTENT_FORMAT_REF,
        )


# ---------------------------------------------------------------------------
# ordered chunk manifest
# ---------------------------------------------------------------------------


def _chunk(document_id: str, chunk_id: str, source: str, content_hash: str) -> dict:
    return {
        "page_content": "text",
        "metadata": {
            "doc_id": document_id,
            "chunk_id": chunk_id,
            "source": source,
            "section_path": "S",
            "content_hash": content_hash,
        },
    }


def test_ordered_chunk_manifest_order_matters() -> None:
    chunks_a = [_chunk("d1", "c1", "a.md", "h1"), _chunk("d1", "c2", "a.md", "h2")]
    chunks_b = [_chunk("d1", "c2", "a.md", "h2"), _chunk("d1", "c1", "a.md", "h1")]
    manifest_a = ordered_chunk_manifest(chunks_a)
    manifest_b = ordered_chunk_manifest(chunks_b)
    assert manifest_a != manifest_b
    assert ordered_chunk_manifest_digest(chunks_a) != ordered_chunk_manifest_digest(chunks_b)


def test_ordered_chunk_manifest_chunk_drift_changes_digest() -> None:
    chunks_a = [_chunk("d1", "c1", "a.md", "h1")]
    chunks_b = [_chunk("d1", "c1", "a.md", "h2")]
    assert ordered_chunk_manifest_digest(chunks_a) != ordered_chunk_manifest_digest(chunks_b)


def test_ordered_chunk_manifest_rejects_missing_identity() -> None:
    with pytest.raises(ValueError):
        ordered_chunk_manifest([{"page_content": "x", "metadata": {"doc_id": "d1"}}])


# ---------------------------------------------------------------------------
# embedding asset tree
# ---------------------------------------------------------------------------


def test_embedding_asset_tree_includes_regular_hidden_and_nested(tmp_path) -> None:
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".hidden").write_text("h", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.bin").write_text("n", encoding="utf-8")
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / ".cache").write_text("c", encoding="utf-8")
    manifest = embedding_asset_tree_manifest(tmp_path)
    paths = [item["path"] for item in manifest]
    assert paths == sorted(paths)
    assert ".hidden" in paths
    assert "config.json" in paths
    assert "sub/nested.bin" in paths
    assert "cache/.cache" in paths


def test_embedding_asset_tree_content_change_changes_digest(tmp_path) -> None:
    (tmp_path / "model.bin").write_bytes(b"v1")
    before = embedding_asset_tree_digest(tmp_path)
    (tmp_path / "model.bin").write_bytes(b"v2")
    after = embedding_asset_tree_digest(tmp_path)
    assert before != after


def test_embedding_asset_tree_mtime_only_does_not_change_digest(tmp_path) -> None:
    path = tmp_path / "model.bin"
    path.write_bytes(b"fixed")
    before = embedding_asset_tree_digest(tmp_path)
    os.utime(path, (1_600_000_000, 1_600_000_000))
    after = embedding_asset_tree_digest(tmp_path)
    assert before == after


@pytest.mark.skipif(os.name == "nt", reason="symlink creation may require privileges on Windows")
def test_embedding_asset_tree_rejects_symlink(tmp_path) -> None:
    (tmp_path / "real.bin").write_bytes(b"x")
    (tmp_path / "link.bin").symlink_to(tmp_path / "real.bin")
    with pytest.raises(ValueError, match="EMBEDDING_MODEL_ASSET_INVALID"):
        embedding_asset_tree_manifest(tmp_path)


def test_embedding_asset_tree_rejects_missing_root(tmp_path) -> None:
    with pytest.raises(ValueError, match="EMBEDDING_MODEL_ASSET_INVALID"):
        embedding_asset_tree_digest(tmp_path / "missing")


# ---------------------------------------------------------------------------
# generation identity helpers
# ---------------------------------------------------------------------------


def test_collection_key_is_opaque_hex_prefix() -> None:
    key = collection_key("huawei_wiki_collection")
    assert len(key) == 16
    assert key == collection_key("huawei_wiki_collection")
    assert key != collection_key("other_collection")


def test_physical_dense_collection_name_convention() -> None:
    generation_id = "12345678-1234-4234-8234-123456789abc"
    name = physical_dense_collection_name("kb", generation_id)
    assert name == f"la_{collection_key('kb')}_g_{generation_id.replace('-', '')}"
    assert name.startswith("la_")


def test_generation_id_validation() -> None:
    assert is_canonical_generation_id("12345678-1234-4234-8234-123456789abc")
    assert not is_canonical_generation_id("12345678-1234-4234-8234-123456789AB")
    assert not is_canonical_generation_id("not-a-uuid")


def test_retrieval_root_layout(tmp_path) -> None:
    root = retrieval_root(tmp_path, "kb")
    assert root == tmp_path / "localagent_retrieval" / collection_key("kb")


# ---------------------------------------------------------------------------
# RetrievalIndexProvenance + manifest validation
# ---------------------------------------------------------------------------


def _provenance() -> RetrievalIndexProvenance:
    chunk_policy = chunk_policy_from(chunk_size=1400, chunk_overlap=180)
    chunks = [_chunk("d1", "c1", "a.md", "h1")]
    return RetrievalIndexProvenance(
        generation_id=new_generation_id(),
        corpus_id="kb",
        source_manifest_sha256="a" * 64,
        chunk_policy=chunk_policy,
        chunk_policy_sha256=chunk_policy_digest(chunk_policy),
        chunk_manifest_sha256=ordered_chunk_manifest_digest(chunks),
        document_count=1,
        chunk_count=1,
    )


def test_provenance_roundtrip_and_digest() -> None:
    provenance = _provenance()
    payload = provenance.to_dict()
    restored = RetrievalIndexProvenance.from_dict(payload)
    assert restored == provenance
    assert provenance.provenance_sha256() == canonical_sha256(payload)


def test_manifest_validation_detects_provenance_tamper() -> None:
    provenance = _provenance()
    chunks = [_chunk("d1", "c1", "a.md", "h1")]
    manifest = build_retrieval_index_manifest(provenance, chunks)
    validated = validate_retrieval_index_manifest(manifest)
    assert validated.generation_id == provenance.generation_id
    tampered = dict(manifest)
    tampered["provenance_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="provenance_sha256 mismatch"):
        validate_retrieval_index_manifest(tampered)


# ---------------------------------------------------------------------------
# active descriptor
# ---------------------------------------------------------------------------


def _descriptor(tmp_path: Path, generation_id: str) -> ActiveGenerationDescriptor:
    root = retrieval_root(tmp_path, "kb")
    generation_rel = f"generations/{generation_id}"
    return ActiveGenerationDescriptor(
        generation_id=generation_id,
        provenance_contract_version=PROVENANCE_CONTRACT_VERSION,
        provenance_sha256="b" * 64,
        corpus_id="kb",
        dense_persist_dir_ref="LOCAL_AGENT_CHROMA_DIR",
        dense_collection_name=physical_dense_collection_name("kb", generation_id),
        bm25_artifact_path=f"{generation_rel}/bm25_index.json",
        provenance_manifest_path=f"{generation_rel}/retrieval_index_manifest.json",
        artifact_metadata_path=f"{generation_rel}/artifact_metadata.json",
    )


def test_active_descriptor_valid_roundtrip(tmp_path) -> None:
    root = retrieval_root(tmp_path, "kb")
    generation_id = new_generation_id()
    descriptor = _descriptor(tmp_path, generation_id)
    publish_active_descriptor(descriptor, root)
    restored = read_active_descriptor(root)
    assert restored is not None
    assert restored.generation_id == generation_id
    assert restored.dense_persist_dir_ref == "LOCAL_AGENT_CHROMA_DIR"
    payload = restored.to_dict()
    assert payload["schema_version"] == ACTIVE_DESCRIPTOR_SCHEMA_VERSION


def test_active_descriptor_rejects_absolute_path(tmp_path) -> None:
    root = retrieval_root(tmp_path, "kb")
    descriptor = ActiveGenerationDescriptor(
        generation_id=new_generation_id(),
        provenance_contract_version=PROVENANCE_CONTRACT_VERSION,
        provenance_sha256="b" * 64,
        corpus_id="kb",
        dense_persist_dir_ref="LOCAL_AGENT_CHROMA_DIR",
        dense_collection_name="la_x_g_y",
        bm25_artifact_path="/abs/bm25_index.json",
        provenance_manifest_path="generations/g/manifest.json",
        artifact_metadata_path="generations/g/artifact_metadata.json",
    )
    with pytest.raises(ValueError, match="relative"):
        descriptor.resolve_locators(root)


def test_active_descriptor_rejects_parent_traversal(tmp_path) -> None:
    root = retrieval_root(tmp_path, "kb")
    descriptor = ActiveGenerationDescriptor(
        generation_id=new_generation_id(),
        provenance_contract_version=PROVENANCE_CONTRACT_VERSION,
        provenance_sha256="b" * 64,
        corpus_id="kb",
        dense_persist_dir_ref="LOCAL_AGENT_CHROMA_DIR",
        dense_collection_name="la_x_g_y",
        bm25_artifact_path="../escape/bm25_index.json",
        provenance_manifest_path="generations/g/manifest.json",
        artifact_metadata_path="generations/g/artifact_metadata.json",
    )
    with pytest.raises(ValueError, match="invalid path segments"):
        descriptor.resolve_locators(root)


def test_active_descriptor_rejects_drive_escape(tmp_path) -> None:
    root = retrieval_root(tmp_path, "kb")
    descriptor = ActiveGenerationDescriptor(
        generation_id=new_generation_id(),
        provenance_contract_version=PROVENANCE_CONTRACT_VERSION,
        provenance_sha256="b" * 64,
        corpus_id="kb",
        dense_persist_dir_ref="LOCAL_AGENT_CHROMA_DIR",
        dense_collection_name="la_x_g_y",
        bm25_artifact_path="C:/escape/bm25_index.json",
        provenance_manifest_path="generations/g/manifest.json",
        artifact_metadata_path="generations/g/artifact_metadata.json",
    )
    with pytest.raises(ValueError, match="drive-qualified"):
        descriptor.resolve_locators(root)


def test_active_descriptor_atomic_publish_and_failure_preserves_old(tmp_path) -> None:
    root = retrieval_root(tmp_path, "kb")
    old_generation = new_generation_id()
    old = _descriptor(tmp_path, old_generation)
    publish_active_descriptor(old, root)
    old_text = (root / "active.json").read_text(encoding="utf-8")

    # 失败：invalid descriptor（非法 locator）不能替换旧 active。
    bad = ActiveGenerationDescriptor(
        generation_id=new_generation_id(),
        provenance_contract_version=PROVENANCE_CONTRACT_VERSION,
        provenance_sha256="b" * 64,
        corpus_id="kb",
        dense_persist_dir_ref="LOCAL_AGENT_CHROMA_DIR",
        dense_collection_name="la_x_g_y",
        bm25_artifact_path="generations/g/bm25_index.json",
        provenance_manifest_path="../../evil.json",
        artifact_metadata_path="generations/g/artifact_metadata.json",
    )
    with pytest.raises(ValueError):
        publish_active_descriptor(bad, root)
    assert (root / "active.json").read_text(encoding="utf-8") == old_text

    # 成功：新 descriptor 原子替换。
    new_generation = new_generation_id()
    publish_active_descriptor(_descriptor(tmp_path, new_generation), root)
    restored = read_active_descriptor(root)
    assert restored is not None
    assert restored.generation_id == new_generation


def test_active_descriptor_contains_required_fields() -> None:
    descriptor = _descriptor(Path("."), new_generation_id())
    payload = descriptor.to_dict()
    assert payload["schema_version"] == ACTIVE_DESCRIPTOR_SCHEMA_VERSION
    for key in (
        "generation_id",
        "provenance_contract_version",
        "provenance_sha256",
        "corpus_id",
    ):
        assert key in payload
    assert payload["dense"]["persist_dir_ref"] == "LOCAL_AGENT_CHROMA_DIR"
    assert payload["dense"]["collection_name"]
    for key in ("bm25_artifact_path", "provenance_manifest_path", "artifact_metadata_path"):
        assert key in payload


def test_active_descriptor_rejects_wrong_persist_dir_ref() -> None:
    with pytest.raises(ValueError, match="LOCAL_AGENT_CHROMA_DIR"):
        ActiveGenerationDescriptor.from_dict(
            {
                "schema_version": ACTIVE_DESCRIPTOR_SCHEMA_VERSION,
                "generation_id": new_generation_id(),
                "provenance_contract_version": PROVENANCE_CONTRACT_VERSION,
                "provenance_sha256": "b" * 64,
                "corpus_id": "kb",
                "dense": {"persist_dir_ref": "OTHER_DIR", "collection_name": "la_x"},
                "bm25_artifact_path": "generations/g/bm25_index.json",
                "provenance_manifest_path": "generations/g/manifest.json",
                "artifact_metadata_path": "generations/g/metadata.json",
            }
        )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_ENV_BASE = {
    "LOCAL_AGENT_ENVIRONMENT_PROFILE": None,
    "LOCAL_AGENT_REMOTE_API_BASE_URL": "https://example.test/v1",
    "LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS": None,
}


def _load(monkeypatch, **env):
    for key, value in _ENV_BASE.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return Settings.load()


def test_settings_default_retrieval_strategy_baseline(monkeypatch) -> None:
    assert _load(monkeypatch).retrieval_strategy is RetrievalStrategy.BASELINE


def test_settings_valid_hybrid_rrf_strategy(monkeypatch) -> None:
    settings = _load(monkeypatch, LOCAL_AGENT_RETRIEVAL_STRATEGY="HYBRID_RRF")
    assert settings.retrieval_strategy is RetrievalStrategy.HYBRID_RRF


def test_settings_invalid_strategy_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(monkeypatch, LOCAL_AGENT_RETRIEVAL_STRATEGY="MEGA")
    assert captured.value.safe_error_code == SETTINGS_VALIDATION_ERROR
    assert captured.value.field == "LOCAL_AGENT_RETRIEVAL_STRATEGY"


def test_settings_default_chunk_size_overlap(monkeypatch) -> None:
    settings = _load(monkeypatch)
    assert settings.knowledge_chunk_size == 1400
    assert settings.knowledge_chunk_overlap == 180


def test_settings_invalid_chunk_size_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError):
        _load(monkeypatch, LOCAL_AGENT_KB_CHUNK_SIZE="0")


def test_settings_invalid_chunk_overlap_negative_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError):
        _load(monkeypatch, LOCAL_AGENT_KB_CHUNK_OVERLAP="-1")


def test_settings_overlap_not_below_size_fails_closed(monkeypatch) -> None:
    with pytest.raises(SettingsValidationError) as captured:
        _load(
            monkeypatch,
            LOCAL_AGENT_KB_CHUNK_SIZE="100",
            LOCAL_AGENT_KB_CHUNK_OVERLAP="100",
        )
    assert captured.value.reason_code == "overlap_not_below_size"
