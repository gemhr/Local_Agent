"""WP3 evaluation-only generation pin 与 rewrite fixture 控制。

默认配置不启用本模块的任何行为；所有输入均为 startup-scoped、只读、bounded JSON。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


def canonical_sha256(value: object) -> str:
    """使用 canonical JSON 计算 SHA-256。"""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest_query(value: str) -> str:
    return canonical_sha256(value)


@dataclass(frozen=True, slots=True)
class EvaluationGenerationPin:
    """两次 evaluation startup 共用的 generation identity receipt。"""

    generation_id: str
    dense_collection_name: str
    provenance_sha256: str
    corpus_id: str
    source_manifest_sha256: str
    chunk_policy_sha256: str
    chunk_manifest_sha256: str
    document_count: int
    chunk_count: int
    embedding_identity: str
    generation_pin_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.generation_id or not self.dense_collection_name or not self.provenance_sha256:
            raise ValueError("generation pin identity fields must be non-empty")
        if self.document_count < 0 or self.chunk_count < 1:
            raise ValueError("generation pin counts are invalid")
        object.__setattr__(self, "generation_pin_sha256", canonical_sha256(self.to_dict(include_digest=False)))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        payload = {
            "schema_version": "localagent-evaluation-generation-pin.v1",
            "generation_id": self.generation_id,
            "dense_collection_name": self.dense_collection_name,
            "provenance_sha256": self.provenance_sha256,
            "corpus_id": self.corpus_id,
            "source_manifest_sha256": self.source_manifest_sha256,
            "chunk_policy_sha256": self.chunk_policy_sha256,
            "chunk_manifest_sha256": self.chunk_manifest_sha256,
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
            "embedding_identity": self.embedding_identity,
        }
        if include_digest:
            payload["generation_pin_sha256"] = self.generation_pin_sha256
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluationGenerationPin":
        if payload.get("schema_version") != "localagent-evaluation-generation-pin.v1":
            raise ValueError("evaluation generation pin schema mismatch")
        pin = cls(
            generation_id=str(payload["generation_id"]),
            dense_collection_name=str(payload["dense_collection_name"]),
            provenance_sha256=str(payload["provenance_sha256"]),
            corpus_id=str(payload["corpus_id"]),
            source_manifest_sha256=str(payload["source_manifest_sha256"]),
            chunk_policy_sha256=str(payload["chunk_policy_sha256"]),
            chunk_manifest_sha256=str(payload["chunk_manifest_sha256"]),
            document_count=int(payload["document_count"]),
            chunk_count=int(payload["chunk_count"]),
            embedding_identity=str(payload["embedding_identity"]),
        )
        if payload.get("generation_pin_sha256") != pin.generation_pin_sha256:
            raise ValueError("generation_pin_sha256 mismatch")
        return pin

    @classmethod
    def from_validated_generation(cls, generation) -> "EvaluationGenerationPin":
        provenance = generation.provenance
        marker = generation.expected_v2_marker
        return cls(
            generation_id=generation.generation_id,
            dense_collection_name=generation.dense_collection_name,
            provenance_sha256=generation.provenance_sha256,
            corpus_id=generation.corpus_id,
            source_manifest_sha256=provenance.source_manifest_sha256,
            chunk_policy_sha256=provenance.chunk_policy_sha256,
            chunk_manifest_sha256=provenance.chunk_manifest_sha256,
            document_count=provenance.document_count,
            chunk_count=provenance.chunk_count,
            embedding_identity=str(marker.get("embedding_asset_tree_sha256", "")),
        )


def load_generation_pin(path: str | Path) -> EvaluationGenerationPin:
    """从 startup 配置加载 immutable generation pin。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return EvaluationGenerationPin.from_dict(payload)


@dataclass(frozen=True, slots=True)
class EvaluationRewriteEntry:
    case_id: str
    query_digest: str
    rewritten_query: str
    rewritten_query_digest: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluationRewriteEntry":
        entry = cls(
            case_id=str(payload["case_id"]),
            query_digest=str(payload["query_digest"]),
            rewritten_query=str(payload["rewritten_query"]),
            rewritten_query_digest=str(payload["rewritten_query_digest"]),
        )
        if entry.query_digest != _digest_query(payload.get("query", "")) and "query" in payload:
            raise ValueError("rewrite fixture query digest mismatch")
        if entry.rewritten_query_digest != _digest_query(entry.rewritten_query):
            raise ValueError("rewrite fixture rewritten query digest mismatch")
        return entry


@dataclass(frozen=True, slots=True)
class EvaluationRewriteFixture:
    fixture_version: str
    entries: tuple[EvaluationRewriteEntry, ...]
    fixture_id: str = field(init=False)

    def __post_init__(self) -> None:
        if len(self.entries) != 24:
            raise ValueError("evaluation rewrite fixture must contain 24 entries")
        if len({item.case_id for item in self.entries}) != 24:
            raise ValueError("rewrite fixture case ids must be unique")
        payload = {
            "fixture_version": self.fixture_version,
            "entries": [
                {
                    "case_id": item.case_id,
                    "query_digest": item.query_digest,
                    "rewritten_query": item.rewritten_query,
                    "rewritten_query_digest": item.rewritten_query_digest,
                }
                for item in self.entries
            ],
        }
        object.__setattr__(self, "fixture_id", canonical_sha256(payload))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluationRewriteFixture":
        if payload.get("schema_version") != "localagent-evaluation-rewrite-fixture.v1":
            raise ValueError("rewrite fixture schema mismatch")
        entries = tuple(EvaluationRewriteEntry.from_dict(item) for item in payload["entries"])
        fixture = cls(str(payload["fixture_version"]), entries)
        if payload.get("rewrite_fixture_id") != fixture.fixture_id:
            raise ValueError("rewrite_fixture_id mismatch")
        return fixture

    def resolve(self, *, case_id: str | None, query: str) -> EvaluationRewriteEntry:
        query_digest = _digest_query(query)
        matches = [item for item in self.entries if item.query_digest == query_digest]
        if case_id is not None:
            matches = [item for item in matches if item.case_id == case_id]
        if len(matches) != 1:
            raise ValueError("rewrite fixture case/query mismatch")
        return matches[0]


def load_rewrite_fixture(path: str | Path) -> EvaluationRewriteFixture:
    """加载并完整校验 immutable rewrite fixture。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return EvaluationRewriteFixture.from_dict(payload)


__all__ = [
    "EvaluationGenerationPin",
    "EvaluationRewriteEntry",
    "EvaluationRewriteFixture",
    "canonical_sha256",
    "load_generation_pin",
    "load_rewrite_fixture",
]
