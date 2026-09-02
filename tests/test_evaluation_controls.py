"""WP3 evaluation-only generation pin 与 rewrite fixture 合同测试。"""

import json

import pytest

from core.runtime.evaluation_controls import (
    EvaluationGenerationPin,
    EvaluationRewriteEntry,
    EvaluationRewriteFixture,
    canonical_sha256,
    load_generation_pin,
    load_rewrite_fixture,
)


def _pin() -> EvaluationGenerationPin:
    return EvaluationGenerationPin(
        generation_id="generation-1", dense_collection_name="dense-generation-1",
        provenance_sha256="p" * 64, corpus_id="corpus-1", source_manifest_sha256="s" * 64,
        chunk_policy_sha256="c" * 64, chunk_manifest_sha256="m" * 64,
        document_count=2, chunk_count=4, embedding_identity="e" * 64,
    )


def test_generation_pin_digest_round_trip(tmp_path) -> None:
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(_pin().to_dict()), encoding="utf-8")
    loaded = load_generation_pin(path)
    assert loaded.generation_pin_sha256 == _pin().generation_pin_sha256


def test_generation_pin_rejects_digest_drift(tmp_path) -> None:
    payload = _pin().to_dict()
    payload["generation_id"] = "other"
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="generation_pin_sha256 mismatch"):
        load_generation_pin(path)


def test_rewrite_fixture_round_trip_and_query_mismatch(tmp_path) -> None:
    fixture = EvaluationRewriteFixture(
        "v1",
        tuple(
            EvaluationRewriteEntry(
                case_id=f"case-{index}", query_digest=canonical_sha256(f"query-{index}"),
                rewritten_query=f"rewritten-{index}", rewritten_query_digest=canonical_sha256(f"rewritten-{index}"),
            )
            for index in range(24)
        ),
    )
    payload = {"schema_version": "localagent-evaluation-rewrite-fixture.v1", "fixture_version": "v1", "rewrite_fixture_id": fixture.fixture_id, "entries": [
        {"case_id": item.case_id, "query_digest": item.query_digest, "rewritten_query": item.rewritten_query, "rewritten_query_digest": item.rewritten_query_digest}
        for item in fixture.entries
    ]}
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_rewrite_fixture(path)
    assert loaded.resolve(case_id=None, query="query-3").rewritten_query == "rewritten-3"
    with pytest.raises(ValueError, match="case/query mismatch"):
        loaded.resolve(case_id=None, query="missing")
