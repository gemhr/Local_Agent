from types import SimpleNamespace

import pytest

from core.knowledge_base import vector_db_manager as vector_module


class FakeEmbeddings:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def embed_query(self, query):
        return [0.1, 0.2, 0.3]


class FakeCollection:
    def __init__(self) -> None:
        self._client = SimpleNamespace(max_batch_size=100)
        self._count = 0
        self.get_result = {"ids": [], "documents": [], "metadatas": []}
        self.get_calls = []

    def count(self) -> int:
        return self._count

    def get(self, **kwargs) -> dict:
        self.get_calls.append(kwargs)
        return self.get_result


class FakeChroma:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self._collection = FakeCollection()
        self.added = []

    def delete(self, **kwargs) -> None:
        return None

    def add_documents(self, documents, ids) -> None:
        self.added.extend(zip(ids, documents))

    def similarity_search_with_score(self, **kwargs) -> list:
        return []


class FakeCollectionV2(FakeCollection):
    """支持 modify(metadata=...) 的 collection（marker 发布/校验）。"""

    def __init__(self) -> None:
        super().__init__()
        self.metadata: dict = {}

    def modify(self, *, metadata) -> None:
        self.metadata = dict(metadata)


def test_vector_manager_forwards_collection_and_embedding_config(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(vector_module, "HuggingFaceEmbeddings", FakeEmbeddings)
    monkeypatch.setattr(vector_module, "Chroma", FakeChroma)

    manager = vector_module.VectorDBManager(
        str(tmp_path),
        str(tmp_path),
        collection_name="local_agent_mock_v1",
        embedding_batch_size=4,
        query_prompt_name="query",
    )

    assert manager.vector_store.kwargs["collection_name"] == "local_agent_mock_v1"
    assert manager.embeddings.kwargs["model_kwargs"]["local_files_only"] is True
    assert manager.embeddings.kwargs["encode_kwargs"]["batch_size"] == 4
    assert manager.embeddings.kwargs["query_encode_kwargs"]["prompt_name"] == "query"


def test_vector_manager_sanitizes_metadata_and_counts_writes(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(vector_module, "HuggingFaceEmbeddings", FakeEmbeddings)
    monkeypatch.setattr(vector_module, "Chroma", FakeChroma)
    manager = vector_module.VectorDBManager(
        str(tmp_path), str(tmp_path), ingest_batch_size=2
    )

    written = manager.ingest_chunks(
        [
            {
                "page_content": "hello",
                "metadata": {"chunk_id": "one", "drop_me": None, "items": [1, 2]},
            }
        ]
    )

    assert written == 1
    metadata = manager.vector_store.added[0][1].metadata
    assert "drop_me" not in metadata
    assert metadata["items"] == "[1, 2]"


def test_similarity_distances_are_normalized_to_higher_is_better(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(vector_module, "HuggingFaceEmbeddings", FakeEmbeddings)
    monkeypatch.setattr(vector_module, "Chroma", FakeChroma)
    manager = vector_module.VectorDBManager(str(tmp_path), str(tmp_path))
    closer = vector_module.Document(page_content="closer", metadata={})
    farther = vector_module.Document(page_content="farther", metadata={})
    manager.vector_store.similarity_search_with_score = lambda **kwargs: [
        (closer, 0.5),
        (farther, 2.0),
    ]

    results = manager.similarity_search_with_scores("query", top_k=2)

    assert results[0][1] == 2 / 3
    assert results[1][1] == 1 / 3
    assert results[0][1] > results[1][1]


def test_keyword_search_reads_matching_chroma_documents(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(vector_module, "HuggingFaceEmbeddings", FakeEmbeddings)
    monkeypatch.setattr(vector_module, "Chroma", FakeChroma)
    manager = vector_module.VectorDBManager(str(tmp_path), str(tmp_path))
    manager.vector_store._collection.get_result = {
        "ids": ["cdt-1"],
        "documents": ["CDT 字段映射正文"],
        "metadatas": [{"source": "cdt_field_mapping.md"}],
    }

    results = manager.keyword_search(["cdt"], k=3)

    assert len(results) == 1
    assert results[0].page_content == "CDT 字段映射正文"
    assert results[0].metadata["source"] == "cdt_field_mapping.md"
    assert manager.vector_store._collection.get_calls[0]["where_document"] == {
        "$contains": "cdt"
    }


def test_missing_local_embedding_model_path_fails_before_adapter_load(
    monkeypatch, tmp_path
) -> None:
    adapter_called = False

    def unexpected_adapter(**kwargs):
        nonlocal adapter_called
        adapter_called = True
        return FakeEmbeddings(**kwargs)

    monkeypatch.setattr(vector_module, "HuggingFaceEmbeddings", unexpected_adapter)

    with pytest.raises(FileNotFoundError, match="EMBEDDING_MODEL_ASSET_INVALID"):
        vector_module.VectorDBManager(
            str(tmp_path / "chroma"), str(tmp_path / "missing-model")
        )

    assert adapter_called is False


# ---------------------------------------------------------------------------
# Stage5-Phase6-WP1 Chroma marker v1/v2 preflight
# ---------------------------------------------------------------------------


def _v2_manager(monkeypatch, tmp_path):
    monkeypatch.setattr(vector_module, "HuggingFaceEmbeddings", FakeEmbeddings)
    monkeypatch.setattr(vector_module, "Chroma", FakeChroma)
    return vector_module.VectorDBManager(
        str(tmp_path), str(tmp_path), collection_name="la_kb_g_gen"
    )


def _expected_v2(manager, generation_id="12345678-1234-4234-8234-123456789abc"):
    return manager.expected_v2_collection_marker(
        generation_id=generation_id,
        provenance_contract_version="retrieval-index-provenance.v1",
        provenance_sha256="b" * 64,
        corpus_id="kb",
        source_manifest_sha256="c" * 64,
        chunk_policy_sha256="d" * 64,
        chunk_manifest_sha256="e" * 64,
        document_count=1,
        chunk_count=1,
        embedding_asset_tree_sha256="f" * 64,
    )


def test_baseline_preflight_accepts_v1_marker(monkeypatch, tmp_path) -> None:
    manager = _v2_manager(monkeypatch, tmp_path)
    manager.vector_store._collection = FakeCollectionV2()
    manager.vector_store._collection._count = 3
    manager.publish_collection_marker()  # v1 marker
    result = manager.collection_preflight()  # baseline
    assert result.status.value == "CURRENT"


def test_v1_collection_is_hybrid_incompatible(monkeypatch, tmp_path) -> None:
    manager = _v2_manager(monkeypatch, tmp_path)
    manager.vector_store._collection = FakeCollectionV2()
    manager.vector_store._collection._count = 3
    # 真实 legacy v1 marker：contract version=1，无任何 v2 provenance 字段。
    v1_marker = manager.expected_collection_marker()
    v1_marker["localagent_collection_contract_version"] = 1
    manager.publish_v2_collection_marker(v1_marker)
    result = manager.collection_preflight(
        hybrid_required=True,
        expected_v2_marker=_expected_v2(manager),
    )
    assert result.status.value == "REBUILD_REQUIRED"
    assert result.detected_version == "v1-incompatible"


def test_valid_v2_marker_preflight_current(monkeypatch, tmp_path) -> None:
    manager = _v2_manager(monkeypatch, tmp_path)
    manager.vector_store._collection = FakeCollectionV2()
    manager.vector_store._collection._count = 3
    expected = _expected_v2(manager)
    manager.publish_v2_collection_marker(expected)
    result = manager.collection_preflight(hybrid_required=True, expected_v2_marker=expected)
    assert result.status.value == "CURRENT"


def test_v2_provenance_mismatch_rebuild_required(monkeypatch, tmp_path) -> None:
    manager = _v2_manager(monkeypatch, tmp_path)
    manager.vector_store._collection = FakeCollectionV2()
    manager.vector_store._collection._count = 3
    expected = _expected_v2(manager)
    manager.publish_v2_collection_marker(expected)
    tampered = dict(expected)
    tampered["provenance_sha256"] = "9" * 64
    result = manager.collection_preflight(hybrid_required=True, expected_v2_marker=tampered)
    assert result.status.value == "REBUILD_REQUIRED"
    assert result.detected_version == "v2-mismatch"


def test_v2_generation_mismatch_rebuild_required(monkeypatch, tmp_path) -> None:
    manager = _v2_manager(monkeypatch, tmp_path)
    manager.vector_store._collection = FakeCollectionV2()
    manager.vector_store._collection._count = 3
    expected = _expected_v2(manager)
    manager.publish_v2_collection_marker(expected)
    other = _expected_v2(manager, generation_id="87654321-4321-4321-8321-123456789abc")
    result = manager.collection_preflight(hybrid_required=True, expected_v2_marker=other)
    assert result.status.value == "REBUILD_REQUIRED"


def test_v2_asset_digest_mismatch_rebuild_required(monkeypatch, tmp_path) -> None:
    manager = _v2_manager(monkeypatch, tmp_path)
    manager.vector_store._collection = FakeCollectionV2()
    manager.vector_store._collection._count = 3
    expected = _expected_v2(manager)
    manager.publish_v2_collection_marker(expected)
    tampered = dict(expected)
    tampered["embedding_asset_tree_sha256"] = "0" * 64
    result = manager.collection_preflight(hybrid_required=True, expected_v2_marker=tampered)
    assert result.status.value == "REBUILD_REQUIRED"


def test_no_inferred_migration_from_chunk_rows(monkeypatch, tmp_path) -> None:
    """非空 collection 只有 v1 基础字段（无 v2 provenance）不能推断迁移。"""
    manager = _v2_manager(monkeypatch, tmp_path)
    manager.vector_store._collection = FakeCollectionV2()
    manager.vector_store._collection._count = 3
    manager.publish_collection_marker()  # v1 marker only
    expected = _expected_v2(manager)
    result = manager.collection_preflight(hybrid_required=True, expected_v2_marker=expected)
    assert result.status.value == "REBUILD_REQUIRED"
