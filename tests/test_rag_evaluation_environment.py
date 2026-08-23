from __future__ import annotations

from core.knowledge_base.evaluation_environment import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    COLLECTION_NAME,
    CORPUS_ID,
    default_corpus_dir,
    prepare_evaluation_chunks,
)


def test_evaluation_corpus_has_frozen_scale_and_deterministic_identities() -> None:
    first, first_documents = prepare_evaluation_chunks(default_corpus_dir())
    second, second_documents = prepare_evaluation_chunks(default_corpus_dir())

    assert CORPUS_ID == "rag-evaluation-corpus.v1"
    assert COLLECTION_NAME == "rag_evaluation_kb_v1"
    assert CHUNK_SIZE == 1400
    assert CHUNK_OVERLAP == 180
    assert first_documents == second_documents == 15
    assert len(first) == len(second) == 60
    first_ids = [
        (item["metadata"]["doc_id"], item["metadata"]["chunk_id"]) for item in first
    ]
    second_ids = [
        (item["metadata"]["doc_id"], item["metadata"]["chunk_id"]) for item in second
    ]
    assert first_ids == second_ids
    assert len(first_ids) == len(set(first_ids))
    assert all(item["metadata"]["ingest_batch_id"] == CORPUS_ID for item in first)
