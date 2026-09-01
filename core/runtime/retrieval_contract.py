#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Retrieval Runtime 的不可变合约与安全序列化边界。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4

from core.runtime.model_context import ContextTrustLevel


class RetrievalStage(str, Enum):
    QUERY_REWRITE = "QUERY_REWRITE"
    EMBEDDING = "EMBEDDING"
    RETRIEVE = "RETRIEVE"
    RERANK = "RERANK"
    DOCUMENT_LOAD = "DOCUMENT_LOAD"
    CONTEXT_BUILD = "CONTEXT_BUILD"


class RetrievalStageStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class RetrievalExecutionStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    EMPTY = "EMPTY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class RetrievalErrorCategory(str, Enum):
    VALIDATION = "VALIDATION"
    QUERY_REWRITE_FAILED = "QUERY_REWRITE_FAILED"
    EMBEDDING_FAILED = "EMBEDDING_FAILED"
    VECTOR_STORE_FAILED = "VECTOR_STORE_FAILED"
    DOCUMENT_LOAD_FAILED = "DOCUMENT_LOAD_FAILED"
    RERANK_FAILED = "RERANK_FAILED"
    SPARSE_RETRIEVAL_FAILED = "SPARSE_RETRIEVAL_FAILED"
    FUSION_FAILED = "FUSION_FAILED"
    STRATEGY_UNAVAILABLE = "STRATEGY_UNAVAILABLE"
    CONTEXT_BUILD_FAILED = "CONTEXT_BUILD_FAILED"
    METADATA_INVALID = "METADATA_INVALID"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    INTERNAL = "INTERNAL"


class RetrievalTransformation(str, Enum):
    LOADED = "LOADED"
    NORMALIZED = "NORMALIZED"
    DEDUPLICATED = "DEDUPLICATED"
    TRUNCATED = "TRUNCATED"
    RERANKED = "RERANKED"
    RRF_FUSED = "RRF_FUSED"
    CONTEXT_SELECTED = "CONTEXT_SELECTED"


class QueryRewriteStrategy(str, Enum):
    NONE = "NONE"
    EXISTING_MODEL = "EXISTING_MODEL"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")


def _require_positive_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} 必须是正整数")


def _require_non_negative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} 必须是非负整数")


def _require_positive_number(value: int | float, field_name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field_name} 必须是有限正数")


def _require_score(value: float, field_name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ValueError(f"{field_name} 必须是 0 到 1 的有限数")


def _require_utc(value: datetime, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.astimezone(UTC) != value
    ):
        raise ValueError(f"{field_name} 必须是带时区的 UTC 时间")


def _freeze_json(value: Any, path: str = "filters") -> Any:
    """递归校验并冻结 JSON-safe 值，拒绝非有限浮点数与任意对象。"""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} 不允许非有限浮点数")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} 的对象 Key 必须是字符串")
            frozen[key] = _freeze_json(item, f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, f"{path}[]") for item in value)
    raise ValueError(f"{path} 只允许 JSON-safe 值")


def thaw_json(value: Any) -> Any:
    """把冻结值还原为 Adapter 可消费的普通 JSON 结构。"""
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def normalize_query(value: str) -> str:
    """使用稳定、低损的空白规范化计算 Query 身份。"""
    _require_text(value, "original_query")
    return re.sub(r"\s+", " ", value).strip()


def query_digest(value: str) -> str:
    return hashlib.sha256(normalize_query(value).encode("utf-8")).hexdigest()


def content_digest(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("content 必须是字符串")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RetrievalInvocation:
    retrieval_id: str
    original_query: str = field(repr=False)
    collection_names: tuple[str, ...]
    top_k: int
    rerank_top_k: int
    requested_timeout_seconds: float
    filters: Mapping[str, Any] = field(default_factory=dict, repr=False)
    query_digest: str = ""

    def __post_init__(self) -> None:
        _require_text(self.retrieval_id, "retrieval_id")
        normalize_query(self.original_query)
        if (
            not isinstance(self.collection_names, tuple)
            or not self.collection_names
            or any(not isinstance(item, str) or not item.strip() for item in self.collection_names)
        ):
            raise ValueError("collection_names 必须是非空字符串元组")
        if len(set(self.collection_names)) != len(self.collection_names):
            raise ValueError("collection_names 不允许重复")
        _require_positive_int(self.top_k, "top_k")
        _require_positive_int(self.rerank_top_k, "rerank_top_k")
        if self.rerank_top_k > self.top_k:
            raise ValueError("rerank_top_k 不得大于 top_k")
        _require_positive_number(
            self.requested_timeout_seconds, "requested_timeout_seconds"
        )
        frozen = _freeze_json(self.filters)
        if not isinstance(frozen, Mapping):
            raise ValueError("filters 必须是 JSON 对象")
        object.__setattr__(self, "filters", frozen)
        expected = query_digest(self.original_query)
        if self.query_digest and self.query_digest != expected:
            raise ValueError("query_digest 与规范化 Query 不匹配")
        object.__setattr__(self, "query_digest", expected)

    @classmethod
    def create(
        cls,
        original_query: str,
        *,
        collection_names: tuple[str, ...],
        top_k: int,
        rerank_top_k: int | None = None,
        requested_timeout_seconds: float = 30.0,
        filters: Mapping[str, Any] | None = None,
        retrieval_id: str | None = None,
    ) -> "RetrievalInvocation":
        return cls(
            retrieval_id=retrieval_id or uuid4().hex,
            original_query=original_query,
            collection_names=collection_names,
            top_k=top_k,
            rerank_top_k=rerank_top_k or top_k,
            requested_timeout_seconds=requested_timeout_seconds,
            filters=filters or {},
        )

    def to_safe_dict(self) -> dict[str, object]:
        """Query 与 Filter 正文不进入普通日志或 Runtime Event。"""
        return {
            "retrieval_id": self.retrieval_id,
            "query_digest": self.query_digest,
            "collection_names": list(self.collection_names),
            "top_k": self.top_k,
            "rerank_top_k": self.rerank_top_k,
            "requested_timeout_seconds": self.requested_timeout_seconds,
            "filter_digest": hashlib.sha256(
                json.dumps(
                    thaw_json(self.filters),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }


def _default_stage_timeouts() -> Mapping[RetrievalStage, float]:
    return {
        RetrievalStage.QUERY_REWRITE: 8.0,
        RetrievalStage.EMBEDDING: 10.0,
        RetrievalStage.RETRIEVE: 10.0,
        RetrievalStage.RERANK: 5.0,
        RetrievalStage.DOCUMENT_LOAD: 10.0,
        RetrievalStage.CONTEXT_BUILD: 5.0,
    }


@dataclass(frozen=True, slots=True)
class RetrievalExecutionSpec:
    total_timeout_seconds: float = 30.0
    stage_timeouts: Mapping[RetrievalStage, float] = field(
        default_factory=_default_stage_timeouts
    )
    max_candidates: int = 16
    max_context_chunks: int = 4
    max_context_chars: int = 2400
    max_single_chunk_chars: int = 1000
    max_document_reads: int = 16
    allow_partial_document_load: bool = True

    def __post_init__(self) -> None:
        _require_positive_number(self.total_timeout_seconds, "total_timeout_seconds")
        frozen: dict[RetrievalStage, float] = {}
        for raw_stage, timeout in self.stage_timeouts.items():
            try:
                stage = (
                    raw_stage
                    if isinstance(raw_stage, RetrievalStage)
                    else RetrievalStage(str(raw_stage))
                )
            except ValueError as exc:
                raise ValueError("stage_timeouts 包含未知 Retrieval Stage") from exc
            _require_positive_number(timeout, f"{stage.value} timeout")
            frozen[stage] = float(timeout)
        missing = set(RetrievalStage) - set(frozen)
        if missing:
            raise ValueError(
                "stage_timeouts 缺少阶段：" + ", ".join(sorted(x.value for x in missing))
            )
        object.__setattr__(self, "stage_timeouts", MappingProxyType(frozen))
        for value, field_name in (
            (self.max_candidates, "max_candidates"),
            (self.max_context_chunks, "max_context_chunks"),
            (self.max_context_chars, "max_context_chars"),
            (self.max_single_chunk_chars, "max_single_chunk_chars"),
            (self.max_document_reads, "max_document_reads"),
        ):
            _require_positive_int(value, field_name)
        if self.max_context_chunks > self.max_document_reads:
            raise ValueError("max_context_chunks 不得大于 max_document_reads")
        if self.max_single_chunk_chars > self.max_context_chars:
            raise ValueError("max_single_chunk_chars 不得大于 max_context_chars")
        if not isinstance(self.allow_partial_document_load, bool):
            raise TypeError("allow_partial_document_load 必须是 bool")

    def timeout_for(self, stage: RetrievalStage) -> float:
        return self.stage_timeouts[stage]


@dataclass(frozen=True, slots=True)
class RetrievalBudgetUsage:
    retrieval_calls: int = 0
    embedding_calls: int = 0
    vector_queries: int = 0
    keyword_queries: int = 0
    bm25_queries: int = 0
    rrf_fusions: int = 0
    document_reads: int = 0
    context_chars: int = 0

    def __post_init__(self) -> None:
        for name in (
            "retrieval_calls",
            "embedding_calls",
            "vector_queries",
            "keyword_queries",
            "bm25_queries",
            "rrf_fusions",
            "document_reads",
            "context_chars",
        ):
            _require_non_negative_int(getattr(self, name), name)

    def plus(self, other: "RetrievalBudgetUsage") -> "RetrievalBudgetUsage":
        return RetrievalBudgetUsage(
            **{
                name: getattr(self, name) + getattr(other, name)
                for name in self.__dataclass_fields__
            }
        )

    def to_safe_dict(self) -> dict[str, int]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class RetrievalStageRecord:
    stage: RetrievalStage
    status: RetrievalStageStatus
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    input_count: int = 0
    output_count: int = 0
    safe_error_code: str | None = None
    budget_usage: RetrievalBudgetUsage = field(default_factory=RetrievalBudgetUsage)
    degraded: bool = False
    worker_terminated: bool = True
    execution_detached: bool = False
    background_work_pending: bool = False

    def __post_init__(self) -> None:
        _require_utc(self.started_at, "started_at")
        _require_utc(self.completed_at, "completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at 不得早于 started_at")
        _require_non_negative_int(self.duration_ms, "duration_ms")
        _require_non_negative_int(self.input_count, "input_count")
        _require_non_negative_int(self.output_count, "output_count")
        if self.safe_error_code is not None:
            _require_text(self.safe_error_code, "safe_error_code")
        if not isinstance(self.degraded, bool):
            raise TypeError("degraded 必须是 bool")
        for name in (
            "worker_terminated",
            "execution_detached",
            "background_work_pending",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} 必须是 bool")
        if self.execution_detached and self.worker_terminated:
            raise ValueError("Detached Worker 不能标记为已终止")
        if self.execution_detached and not self.background_work_pending:
            raise ValueError("Detached Worker 必须标记为后台工作待完成")

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage.value,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "duration_ms": self.duration_ms,
            "input_count": self.input_count,
            "output_count": self.output_count,
            "safe_error_code": self.safe_error_code,
            "budget_usage": self.budget_usage.to_safe_dict(),
            "degraded": self.degraded,
            "worker_terminated": self.worker_terminated,
            "execution_detached": self.execution_detached,
            "background_work_pending": self.background_work_pending,
        }


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    source_id: str
    source_type: str
    collection: str
    canonical_uri: str
    display_name: str
    document_version: str
    page: int | None
    section_path: str
    chunk_id: str
    chunk_index: int

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.source_id, "source_id"),
            (self.source_type, "source_type"),
            (self.collection, "collection"),
            (self.canonical_uri, "canonical_uri"),
            (self.display_name, "display_name"),
            (self.document_version, "document_version"),
            (self.chunk_id, "chunk_id"),
        ):
            _require_text(value, field_name)
        if self.page is not None:
            _require_positive_int(self.page, "page")
        if not isinstance(self.section_path, str):
            raise TypeError("section_path 必须是字符串")
        _require_non_negative_int(self.chunk_index, "chunk_index")

    def to_safe_dict(self) -> dict[str, object]:
        """路径正文默认不进入事件；仅输出稳定身份与结构字段。"""
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "collection": self.collection,
            "display_name": self.display_name,
            "document_version": self.document_version,
            "page": self.page,
            "section_path": self.section_path,
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
        }


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    candidate_id: str
    source: SourceMetadata
    score: float
    original_rank: int
    metadata: Mapping[str, Any]
    content_locator: str
    text: str | None = field(default=None, repr=False)
    reranked_score: float | None = None
    reranked_rank: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "candidate_id")
        _require_score(self.score, "score")
        _require_positive_int(self.original_rank, "original_rank")
        if self.reranked_score is not None:
            _require_score(self.reranked_score, "reranked_score")
        if self.reranked_rank is not None:
            _require_positive_int(self.reranked_rank, "reranked_rank")
        frozen = _freeze_json(self.metadata, "metadata")
        if not isinstance(frozen, Mapping):
            raise ValueError("metadata 必须是 JSON 对象")
        object.__setattr__(self, "metadata", frozen)
        _require_text(self.content_locator, "content_locator")
        if self.text is not None and not isinstance(self.text, str):
            raise TypeError("text 必须是字符串或 None")

    @property
    def source_id(self) -> str:
        return self.source.source_id

    @property
    def chunk_id(self) -> str:
        return self.source.chunk_id

    @property
    def effective_score(self) -> float:
        return self.reranked_score if self.reranked_score is not None else self.score


@dataclass(frozen=True, slots=True)
class MaterializedDocument:
    candidate: RetrievalCandidate
    text: str = field(repr=False)
    original_content_hash: str

    def __post_init__(self) -> None:
        _require_text(self.text, "text")
        _require_text(self.original_content_hash, "original_content_hash")
        if content_digest(self.text) != self.original_content_hash:
            raise ValueError("original_content_hash 与文档正文不匹配")


@dataclass(frozen=True, slots=True)
class RetrievalProvenance:
    source_id: str
    chunk_id: str
    original_rank: int
    reranked_rank: int | None
    retrieval_score: float
    transformations: tuple[RetrievalTransformation, ...]
    original_content_hash: str
    context_content_hash: str

    def __post_init__(self) -> None:
        _require_text(self.source_id, "source_id")
        _require_text(self.chunk_id, "chunk_id")
        _require_positive_int(self.original_rank, "original_rank")
        if self.reranked_rank is not None:
            _require_positive_int(self.reranked_rank, "reranked_rank")
        _require_score(self.retrieval_score, "retrieval_score")
        if not self.transformations:
            raise ValueError("transformations 不得为空")
        if any(not isinstance(item, RetrievalTransformation) for item in self.transformations):
            raise TypeError("transformations 必须使用 RetrievalTransformation")
        _require_text(self.original_content_hash, "original_content_hash")
        _require_text(self.context_content_hash, "context_content_hash")
        if (
            RetrievalTransformation.TRUNCATED in self.transformations
            and self.original_content_hash == self.context_content_hash
        ):
            raise ValueError("截断后的 Context Hash 必须与原文 Hash 不同")


@dataclass(frozen=True, slots=True)
class CitationBinding:
    citation_id: str
    source_id: str
    chunk_id: str
    context_block_id: str
    context_content_hash: str
    display_label: str
    page: int | None
    section_path: str

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.citation_id, "citation_id"),
            (self.source_id, "source_id"),
            (self.chunk_id, "chunk_id"),
            (self.context_block_id, "context_block_id"),
            (self.context_content_hash, "context_content_hash"),
            (self.display_label, "display_label"),
        ):
            _require_text(value, field_name)
        if self.page is not None:
            _require_positive_int(self.page, "page")
        if not isinstance(self.section_path, str):
            raise TypeError("section_path 必须是字符串")


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    context_block_id: str
    text: str = field(repr=False)
    source: SourceMetadata
    provenance: RetrievalProvenance
    citation: CitationBinding
    trust_level: ContextTrustLevel
    score: float

    def __post_init__(self) -> None:
        _require_text(self.context_block_id, "context_block_id")
        _require_text(self.text, "text")
        _require_score(self.score, "score")
        if self.trust_level not in {
            ContextTrustLevel.UNTRUSTED_EXTERNAL,
        }:
            raise ValueError("知识库正文只能使用不可信外部内容信任级别")
        if self.context_block_id != self.citation.context_block_id:
            raise ValueError("Citation 必须绑定当前 Context Block")
        if content_digest(self.text) != self.citation.context_content_hash:
            raise ValueError("Citation Context Hash 与最终正文不匹配")
        if self.source.source_id != self.citation.source_id:
            raise ValueError("Citation Source 与 Chunk Source 不匹配")
        if self.source.chunk_id != self.citation.chunk_id:
            raise ValueError("Citation Chunk 与最终 Chunk 不匹配")


@dataclass(frozen=True, slots=True)
class RetrievalExecutionError:
    category: RetrievalErrorCategory
    safe_error_code: str
    safe_message: str
    stage: RetrievalStage | None = None
    failed_source_count: int = 0

    def __post_init__(self) -> None:
        _require_text(self.safe_error_code, "safe_error_code")
        _require_text(self.safe_message, "safe_message")
        _require_non_negative_int(self.failed_source_count, "failed_source_count")

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "safe_error_code": self.safe_error_code,
            "safe_message": self.safe_message,
            "stage": self.stage.value if self.stage else None,
            "failed_source_count": self.failed_source_count,
        }


@dataclass(frozen=True, slots=True)
class RetrievalExecutionResult:
    retrieval_id: str
    status: RetrievalExecutionStatus
    rewritten_query_digest: str
    final_chunks: tuple[RetrievedChunk, ...]
    citations: tuple[CitationBinding, ...]
    stage_records: tuple[RetrievalStageRecord, ...]
    degraded: bool
    degradation_reasons: tuple[str, ...]
    budget_usage: RetrievalBudgetUsage
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    error: RetrievalExecutionError | None = None

    def __post_init__(self) -> None:
        _require_text(self.retrieval_id, "retrieval_id")
        _require_text(self.rewritten_query_digest, "rewritten_query_digest")
        _require_utc(self.started_at, "started_at")
        _require_utc(self.completed_at, "completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at 不得早于 started_at")
        _require_non_negative_int(self.duration_ms, "duration_ms")
        if self.degraded != bool(self.degradation_reasons):
            raise ValueError("degraded 必须与 degradation_reasons 是否存在一致")
        if self.status == RetrievalExecutionStatus.DEGRADED and not self.degraded:
            raise ValueError("DEGRADED 状态必须提供降级原因")
        if self.status == RetrievalExecutionStatus.SUCCEEDED and self.degraded:
            raise ValueError("SUCCEEDED 状态不得携带降级原因")
        if len(self.final_chunks) != len(self.citations):
            raise ValueError("最终 Chunk 与 Citation 必须一一对应")
        chunk_citations = tuple(item.citation for item in self.final_chunks)
        if chunk_citations != self.citations:
            raise ValueError("citations 必须严格对应最终 Chunk 顺序")
        if len({item.citation_id for item in self.citations}) != len(self.citations):
            raise ValueError("Citation ID 不允许重复")
        if self.status in {
            RetrievalExecutionStatus.SUCCEEDED,
            RetrievalExecutionStatus.DEGRADED,
        } and not self.final_chunks:
            raise ValueError("成功或降级结果必须包含最终 Chunk")
        if self.status == RetrievalExecutionStatus.EMPTY and self.final_chunks:
            raise ValueError("EMPTY 结果不得包含最终 Chunk")
        failed_status = self.status in {
            RetrievalExecutionStatus.FAILED,
            RetrievalExecutionStatus.CANCELLED,
            RetrievalExecutionStatus.TIMED_OUT,
        }
        if failed_status != (self.error is not None):
            raise ValueError("失败、取消或超时状态必须且只能携带安全 Error")

    @property
    def rendered_context(self) -> str:
        """兼容旧上层的字符串输入，同时保留每块独立 Citation。"""
        blocks = []
        for chunk in self.final_chunks:
            label = chunk.citation.display_label
            blocks.append(
                f"[来源: {label}] [{chunk.citation.citation_id}]\n{chunk.text}"
            )
        return "\n\n".join(blocks)

    @property
    def background_work_pending(self) -> bool:
        return any(item.background_work_pending for item in self.stage_records)

    @property
    def execution_detached(self) -> bool:
        return any(item.execution_detached for item in self.stage_records)

    @property
    def worker_terminated(self) -> bool:
        return not self.background_work_pending

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "retrieval_id": self.retrieval_id,
            "status": self.status.value,
            "rewritten_query_digest": self.rewritten_query_digest,
            "chunk_count": len(self.final_chunks),
            "citation_count": len(self.citations),
            "stage_records": [item.to_safe_dict() for item in self.stage_records],
            "degraded": self.degraded,
            "degradation_reasons": list(self.degradation_reasons),
            "budget_usage": self.budget_usage.to_safe_dict(),
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "duration_ms": self.duration_ms,
            "error": self.error.to_safe_dict() if self.error else None,
            "worker_terminated": self.worker_terminated,
            "execution_detached": self.execution_detached,
            "background_work_pending": self.background_work_pending,
        }


__all__ = [
    "CitationBinding",
    "MaterializedDocument",
    "QueryRewriteStrategy",
    "RetrievalBudgetUsage",
    "RetrievalCandidate",
    "RetrievalErrorCategory",
    "RetrievalExecutionError",
    "RetrievalExecutionResult",
    "RetrievalExecutionSpec",
    "RetrievalExecutionStatus",
    "RetrievalInvocation",
    "RetrievalProvenance",
    "RetrievalStage",
    "RetrievalStageRecord",
    "RetrievalStageStatus",
    "RetrievalTransformation",
    "RetrievedChunk",
    "SourceMetadata",
    "content_digest",
    "normalize_query",
    "query_digest",
    "thaw_json",
]
