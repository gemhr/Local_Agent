#!/usr/bin/env python
"""请求级 RAG evaluation sidecar；不参与 Runtime 状态或持久化。"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from enum import Enum
from uuid import UUID

from core.runtime.retrieval_contract import (
    RetrievalCandidate,
    RetrievalExecutionResult,
    RetrievalInvocation,
    RetrievalStage,
    RetrievalStageStatus,
)

ARTIFACT_SCHEMA_VERSION = "rag-evaluation-artifact.v1"
ARTIFACT_SCHEMA_VERSION_V2 = "rag-evaluation-artifact.v2"
MAX_ARTIFACTS_PER_RUN = 16
MAX_RETRIEVED_ITEMS = 64
MAX_RANKED_ITEMS = 64
MAX_SELECTED_ITEMS = 16
MAX_CITATIONS = 16
MAX_QUERY_CHARS = 32_768
MAX_SELECTED_TEXT_CHARS = 32_768
MAX_SCALAR_CHARS = 1_024
MAX_RESPONSE_BYTES = 1_048_576

_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]*$")
_CAPTURE_ERROR_CODE = re.compile(r"^RAG_EVALUATION_[A-Z0-9_]+$")


class RetrievalEvaluationCaptureError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("retrieval evaluation capture failed")


def _capture_error(code: str) -> RetrievalEvaluationCaptureError:
    return RetrievalEvaluationCaptureError(code)


class RetrievalEvaluationChannel(str, Enum):
    VECTOR_REWRITTEN_QUERY = "VECTOR_REWRITTEN_QUERY"
    VECTOR_ORIGINAL_QUERY = "VECTOR_ORIGINAL_QUERY"
    VECTOR_ORIGINAL_AND_REWRITTEN = "VECTOR_ORIGINAL_AND_REWRITTEN"
    KEYWORD = "KEYWORD"
    # Stage5-Phase6-WP1 artifact v2：Hybrid 语义通道（WP1 只建立 schema/plumbing，
    # BASELINE 不产生这些值）。
    BM25 = "BM25"
    RRF = "RRF"


class RetrievalEvaluationScoreKind(str, Enum):
    VECTOR_NORMALIZED_RELEVANCE = "VECTOR_NORMALIZED_RELEVANCE"
    KEYWORD_FIXED_HEURISTIC = "KEYWORD_FIXED_HEURISTIC"
    HEURISTIC_RERANK = "HEURISTIC_RERANK"
    # Stage5-Phase6-WP1 artifact v2：Hybrid 评分语义（WP1 只建立 schema/plumbing）。
    BM25_RAW_SCORE = "BM25_RAW_SCORE"
    RRF_SCORE = "RRF_SCORE"


class RetrievalEvaluationCaptureStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


def _bounded_scalar(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_SCALAR_CHARS:
        raise ValueError(f"{field_name} exceeds evaluation wire bounds")
    return value


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationSource:
    source_type: str
    collection: str
    display_name: str
    document_version: str

    def __post_init__(self) -> None:
        for name in ("source_type", "collection", "display_name", "document_version"):
            _bounded_scalar(getattr(self, name), name)

    def to_wire_dict(self) -> dict[str, object]:
        return {
            "source_type": self.source_type,
            "collection": self.collection,
            "display_name": self.display_name,
            "document_version": self.document_version,
        }


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationCandidateItem:
    document_id: str
    chunk_id: str
    rank: int
    retrieval_rank: int
    retrieval_score: float
    retrieval_score_kind: str
    retrieval_channels: tuple[str, ...]
    source: RetrievalEvaluationSource
    selected: bool
    rerank_rank: int | None = None
    rerank_score: float | None = None
    rerank_score_kind: str | None = None
    page: int | None = None
    section: str | None = None
    sheet: str | None = None
    content_hash: str | None = None
    # WP2 Hybrid v2 optional channel ranks（缺失代表旧 producer/v1 兼容工件）。
    dense_channel_rank: int | None = None
    bm25_channel_rank: int | None = None
    rrf_fused_rank: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "document_id",
            "chunk_id",
            "retrieval_score_kind",
            "section",
            "sheet",
            "content_hash",
        ):
            _bounded_scalar(getattr(self, name), name)
        if self.rank <= 0 or self.retrieval_rank <= 0:
            raise ValueError("evaluation ranks must be positive")
        if self.rerank_rank is not None and self.rerank_rank <= 0:
            raise ValueError("rerank_rank must be positive")
        for name in (
            "dense_channel_rank",
            "bm25_channel_rank",
            "rrf_fused_rank",
        ):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")

    def to_wire_dict(self) -> dict[str, object]:
        payload = {
            "document_id": self.document_id,
            "chunk_id": self.chunk_id,
            "rank": self.rank,
            "retrieval_rank": self.retrieval_rank,
            "rerank_rank": self.rerank_rank,
            "retrieval_score": self.retrieval_score,
            "retrieval_score_kind": self.retrieval_score_kind,
            "retrieval_channels": list(self.retrieval_channels),
            "rerank_score": self.rerank_score,
            "rerank_score_kind": self.rerank_score_kind,
            "source": self.source.to_wire_dict(),
            "page": self.page,
            "section": self.section,
            "sheet": self.sheet,
            "content_hash": self.content_hash,
            "selected": self.selected,
        }
        # Optional v2 ranks：仅在有值时发射（v1/旧 v2 工件保持字段缺失）。
        if self.dense_channel_rank is not None:
            payload["dense_channel_rank"] = self.dense_channel_rank
        if self.bm25_channel_rank is not None:
            payload["bm25_channel_rank"] = self.bm25_channel_rank
        if self.rrf_fused_rank is not None:
            payload["rrf_fused_rank"] = self.rrf_fused_rank
        return payload


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationSelectedItem:
    document_id: str
    chunk_id: str
    selection_rank: int
    context_block_id: str
    citation_id: str
    context_content_hash: str
    text: str

    def __post_init__(self) -> None:
        for name in (
            "document_id",
            "chunk_id",
            "context_block_id",
            "citation_id",
            "context_content_hash",
        ):
            _bounded_scalar(getattr(self, name), name)
        if self.selection_rank <= 0 or not isinstance(self.text, str):
            raise ValueError("invalid selected evaluation item")

    def to_wire_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "chunk_id": self.chunk_id,
            "selection_rank": self.selection_rank,
            "context_block_id": self.context_block_id,
            "citation_id": self.citation_id,
            "context_content_hash": self.context_content_hash,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationCitation:
    citation_id: str
    document_id: str
    chunk_id: str
    context_block_id: str
    context_content_hash: str
    display_label: str
    page: int | None
    section: str | None

    def __post_init__(self) -> None:
        for name in (
            "citation_id",
            "document_id",
            "chunk_id",
            "context_block_id",
            "context_content_hash",
            "display_label",
            "section",
        ):
            _bounded_scalar(getattr(self, name), name)

    def to_wire_dict(self) -> dict[str, object]:
        return {
            "citation_id": self.citation_id,
            "document_id": self.document_id,
            "chunk_id": self.chunk_id,
            "context_block_id": self.context_block_id,
            "context_content_hash": self.context_content_hash,
            "display_label": self.display_label,
            "page": self.page,
            "section": self.section,
        }


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationError:
    category: str
    safe_error_code: str
    safe_message: str
    stage: str | None
    failed_source_count: int

    def __post_init__(self) -> None:
        for name in ("category", "safe_error_code", "safe_message", "stage"):
            _bounded_scalar(getattr(self, name), name)

    def to_wire_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "safe_error_code": self.safe_error_code,
            "safe_message": self.safe_message,
            "stage": self.stage,
            "failed_source_count": self.failed_source_count,
        }


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationBudgetUsage:
    retrieval_calls: int
    embedding_calls: int
    vector_queries: int
    keyword_queries: int
    bm25_queries: int
    rrf_fusions: int
    document_reads: int
    context_chars: int

    def to_wire_dict(self) -> dict[str, int]:
        return {
            "retrieval_calls": self.retrieval_calls,
            "embedding_calls": self.embedding_calls,
            "vector_queries": self.vector_queries,
            "keyword_queries": self.keyword_queries,
            "bm25_queries": self.bm25_queries,
            "rrf_fusions": self.rrf_fusions,
            "document_reads": self.document_reads,
            "context_chars": self.context_chars,
        }


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationSnapshot:
    schema_version: str
    artifact_id: str
    run_id: str
    attempt_id: str
    retrieval_id: str
    invocation_index: int
    retrieval_status: str
    query: str
    rewritten_query: str
    retrieved_items: tuple[RetrievalEvaluationCandidateItem, ...]
    ranked_items: tuple[RetrievalEvaluationCandidateItem, ...]
    selected_items: tuple[RetrievalEvaluationSelectedItem, ...]
    citations: tuple[RetrievalEvaluationCitation, ...]
    retrieval_latency_ms: int | None
    rerank_latency_ms: int | None
    total_latency_ms: int
    degraded: bool
    degradation_reasons: tuple[str, ...]
    error: RetrievalEvaluationError | None
    budget_usage: RetrievalEvaluationBudgetUsage
    # Stage5-Phase6-WP1 artifact v2：snapshot 级 Hybrid 能力字段（仅 schema/plumbing；
    # BASELINE producer 不填充 BM25/RRF 通道值）。
    retrieval_strategy: str | None = None
    provenance_sha256: str | None = None

    def to_wire_dict(self) -> dict[str, object]:
        payload = {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "retrieval_id": self.retrieval_id,
            "invocation_index": self.invocation_index,
            "retrieval_status": self.retrieval_status,
            "query": self.query,
            "rewritten_query": self.rewritten_query,
            "retrieved_items": [item.to_wire_dict() for item in self.retrieved_items],
            "ranked_items": [item.to_wire_dict() for item in self.ranked_items],
            "selected_items": [item.to_wire_dict() for item in self.selected_items],
            "citations": [item.to_wire_dict() for item in self.citations],
            "retrieval_latency_ms": self.retrieval_latency_ms,
            "rerank_latency_ms": self.rerank_latency_ms,
            "total_latency_ms": self.total_latency_ms,
            "degraded": self.degraded,
            "degradation_reasons": list(self.degradation_reasons),
            "error": self.error.to_wire_dict() if self.error else None,
            "budget_usage": self.budget_usage.to_wire_dict(),
        }
        if self.retrieval_strategy is not None:
            payload["retrieval_strategy"] = self.retrieval_strategy
        if self.provenance_sha256 is not None:
            payload["provenance_sha256"] = self.provenance_sha256
        return payload


@dataclass(slots=True)
class _Observation:
    winner: RetrievalCandidate
    winner_kind: RetrievalEvaluationScoreKind
    channels: list[RetrievalEvaluationChannel]


class RetrievalEvaluationCaptureBuilder:
    """只保存当前 invocation 的原执行事实；所有 capture 失败均留在 sidecar。"""

    def __init__(
        self,
        *,
        run_id: str,
        invocation_index: int,
        invocation: RetrievalInvocation,
        max_context_chars: int,
        retrieval_strategy: str = "BASELINE",
        provenance_sha256: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.invocation_index = invocation_index
        self.invocation = invocation
        self.max_context_chars = max_context_chars
        self.retrieval_strategy = retrieval_strategy
        self.provenance_sha256 = provenance_sha256
        self.rewritten_query = invocation.original_query
        self._observations: dict[str, _Observation] = {}
        self._retrieved: tuple[RetrievalEvaluationCandidateItem, ...] = ()
        self._ranked: tuple[RetrievalEvaluationCandidateItem, ...] = ()
        # WP2：BM25 channel 输入证据（与 Dense 证据行共存于 retrieved_items）。
        self._bm25_evidence: tuple[RetrievalEvaluationCandidateItem, ...] = ()
        self.capture_error_code: str | None = None

    def _fail(self, code: str) -> None:
        if self.capture_error_code is None:
            self.capture_error_code = code

    def capture_rewritten_query(self, value: str) -> None:
        try:
            if not isinstance(value, str):
                raise TypeError
            self.rewritten_query = value
        except Exception:  # noqa: BLE001 - capture 失败必须与 Runtime 隔离
            self._fail("RAG_EVALUATION_REWRITE_CAPTURE_FAILED")

    def observe_candidates(
        self,
        candidates: Sequence[RetrievalCandidate],
        channel: RetrievalEvaluationChannel,
    ) -> None:
        try:
            kind = (
                RetrievalEvaluationScoreKind.KEYWORD_FIXED_HEURISTIC
                if channel is RetrievalEvaluationChannel.KEYWORD
                else RetrievalEvaluationScoreKind.VECTOR_NORMALIZED_RELEVANCE
            )
            for candidate in candidates:
                if channel is RetrievalEvaluationChannel.KEYWORD and not math.isclose(
                    candidate.score, 0.55, rel_tol=0.0, abs_tol=0.0
                ):
                    raise ValueError
                observation = self._observations.get(candidate.candidate_id)
                if observation is None:
                    self._observations[candidate.candidate_id] = _Observation(
                        winner=candidate,
                        winner_kind=kind,
                        channels=[channel],
                    )
                    continue
                if channel not in observation.channels:
                    observation.channels.append(channel)
                if candidate.score > observation.winner.score:
                    observation.winner = candidate
                    observation.winner_kind = kind
        except Exception:  # noqa: BLE001 - capture 失败必须与 Runtime 隔离
            self._fail("RAG_EVALUATION_PROVENANCE_CAPTURE_FAILED")

    def _project_candidate(
        self,
        candidate: RetrievalCandidate,
        *,
        rank: int,
        reranked: bool,
    ) -> RetrievalEvaluationCandidateItem:
        observation = self._observations[candidate.candidate_id]
        metadata = candidate.metadata
        sheet = metadata.get("sheet_name")
        content_hash = metadata.get("content_hash")
        return RetrievalEvaluationCandidateItem(
            document_id=candidate.source_id,
            chunk_id=candidate.chunk_id,
            rank=rank,
            retrieval_rank=candidate.original_rank,
            retrieval_score=candidate.score,
            retrieval_score_kind=observation.winner_kind.value,
            retrieval_channels=tuple(item.value for item in observation.channels),
            rerank_rank=candidate.reranked_rank if reranked else None,
            rerank_score=candidate.reranked_score if reranked else None,
            rerank_score_kind=(
                RetrievalEvaluationScoreKind.HEURISTIC_RERANK.value
                if reranked
                else None
            ),
            source=RetrievalEvaluationSource(
                source_type=candidate.source.source_type,
                collection=candidate.source.collection,
                display_name=candidate.source.display_name,
                document_version=candidate.source.document_version,
            ),
            page=candidate.source.page,
            section=candidate.source.section_path or None,
            sheet=sheet if isinstance(sheet, str) else None,
            content_hash=content_hash if isinstance(content_hash, str) else None,
            selected=False,
        )

    def capture_retrieved(self, candidates: Sequence[RetrievalCandidate]) -> None:
        try:
            if len(candidates) > MAX_RETRIEVED_ITEMS:
                raise ValueError
            self._retrieved = tuple(
                self._project_candidate(candidate, rank=index, reranked=False)
                for index, candidate in enumerate(candidates, start=1)
            )
        except Exception:  # noqa: BLE001 - capture 失败必须与 Runtime 隔离
            self._fail("RAG_EVALUATION_RETRIEVED_CAPTURE_FAILED")

    def capture_ranked(
        self,
        candidates: Sequence[RetrievalCandidate],
        *,
        reranked: bool,
    ) -> None:
        try:
            if len(candidates) > MAX_RANKED_ITEMS:
                raise ValueError
            self._ranked = tuple(
                self._project_candidate(candidate, rank=index, reranked=reranked)
                for index, candidate in enumerate(candidates, start=1)
            )
        except Exception:  # noqa: BLE001 - capture 失败必须与 Runtime 隔离
            self._fail("RAG_EVALUATION_RANKED_CAPTURE_FAILED")

    def capture_bm25_evidence(self, evidence_rows: Sequence) -> None:
        """记录 BM25 channel 输入证据（raw score，不与 Dense score 合并）。

        同一 ``(document_id, chunk_id)`` 可同时存在 Dense 与 BM25 证据行——
        这是 frozen decision §21 的合法 multi-channel retrieved 语义。
        """
        try:
            if len(evidence_rows) > MAX_RANKED_ITEMS:
                raise ValueError
            if self.retrieval_strategy != "HYBRID_RRF":
                raise ValueError
            projected = []
            for offset, row in enumerate(evidence_rows, start=1):
                _bounded_scalar(row.document_id, "document_id")
                _bounded_scalar(row.chunk_id, "chunk_id")
                projected.append(
                    RetrievalEvaluationCandidateItem(
                        document_id=row.document_id,
                        chunk_id=row.chunk_id,
                        rank=len(self._retrieved) + offset,
                        retrieval_rank=row.rank,
                        retrieval_score=float(row.raw_score),
                        retrieval_score_kind=(
                            RetrievalEvaluationScoreKind.BM25_RAW_SCORE.value
                        ),
                        retrieval_channels=(RetrievalEvaluationChannel.BM25.value,),
                        source=RetrievalEvaluationSource(
                            source_type="bm25_sparse_index",
                            collection=self.invocation.collection_names[0],
                            display_name=row.chunk_id,
                            document_version="bm25",
                        ),
                        selected=False,
                        bm25_channel_rank=row.rank,
                    )
                )
            self._bm25_evidence = tuple(projected)
        except Exception:  # noqa: BLE001 - capture 失败必须与 Runtime 隔离
            self._fail("RAG_EVALUATION_SPARSE_EVIDENCE_CAPTURE_FAILED")

    def capture_hybrid_ranked(self, fusion_result) -> None:
        """记录唯一 fused ranking（RRF_SCORE + 参与通道 + optional ranks）。"""
        try:
            if self.retrieval_strategy != "HYBRID_RRF":
                raise ValueError
            candidates = list(fusion_result.candidates)
            if len(candidates) > MAX_RANKED_ITEMS:
                raise ValueError
            facts_by_candidate = {
                fact.candidate_id: fact for fact in fusion_result.ranked_facts
            }
            projected: list[RetrievalEvaluationCandidateItem] = []
            for index, candidate in enumerate(candidates, start=1):
                fact = facts_by_candidate.get(candidate.candidate_id)
                if fact is None:
                    raise ValueError
                observation = self._observations.get(candidate.candidate_id)
                channels: list[str] = []
                if observation is not None:
                    channels.extend(item.value for item in observation.channels)
                if fact.bm25_channel_rank is not None:
                    channels.append(RetrievalEvaluationChannel.BM25.value)
                channels.append(RetrievalEvaluationChannel.RRF.value)
                metadata = candidate.metadata
                sheet = metadata.get("sheet_name")
                content_hash = metadata.get("content_hash")
                projected.append(
                    RetrievalEvaluationCandidateItem(
                        document_id=candidate.source_id,
                        chunk_id=candidate.chunk_id,
                        rank=index,
                        retrieval_rank=candidate.original_rank,
                        retrieval_score=float(candidate.reranked_score),
                        retrieval_score_kind=(
                            RetrievalEvaluationScoreKind.RRF_SCORE.value
                        ),
                        retrieval_channels=tuple(dict.fromkeys(channels)),
                        source=RetrievalEvaluationSource(
                            source_type=candidate.source.source_type,
                            collection=candidate.source.collection,
                            display_name=candidate.source.display_name,
                            document_version=candidate.source.document_version,
                        ),
                        selected=False,
                        page=candidate.source.page,
                        section=candidate.source.section_path or None,
                        sheet=sheet if isinstance(sheet, str) else None,
                        content_hash=(
                            content_hash if isinstance(content_hash, str) else None
                        ),
                        dense_channel_rank=fact.dense_channel_rank,
                        bm25_channel_rank=fact.bm25_channel_rank,
                        rrf_fused_rank=index,
                    )
                )
            self._ranked = tuple(projected)
        except Exception:  # noqa: BLE001 - capture 失败必须与 Runtime 隔离
            self._fail("RAG_EVALUATION_HYBRID_RANKED_CAPTURE_FAILED")

    def finalize(self, result: RetrievalExecutionResult) -> RetrievalEvaluationSnapshot:
        if self.capture_error_code is not None:
            raise _capture_error(self.capture_error_code)
        if (
            len(self.invocation.original_query) > MAX_QUERY_CHARS
            or len(self.rewritten_query) > MAX_QUERY_CHARS
        ):
            raise _capture_error("RAG_EVALUATION_QUERY_LIMIT_EXCEEDED")
        if (
            len(result.final_chunks) > MAX_SELECTED_ITEMS
            or len(result.citations) > MAX_CITATIONS
        ):
            raise _capture_error("RAG_EVALUATION_ITEM_LIMIT_EXCEEDED")
        if sum(len(chunk.text) for chunk in result.final_chunks) > min(
            MAX_SELECTED_TEXT_CHARS, self.max_context_chars
        ):
            raise _capture_error("RAG_EVALUATION_SELECTED_TEXT_LIMIT_EXCEEDED")
        if result.retrieval_id != self.invocation.retrieval_id:
            raise _capture_error("RAG_EVALUATION_RETRIEVAL_ID_MISMATCH")
        selected_identities = {
            (chunk.source.source_id, chunk.source.chunk_id)
            for chunk in result.final_chunks
        }
        ranked_by_identity = {
            (item.document_id, item.chunk_id): item for item in self._ranked
        }
        retrieved = tuple(
            replace(
                item,
                selected=(item.document_id, item.chunk_id) in selected_identities,
                rerank_rank=(
                    ranked_by_identity[(item.document_id, item.chunk_id)].rerank_rank
                    if (item.document_id, item.chunk_id) in ranked_by_identity
                    else None
                ),
                rerank_score=(
                    ranked_by_identity[(item.document_id, item.chunk_id)].rerank_score
                    if (item.document_id, item.chunk_id) in ranked_by_identity
                    else None
                ),
                rerank_score_kind=(
                    ranked_by_identity[
                        (item.document_id, item.chunk_id)
                    ].rerank_score_kind
                    if (item.document_id, item.chunk_id) in ranked_by_identity
                    else None
                ),
            )
            for item in self._retrieved + self._bm25_evidence
        )
        ranked = tuple(
            replace(
                item,
                selected=(item.document_id, item.chunk_id) in selected_identities,
            )
            for item in self._ranked
        )
        retrieved_ids = {(item.document_id, item.chunk_id) for item in retrieved}
        ranked_ids = {(item.document_id, item.chunk_id) for item in ranked}
        if not selected_identities <= ranked_ids <= retrieved_ids:
            raise _capture_error("RAG_EVALUATION_IDENTITY_INVARIANT_FAILED")
        selected = tuple(
            RetrievalEvaluationSelectedItem(
                document_id=chunk.source.source_id,
                chunk_id=chunk.source.chunk_id,
                selection_rank=index,
                context_block_id=chunk.context_block_id,
                citation_id=chunk.citation.citation_id,
                context_content_hash=chunk.citation.context_content_hash,
                text=chunk.text,
            )
            for index, chunk in enumerate(result.final_chunks, start=1)
        )
        citations = tuple(
            RetrievalEvaluationCitation(
                citation_id=item.citation_id,
                document_id=item.source_id,
                chunk_id=item.chunk_id,
                context_block_id=item.context_block_id,
                context_content_hash=item.context_content_hash,
                display_label=item.display_label,
                page=item.page,
                section=item.section_path or None,
            )
            for item in result.citations
        )
        retrieval_latency = None
        rerank_latency = None
        for record in result.stage_records:
            if (
                record.stage is RetrievalStage.RETRIEVE
                and record.status is not RetrievalStageStatus.SKIPPED
            ):
                retrieval_latency = record.duration_ms
            if (
                record.stage is RetrievalStage.RERANK
                and record.status is not RetrievalStageStatus.SKIPPED
            ):
                rerank_latency = record.duration_ms
        error = (
            RetrievalEvaluationError(
                category=result.error.category.value,
                safe_error_code=result.error.safe_error_code,
                safe_message=result.error.safe_message,
                stage=result.error.stage.value if result.error.stage else None,
                failed_source_count=result.error.failed_source_count,
            )
            if result.error is not None
            else None
        )
        for reason in result.degradation_reasons:
            _bounded_scalar(reason, "degradation_reason")
        return RetrievalEvaluationSnapshot(
            schema_version=(
                ARTIFACT_SCHEMA_VERSION_V2
                if self.retrieval_strategy == "HYBRID_RRF"
                else ARTIFACT_SCHEMA_VERSION
            ),
            artifact_id=f"rag-eval://{self.run_id}/{self.invocation.retrieval_id}",
            run_id=self.run_id,
            attempt_id=self.run_id,
            retrieval_id=self.invocation.retrieval_id,
            invocation_index=self.invocation_index,
            retrieval_status=result.status.value,
            query=self.invocation.original_query,
            rewritten_query=self.rewritten_query,
            retrieved_items=retrieved,
            ranked_items=ranked,
            selected_items=selected,
            citations=citations,
            retrieval_latency_ms=retrieval_latency,
            rerank_latency_ms=rerank_latency,
            total_latency_ms=result.duration_ms,
            degraded=result.degraded,
            degradation_reasons=result.degradation_reasons,
            error=error,
            budget_usage=RetrievalEvaluationBudgetUsage(
                retrieval_calls=result.budget_usage.retrieval_calls,
                embedding_calls=result.budget_usage.embedding_calls,
                vector_queries=result.budget_usage.vector_queries,
                keyword_queries=result.budget_usage.keyword_queries,
                bm25_queries=result.budget_usage.bm25_queries,
                rrf_fusions=result.budget_usage.rrf_fusions,
                document_reads=result.budget_usage.document_reads,
                context_chars=result.budget_usage.context_chars,
            ),
            retrieval_strategy=self.retrieval_strategy,
            provenance_sha256=self.provenance_sha256,
        )


class RetrievalEvaluationCollector:
    """单 request、单 run 的并发安全 collector。"""

    def __init__(
        self,
        run_id: str,
        *,
        retrieval_strategy: str = "BASELINE",
        provenance_sha256: str | None = None,
    ) -> None:
        UUID(run_id)
        self.run_id = run_id
        self.retrieval_strategy = retrieval_strategy
        self.provenance_sha256 = provenance_sha256
        self._lock = threading.Lock()
        self._next_index = 1
        self._started_ids: set[str] = set()
        self._snapshots: dict[int, RetrievalEvaluationSnapshot] = {}
        self._capture_error_code: str | None = None

    def _record_failure_locked(self, code: str) -> None:
        if (
            not isinstance(code, str)
            or len(code) > MAX_SCALAR_CHARS
            or not _CAPTURE_ERROR_CODE.fullmatch(code)
        ):
            code = "RAG_EVALUATION_CAPTURE_FAILED"
        if self._capture_error_code is None:
            self._capture_error_code = code

    def record_failure(self, code: str) -> None:
        with self._lock:
            self._record_failure_locked(code)

    def begin(
        self,
        *,
        run_id: str,
        invocation: RetrievalInvocation,
        max_context_chars: int,
    ) -> RetrievalEvaluationCaptureBuilder | None:
        with self._lock:
            if run_id != self.run_id:
                self._record_failure_locked("RAG_EVALUATION_RUN_ID_MISMATCH")
                return None
            if invocation.retrieval_id in self._started_ids:
                self._record_failure_locked("RAG_EVALUATION_DUPLICATE_RETRIEVAL_ID")
                return None
            if len(self._started_ids) >= MAX_ARTIFACTS_PER_RUN:
                self._record_failure_locked("RAG_EVALUATION_ARTIFACT_LIMIT_EXCEEDED")
                return None
            artifact_id = f"rag-eval://{run_id}/{invocation.retrieval_id}"
            if len(artifact_id) > MAX_SCALAR_CHARS or not _WIRE_ID.fullmatch(
                invocation.retrieval_id
            ):
                self._record_failure_locked("RAG_EVALUATION_RETRIEVAL_ID_INVALID")
                return None
            index = self._next_index
            self._next_index += 1
            self._started_ids.add(invocation.retrieval_id)
        return RetrievalEvaluationCaptureBuilder(
            run_id=run_id,
            invocation_index=index,
            invocation=invocation,
            max_context_chars=max_context_chars,
            retrieval_strategy=self.retrieval_strategy,
            provenance_sha256=self.provenance_sha256,
        )

    def complete(
        self,
        builder: RetrievalEvaluationCaptureBuilder,
        result: RetrievalExecutionResult,
    ) -> None:
        try:
            snapshot = builder.finalize(result)
        except RetrievalEvaluationCaptureError as exc:
            self.record_failure(exc.code)
            return
        except Exception:  # noqa: BLE001 - projection 不得改变 retrieval result
            self.record_failure("RAG_EVALUATION_PROJECTION_FAILED")
            return
        with self._lock:
            if snapshot.run_id != self.run_id:
                self._record_failure_locked("RAG_EVALUATION_RUN_ID_MISMATCH")
                return
            if builder.invocation_index in self._snapshots or any(
                item.retrieval_id == snapshot.retrieval_id
                for item in self._snapshots.values()
            ):
                self._record_failure_locked("RAG_EVALUATION_DUPLICATE_RETRIEVAL_ID")
                return
            self._snapshots[builder.invocation_index] = snapshot

    def envelope(
        self,
    ) -> tuple[
        RetrievalEvaluationCaptureStatus,
        str | None,
        tuple[RetrievalEvaluationSnapshot, ...],
    ]:
        with self._lock:
            snapshots = tuple(
                self._snapshots[index] for index in sorted(self._snapshots)
            )
            error_code = self._capture_error_code
        if error_code is None:
            status = RetrievalEvaluationCaptureStatus.COMPLETE
        elif snapshots:
            status = RetrievalEvaluationCaptureStatus.PARTIAL
        else:
            status = RetrievalEvaluationCaptureStatus.FAILED
        return status, error_code, snapshots


_CURRENT_COLLECTOR: ContextVar[RetrievalEvaluationCollector | None] = ContextVar(
    "retrieval_evaluation_collector", default=None
)


def current_retrieval_evaluation_collector() -> RetrievalEvaluationCollector | None:
    return _CURRENT_COLLECTOR.get()


def install_retrieval_evaluation_collector(
    collector: RetrievalEvaluationCollector,
) -> Token[RetrievalEvaluationCollector | None]:
    return _CURRENT_COLLECTOR.set(collector)


def reset_retrieval_evaluation_collector(
    token: Token[RetrievalEvaluationCollector | None],
) -> None:
    _CURRENT_COLLECTOR.reset(token)


__all__ = [
    "MAX_RESPONSE_BYTES",
    "RetrievalEvaluationCaptureStatus",
    "RetrievalEvaluationChannel",
    "RetrievalEvaluationCollector",
    "RetrievalEvaluationSnapshot",
    "current_retrieval_evaluation_collector",
    "install_retrieval_evaluation_collector",
    "reset_retrieval_evaluation_collector",
]
