from types import SimpleNamespace

from core.knowledge_base import vector_db_manager as vector_module


class FakeEmbeddings:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class FakeCollection:
    def __init__(self) -> None:
        self._client = SimpleNamespace(max_batch_size=100)
        self._count = 0

    def count(self) -> int:
        return self._count


class FakeChroma:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self._collection = FakeCollection()
        self.added = []

    def delete(self, **kwargs) -> None:
        return None

    def add_documents(self, documents, ids) -> None:
        self.added.extend(zip(ids, documents))


def test_vector_manager_forwards_collection_and_embedding_config(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(vector_module, "HuggingFaceEmbeddings", FakeEmbeddings)
    monkeypatch.setattr(vector_module, "Chroma", FakeChroma)

    manager = vector_module.VectorDBManager(
        str(tmp_path),
        "mock-model",
        collection_name="local_agent_mock_v1",
        embedding_batch_size=4,
        query_prompt_name="query",
    )

    assert manager.vector_store.kwargs["collection_name"] == "local_agent_mock_v1"
    assert manager.embeddings.kwargs["encode_kwargs"]["batch_size"] == 4
    assert manager.embeddings.kwargs["query_encode_kwargs"]["prompt_name"] == "query"


def test_vector_manager_sanitizes_metadata_and_counts_writes(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(vector_module, "HuggingFaceEmbeddings", FakeEmbeddings)
    monkeypatch.setattr(vector_module, "Chroma", FakeChroma)
    manager = vector_module.VectorDBManager(str(tmp_path), ingest_batch_size=2)

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
