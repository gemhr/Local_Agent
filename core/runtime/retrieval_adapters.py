#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把现有 Knowledge Expert、Embedding 与 Chroma 调用接入 Retrieval Runtime。"""

from __future__ import annotations

import hashlib
import inspect
import math
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Protocol, Sequence

from core.knowledge_base.vector_scores import (
    VectorScoreSemantics,
    normalize_vector_score,
)
from core.runtime.budget import BudgetExceededError
from core.runtime.cancellation import RunCancelledError
from core.runtime.context import RunContext, RunDeadlineExceededError
from core.runtime.event_emitter import StepEventEmitter
from core.runtime.fault_injection import FaultInjectionController
from core.runtime.retrieval_contract import (
    MaterializedDocument,
    QueryRewriteStrategy,
    RetrievalCandidate,
    RetrievalErrorCategory,
    RetrievalInvocation,
    SourceMetadata,
    content_digest,
    query_digest,
    thaw_json,
)


class RetrievalAdapterError(RuntimeError):
    """Adapter 只暴露安全分类和短错误码，不携带原始异常。"""

    def __init__(
        self,
        category: RetrievalErrorCategory,
        safe_error_code: str,
        safe_message: str,
    ) -> None:
        self.category = category
        self.safe_error_code = safe_error_code
        self.safe_message = safe_message
        super().__init__(safe_message)


@dataclass(frozen=True, slots=True)
class QueryEmbedding:
    """向量仅停留在进程内合约，不提供普通安全序列化。"""

    query_digest: str
    vector: tuple[float, ...] = field(repr=False)
    model_id: str
    vector_digest: str

    def __post_init__(self) -> None:
        if not self.query_digest:
            raise ValueError("query_digest 不能为空")
        if not self.vector:
            raise ValueError("Embedding 结果不能为空")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in self.vector
        ):
            raise ValueError("Embedding 只能包含有限数值")
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("model_id 不能为空")
        if not self.vector_digest:
            raise ValueError("vector_digest 不能为空")

    @classmethod
    def create(
        cls, query: str, vector: Sequence[float], model_id: str
    ) -> "QueryEmbedding":
        normalized = tuple(float(value) for value in vector)
        digest = hashlib.sha256(
            ",".join(format(value, ".12g") for value in normalized).encode("ascii")
        ).hexdigest()
        return cls(query_digest(query), normalized, model_id, digest)

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "query_digest": self.query_digest,
            "model_id": self.model_id,
            "dimension": len(self.vector),
            "vector_digest": self.vector_digest,
        }


class RetrievalAdapter(Protocol):
    query_rewrite_strategy: QueryRewriteStrategy
    has_explicit_embedding: bool
    has_reranker: bool

    def rewrite_query(
        self,
        query: str,
        *,
        run_context: RunContext,
        event_emitter: StepEventEmitter | None,
        fault_controller: FaultInjectionController | None = None,
    ) -> str: ...

    def embed_query(self, query: str) -> QueryEmbedding: ...

    def retrieve(
        self,
        query: str,
        embedding: QueryEmbedding | None,
        invocation: RetrievalInvocation,
        *,
        max_candidates: int,
    ) -> list[RetrievalCandidate]: ...

    def keyword_retrieve(
        self,
        terms: list[str],
        invocation: RetrievalInvocation,
        *,
        max_candidates: int,
    ) -> list[RetrievalCandidate]: ...

    def should_keyword_retrieve(
        self,
        terms: list[str],
        invocation: RetrievalInvocation,
    ) -> bool: ...

    def rerank(
        self,
        rewritten_query: str,
        original_query: str,
        candidates: Sequence[RetrievalCandidate],
    ) -> list[RetrievalCandidate]: ...

    def materialize(self, candidate: RetrievalCandidate) -> MaterializedDocument: ...


class RuntimeKnowledgeRetrievalAdapter:
    """复用旧 Knowledge Expert 的真实改写、召回和启发式重排能力。"""

    def __init__(
        self,
        db_manager: object,
        *,
        query_rewriter: Callable[
            ..., str
        ]
        | None,
        query_term_extractor: Callable[[str, str], list[str]] | None,
        candidate_scorer: Callable[
            [str, float, list[str], dict[str, Any] | None], float
        ]
        | None,
    ) -> None:
        self.db_manager = db_manager
        self._query_rewriter = query_rewriter
        self._query_term_extractor = query_term_extractor
        self._candidate_scorer = candidate_scorer
        self.query_rewrite_strategy = (
            QueryRewriteStrategy.EXISTING_MODEL
            if query_rewriter is not None
            else QueryRewriteStrategy.NONE
        )
        self.has_explicit_embedding = all(
            callable(getattr(db_manager, name, None))
            for name in ("embed_query", "search_by_vector_with_scores")
        )
        self.has_reranker = (
            query_term_extractor is not None and candidate_scorer is not None
        )
        self.has_keyword_retrieval = callable(
            getattr(db_manager, "keyword_search", None)
        )

    def rewrite_query(
        self,
        query: str,
        *,
        run_context: RunContext,
        event_emitter: StepEventEmitter | None,
        fault_controller: FaultInjectionController | None = None,
    ) -> str:
        if self._query_rewriter is None:
            return query
        try:
            parameters = inspect.signature(self._query_rewriter).parameters
            if (
                fault_controller is not None
                and (
                    "fault_controller" in parameters
                    or any(
                        item.kind is inspect.Parameter.VAR_KEYWORD
                        for item in parameters.values()
                    )
                )
            ):
                rewritten = self._query_rewriter(
                    query,
                    run_context,
                    event_emitter,
                    fault_controller=fault_controller,
                )
            else:
                rewritten = self._query_rewriter(
                    query,
                    run_context,
                    event_emitter,
                )
        except (
            BudgetExceededError,
            RunCancelledError,
            RunDeadlineExceededError,
            RetrievalAdapterError,
        ):
            raise
        except BaseException:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.INTERNAL,
                "QUERY_REWRITE_INTERNAL",
                "查询改写发生内部错误。",
            ) from None
        if not isinstance(rewritten, str) or not rewritten.strip():
            raise RetrievalAdapterError(
                RetrievalErrorCategory.QUERY_REWRITE_FAILED,
                "QUERY_REWRITE_EMPTY",
                "查询改写返回空结果。",
            )
        return rewritten.strip()

    def embed_query(self, query: str) -> QueryEmbedding:
        if not self.has_explicit_embedding:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.EMBEDDING_FAILED,
                "EMBEDDING_ADAPTER_MANAGED",
                "当前兼容 Adapter 由 Vector Store 内部执行 Embedding。",
            )
        try:
            vector = self.db_manager.embed_query(query)
            model_id = str(
                getattr(self.db_manager, "embedding_model_id", "huggingface-local")
            )
            return QueryEmbedding.create(query, vector, model_id)
        except RetrievalAdapterError:
            raise
        except BaseException:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.EMBEDDING_FAILED,
                "EMBEDDING_FAILED",
                "本地 Embedding 调用失败。",
            ) from None

    def retrieve(
        self,
        query: str,
        embedding: QueryEmbedding | None,
        invocation: RetrievalInvocation,
        *,
        max_candidates: int,
    ) -> list[RetrievalCandidate]:
        filters = thaw_json(invocation.filters)
        try:
            if embedding is not None and self.has_explicit_embedding:
                rows = self.db_manager.search_by_vector_with_scores(
                    list(embedding.vector),
                    k=max_candidates,
                    metadata_filter=filters or None,
                )
            else:
                rows = self.db_manager.search_with_scores(
                    query,
                    k=max_candidates,
                    metadata_filter=filters or None,
                )
        except BaseException:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.VECTOR_STORE_FAILED,
                "VECTOR_STORE_FAILED",
                "向量数据库查询未完成。",
            ) from None
        return self._to_candidates(
            rows,
            invocation,
            default_score=0.0,
            score_semantics=getattr(
                self.db_manager,
                "vector_score_semantics",
                VectorScoreSemantics.NORMALIZED_RELEVANCE,
            ),
        )

    def keyword_retrieve(
        self,
        terms: list[str],
        invocation: RetrievalInvocation,
        *,
        max_candidates: int,
    ) -> list[RetrievalCandidate]:
        keyword_search = getattr(self.db_manager, "keyword_search", None)
        # 旧 keyword_search 不接收 Metadata Filter；有 Filter 时宁可跳过补召回，
        # 也不能把不满足约束的候选混入最终 Context。
        if not callable(keyword_search) or not terms or invocation.filters:
            return []
        try:
            documents = keyword_search(terms, k=max_candidates)
        except BaseException:
            # 这是旧管线的补召回；失败不允许覆盖主向量查询结果。
            return []
        return self._to_candidates(
            [(document, 0.55) for document in documents],
            invocation,
            default_score=0.55,
            score_semantics=VectorScoreSemantics.NORMALIZED_RELEVANCE,
        )

    def should_keyword_retrieve(
        self,
        terms: list[str],
        invocation: RetrievalInvocation,
    ) -> bool:
        return bool(
            self.has_keyword_retrieval and terms and not invocation.filters
        )

    def rerank(
        self,
        rewritten_query: str,
        original_query: str,
        candidates: Sequence[RetrievalCandidate],
    ) -> list[RetrievalCandidate]:
        if not self.has_reranker:
            return list(candidates)
        try:
            terms = self._query_term_extractor(rewritten_query, original_query)
            scored = []
            for candidate in candidates:
                text = candidate.text or ""
                metadata = dict(thaw_json(candidate.metadata))
                score = self._candidate_scorer(
                    text, candidate.score, terms, metadata
                )
                if (
                    isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not math.isfinite(score)
                ):
                    raise ValueError("invalid rerank score")
                scored.append((min(1.0, max(0.0, float(score))), candidate))
            scored.sort(
                key=lambda item: (
                    -item[0],
                    item[1].original_rank,
                    item[1].candidate_id,
                )
            )
            return [
                replace(candidate, reranked_score=score, reranked_rank=index)
                for index, (score, candidate) in enumerate(scored, start=1)
            ]
        except BaseException:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.RERANK_FAILED,
                "RERANK_FAILED",
                "既有启发式重排未完成。",
            ) from None

    def materialize(self, candidate: RetrievalCandidate) -> MaterializedDocument:
        """当前 Chroma 已保存 Chunk 正文，因此此阶段是内容物化。"""
        if not candidate.text or not candidate.text.strip():
            raise RetrievalAdapterError(
                RetrievalErrorCategory.DOCUMENT_LOAD_FAILED,
                "DOCUMENT_CONTENT_EMPTY",
                "候选内容无法物化。",
            )
        return MaterializedDocument(
            candidate=candidate,
            text=candidate.text,
            original_content_hash=content_digest(candidate.text),
        )

    def _to_candidates(
        self,
        rows: Sequence[tuple[object, float]],
        invocation: RetrievalInvocation,
        *,
        default_score: float,
        score_semantics: VectorScoreSemantics,
    ) -> list[RetrievalCandidate]:
        collection = invocation.collection_names[0]
        candidates: list[RetrievalCandidate] = []
        for rank, row in enumerate(rows, start=1):
            document, provider_score = row
            text = str(getattr(document, "page_content", "") or "")
            metadata = dict(getattr(document, "metadata", {}) or {})
            score = (
                default_score
                if provider_score is None
                else normalize_vector_score(provider_score, score_semantics)
            )
            source = self._source_from_metadata(metadata, text, collection)
            candidate_id = hashlib.sha256(
                f"{source.source_id}|{source.chunk_id}".encode("utf-8")
            ).hexdigest()
            candidates.append(
                RetrievalCandidate(
                    candidate_id=candidate_id,
                    source=source,
                    score=score,
                    original_rank=rank,
                    metadata=metadata,
                    content_locator=f"chroma:{collection}:{source.chunk_id}",
                    text=text,
                )
            )
        return candidates

    @staticmethod
    def _source_from_metadata(
        metadata: dict[str, Any], text: str, collection: str
    ) -> SourceMetadata:
        canonical = str(metadata.get("source") or metadata.get("file_name") or "").strip()
        chunk_id = str(
            metadata.get("chunk_id")
            or metadata.get("id")
            or metadata.get("document_id")
            or ""
        ).strip()
        if not canonical or not chunk_id:
            raise RetrievalAdapterError(
                RetrievalErrorCategory.METADATA_INVALID,
                "METADATA_INVALID",
                "候选缺少可追溯的 Source 或 Chunk 标识。",
            )
        source_id = str(metadata.get("doc_id") or "").strip()
        if not source_id:
            source_id = hashlib.sha256(
                f"{collection}|{canonical}".encode("utf-8")
            ).hexdigest()
        version = str(
            metadata.get("file_hash")
            or metadata.get("document_version")
            or metadata.get("content_hash")
            or content_digest(text)
        )
        page_value = metadata.get("page_start", metadata.get("page"))
        try:
            page = int(page_value) if page_value not in (None, "") else None
        except (TypeError, ValueError):
            raise RetrievalAdapterError(
                RetrievalErrorCategory.METADATA_INVALID,
                "METADATA_PAGE_INVALID",
                "候选页码 Metadata 无效。",
            ) from None
        try:
            chunk_index = int(metadata.get("chunk_index", 0))
        except (TypeError, ValueError):
            raise RetrievalAdapterError(
                RetrievalErrorCategory.METADATA_INVALID,
                "METADATA_CHUNK_INDEX_INVALID",
                "候选 Chunk Index Metadata 无效。",
            ) from None
        display_name = str(
            metadata.get("file_name")
            or metadata.get("document_title")
            or canonical.rsplit("/", 1)[-1]
        )
        return SourceMetadata(
            source_id=source_id,
            source_type=str(metadata.get("source_type") or metadata.get("file_type") or "local"),
            collection=collection,
            canonical_uri=canonical,
            display_name=display_name,
            document_version=version,
            page=page,
            section_path=str(metadata.get("section_path") or ""),
            chunk_id=chunk_id,
            chunk_index=chunk_index,
        )


__all__ = [
    "QueryEmbedding",
    "RetrievalAdapter",
    "RetrievalAdapterError",
    "RuntimeKnowledgeRetrievalAdapter",
]
