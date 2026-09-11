"""RetrievalExecutionService 的生产 Cache-Aside 边界。"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from typing import Any, Callable

from core.redis_service import RagQueryCache
from core.runtime.budget import BudgetLedger, BudgetUsage, UsageSource
from core.runtime.cancellation import RunCancelledError
from core.runtime.context import RunDeadlineExceededError
from core.runtime.model_context import ContextTrustLevel
from core.runtime.retrieval_context import (
    RetrievalDeadlineExceededError,
    RetrievalExecutionContext,
)
from core.runtime.retrieval_contract import (
    CitationBinding,
    RetrievalBudgetUsage,
    RetrievalErrorCategory,
    RetrievalExecutionError,
    RetrievalExecutionResult,
    RetrievalExecutionStatus,
    RetrievalInvocation,
    RetrievalProvenance,
    RetrievalStage,
    RetrievalStageRecord,
    RetrievalStageStatus,
    RetrievalTransformation,
    RetrievedChunk,
    SourceMetadata,
    thaw_json,
)
from core.runtime.retrieval_evaluation import current_retrieval_evaluation_collector
from core.runtime.tracing import (
    NoopSpanRecorder,
    current_span_recorder,
    install_span_recorder,
    install_trace_context,
    reset_span_recorder,
    reset_trace_context,
    start_span_safely,
)

logger = logging.getLogger(__name__)


class SyncRedisBridge:
    """仅供 Runtime worker 线程同步等待 application-loop Redis I/O。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, *, timeout_seconds: float) -> None:
        if loop.is_closed():
            raise ValueError("不能绑定已关闭的 event loop")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须为正数")
        self._loop = loop
        self._loop_thread_id = getattr(loop, "_thread_id", None)
        self._timeout_seconds = float(timeout_seconds)

    def run(
        self,
        coroutine_factory: Callable[[], Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        if self._loop.is_closed():
            raise RuntimeError("Redis event loop unavailable")
        if self._loop_thread_id == threading.get_ident():
            raise RuntimeError("Redis bridge cannot block its event-loop thread")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Redis bridge requires a non-event-loop worker thread")
        coroutine = coroutine_factory()
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except Exception:
            coroutine.close()
            raise
        timeout = (
            self._timeout_seconds
            if timeout_seconds is None
            else min(self._timeout_seconds, float(timeout_seconds))
        )
        if timeout <= 0:
            future.cancel()
            raise TimeoutError("Redis bridge timed out")
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            raise TimeoutError("Redis bridge timed out") from None


class RetrievalCacheCodec:
    """拥有 Result 与版本化缓存 Projection 之间的唯一转换。"""

    schema_version = "cached-retrieval-result.v1"

    def encode(self, result: RetrievalExecutionResult) -> dict[str, Any] | None:
        # DEGRADED 往往包含一次执行中的部分读取故障，不属于可跨请求复用的
        # deterministic success；EMPTY/失败结果同样不做 negative cache。
        if result.status is not RetrievalExecutionStatus.SUCCEEDED:
            return None
        return {
            "schema_version": self.schema_version,
            "status": result.status.value,
            "rewritten_query_digest": result.rewritten_query_digest,
            "degradation_reasons": list(result.degradation_reasons),
            "chunks": [self._encode_chunk(chunk) for chunk in result.final_chunks],
        }

    def decode(self, payload: dict[str, Any], *, retrieval_id: str) -> tuple[
        RetrievalExecutionStatus, str, tuple[str, ...], tuple[RetrievedChunk, ...]
    ]:
        if payload.get("schema_version") != self.schema_version:
            raise ValueError("unsupported cached retrieval projection")
        status = RetrievalExecutionStatus(str(payload["status"]))
        if status is not RetrievalExecutionStatus.SUCCEEDED:
            raise ValueError("non-cacheable retrieval status")
        rewritten_digest = str(payload["rewritten_query_digest"])
        if not rewritten_digest:
            raise ValueError("missing rewritten query digest")
        raw_reasons = payload.get("degradation_reasons", [])
        raw_chunks = payload.get("chunks")
        if not isinstance(raw_reasons, list) or not isinstance(raw_chunks, list) or not raw_chunks:
            raise ValueError("malformed cached retrieval projection")
        reasons = tuple(str(item) for item in raw_reasons)
        if (status is RetrievalExecutionStatus.DEGRADED) != bool(reasons):
            raise ValueError("cached degradation contract mismatch")
        chunks = tuple(
            self._decode_chunk(item, retrieval_id=retrieval_id, index=index)
            for index, item in enumerate(raw_chunks, start=1)
        )
        return status, rewritten_digest, reasons, chunks

    @staticmethod
    def _encode_chunk(chunk: RetrievedChunk) -> dict[str, Any]:
        source = chunk.source
        provenance = chunk.provenance
        return {
            "text": chunk.text,
            "score": chunk.score,
            "source": {
                "source_id": source.source_id,
                "source_type": source.source_type,
                "collection": source.collection,
                "canonical_uri": source.canonical_uri,
                "display_name": source.display_name,
                "document_version": source.document_version,
                "page": source.page,
                "section_path": source.section_path,
                "chunk_id": source.chunk_id,
                "chunk_index": source.chunk_index,
            },
            "provenance": {
                "original_rank": provenance.original_rank,
                "reranked_rank": provenance.reranked_rank,
                "retrieval_score": provenance.retrieval_score,
                "transformations": [item.value for item in provenance.transformations],
                "original_content_hash": provenance.original_content_hash,
                "context_content_hash": provenance.context_content_hash,
            },
            "citation": {
                "display_label": chunk.citation.display_label,
                "page": chunk.citation.page,
                "section_path": chunk.citation.section_path,
            },
        }

    @staticmethod
    def _decode_chunk(payload: Any, *, retrieval_id: str, index: int) -> RetrievedChunk:
        if not isinstance(payload, dict):
            raise ValueError("cached chunk must be an object")
        source_payload = payload.get("source")
        provenance_payload = payload.get("provenance")
        citation_payload = payload.get("citation")
        if not all(isinstance(item, dict) for item in (source_payload, provenance_payload, citation_payload)):
            raise ValueError("cached chunk fields must be objects")
        source = SourceMetadata(**source_payload)
        block_id = f"context-{index}"
        citation = CitationBinding(
            citation_id=f"R{retrieval_id[:8]}-{index}",
            source_id=source.source_id,
            chunk_id=source.chunk_id,
            context_block_id=block_id,
            context_content_hash=str(provenance_payload["context_content_hash"]),
            display_label=str(citation_payload["display_label"]),
            page=citation_payload.get("page"),
            section_path=str(citation_payload.get("section_path", "")),
        )
        provenance = RetrievalProvenance(
            source_id=source.source_id,
            chunk_id=source.chunk_id,
            original_rank=int(provenance_payload["original_rank"]),
            reranked_rank=(
                int(provenance_payload["reranked_rank"])
                if provenance_payload.get("reranked_rank") is not None
                else None
            ),
            retrieval_score=float(provenance_payload["retrieval_score"]),
            transformations=tuple(
                RetrievalTransformation(str(item))
                for item in provenance_payload["transformations"]
            ),
            original_content_hash=str(provenance_payload["original_content_hash"]),
            context_content_hash=str(provenance_payload["context_content_hash"]),
        )
        return RetrievedChunk(
            context_block_id=block_id,
            text=str(payload["text"]),
            source=source,
            provenance=provenance,
            citation=citation,
            trust_level=ContextTrustLevel.UNTRUSTED_EXTERNAL,
            score=float(payload["score"]),
        )


class CachedRetrievalExecutionService:
    """不改变 Origin Authority 的同步生产 Retrieval cache-aside facade。"""

    def __init__(
        self,
        origin,
        cache: RagQueryCache,
        bridge: SyncRedisBridge,
        *,
        index_generation_provider: Callable[[], str | None],
        codec: RetrievalCacheCodec | None = None,
        observability: Any = None,
    ) -> None:
        self.origin = origin
        self.cache = cache
        self.bridge = bridge
        self.index_generation_provider = index_generation_provider
        self.codec = codec or RetrievalCacheCodec()
        self.observability = observability

    def __getattr__(self, name: str) -> Any:
        return getattr(self.origin, name)

    def execute(self, invocation: RetrievalInvocation, **kwargs) -> RetrievalExecutionResult:
        run_context = kwargs["run_context"]
        authz_domain = run_context.retrieval_cache_authz_domain
        try:
            index_generation = self.index_generation_provider()
        except Exception:
            index_generation = None
        if not authz_domain or not index_generation:
            self._log("bypass")
            return self.origin.execute(invocation, **kwargs)
        try:
            run_context.raise_if_inactive()
        except (RunCancelledError, RunDeadlineExceededError):
            return self.origin.execute(invocation, **kwargs)
        policy = self._policy_identity(invocation)
        key = self.cache.key(
            authz_domain=authz_domain,
            index_generation=index_generation,
            policy=policy,
            query=invocation.original_query,
        )
        run_remaining = run_context.remaining_seconds()
        lookup_timeout = float(invocation.requested_timeout_seconds)
        if run_remaining is not None:
            lookup_timeout = min(lookup_timeout, float(run_remaining))
        try:
            payload = self.bridge.run(
                lambda: self.cache.get(key),
                timeout_seconds=max(1e-6, lookup_timeout),
            )
        except Exception:
            self._log("error")
            return self.origin.execute(invocation, **kwargs)
        if payload is not None:
            try:
                decoded = self.codec.decode(payload, retrieval_id=invocation.retrieval_id)
                self._log("hit")
                return self._execute_hit(invocation, decoded, **kwargs)
            except (KeyError, TypeError, ValueError):
                self._log("miss")
        else:
            self._log("miss")
        result = self.origin.execute(invocation, **kwargs)
        projection = self.codec.encode(result)
        remaining = run_context.remaining_seconds()
        if projection is not None and (remaining is None or remaining > 0):
            try:
                self.bridge.run(
                    lambda: self.cache.set(key, projection),
                    timeout_seconds=remaining,
                )
            except Exception:
                self._log("error")
        return result

    def _policy_identity(self, invocation: RetrievalInvocation) -> dict[str, Any]:
        adapter = self.origin.adapter
        spec = self.origin.spec
        rrf = getattr(adapter, "_rrf", None)
        profile = getattr(rrf, "profile", None)
        return {
            "schema_version": "retrieval-cache-policy.v1",
            "strategy": getattr(adapter, "retrieval_strategy", "BASELINE"),
            "hybrid_rrf": bool(getattr(adapter, "hybrid_rrf", False)),
            "hybrid_profile": profile.to_dict() if profile is not None else None,
            "rrf_k": getattr(rrf, "rrf_k", None),
            "provenance_sha256": getattr(adapter, "hybrid_provenance_sha256", None),
            "collection_names": list(invocation.collection_names),
            "top_k": invocation.top_k,
            "rerank_top_k": invocation.rerank_top_k,
            "filters": thaw_json(invocation.filters),
            "minimum_score": self.origin.minimum_score,
            "query_rewrite_strategy": adapter.query_rewrite_strategy.value,
            "has_explicit_embedding": bool(
                getattr(adapter, "has_explicit_embedding", False)
            ),
            "has_reranker": bool(getattr(adapter, "has_reranker", False)),
            "has_keyword_retrieval": bool(
                getattr(adapter, "has_keyword_retrieval", False)
            ),
            "max_candidates": spec.max_candidates,
            "max_context_chunks": spec.max_context_chunks,
            "max_context_chars": spec.max_context_chars,
            "max_single_chunk_chars": spec.max_single_chunk_chars,
            "max_document_reads": spec.max_document_reads,
            "allow_partial_document_load": spec.allow_partial_document_load,
        }

    def _execute_hit(self, invocation: RetrievalInvocation, decoded, **kwargs) -> RetrievalExecutionResult:
        run_context = kwargs["run_context"]
        step_id = kwargs.get("step_id", "retrieval")
        collector = current_retrieval_evaluation_collector()
        capture_builder = None
        if collector is not None:
            try:
                capture_builder = collector.begin(
                    run_id=run_context.run_id,
                    invocation=invocation,
                    max_context_chars=self.origin.spec.max_context_chars,
                )
            except Exception:
                collector = None
        recorder = current_span_recorder() or self.origin.span_recorder or NoopSpanRecorder()
        handle = start_span_safely(
            recorder,
            trace_id=run_context.trace_id,
            run_id=run_context.run_id,
            component="retrieval",
            operation="execute",
            step_id=step_id,
        )
        trace_token = install_trace_context(handle.context)
        recorder_token = install_span_recorder(recorder)
        activity_tracker = run_context.activity_tracker
        if activity_tracker is not None:
            activity_tracker.increment("retrievals_active")
        try:
            result = self._execute_hit_impl(invocation, decoded, **kwargs)
            if collector is not None and capture_builder is not None:
                try:
                    collector.complete(capture_builder, result)
                except Exception:
                    try:
                        collector.record_failure("RAG_EVALUATION_COLLECTOR_COMPLETE_FAILED")
                    except Exception:
                        pass
            if handle.context is not None:
                handle.set_safe_attribute("output_count", len(result.final_chunks))
                handle.set_safe_attribute("citation_count", len(result.citations))
                handle.set_safe_attribute("degraded", result.degraded)
                handle.set_safe_attribute("cache_hit", True)
            if kwargs.get("defer_completed_event", False):
                with self.origin._trace_lock:
                    self.origin._deferred_retrieval_spans[result.retrieval_id] = handle
            else:
                self.origin._end_retrieval_span(handle, result)
            return result
        except RunCancelledError:
            handle.end_cancelled("RUN_CANCELLED")
            raise
        except (RunDeadlineExceededError, RetrievalDeadlineExceededError, TimeoutError):
            handle.end_timed_out()
            raise
        except BaseException:
            handle.end_error()
            raise
        finally:
            if activity_tracker is not None:
                activity_tracker.decrement("retrievals_active")
            reset_trace_context(trace_token)
            reset_span_recorder(recorder_token)

    def _execute_hit_impl(self, invocation: RetrievalInvocation, decoded, **kwargs) -> RetrievalExecutionResult:
        status, rewritten_digest, reasons, chunks = decoded
        run_context = kwargs["run_context"]
        event_emitter = kwargs.get("event_emitter")
        defer_completed = bool(kwargs.get("defer_completed_event", False))
        step_id = kwargs.get("step_id", "retrieval")
        ledger = run_context.budget_ledger
        if not isinstance(ledger, BudgetLedger):
            return self.origin.execute(invocation, **kwargs)
        try:
            run_context.raise_if_inactive()
        except (RunCancelledError, RunDeadlineExceededError):
            return self.origin.execute(invocation, **kwargs)
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        context = RetrievalExecutionContext.create(
            run_context=run_context,
            step_id=step_id,
            budget_ledger=ledger,
            event_emitter=event_emitter,
            spec=self.origin.spec,
            requested_timeout_seconds=invocation.requested_timeout_seconds,
            fault_controller=kwargs.get("fault_controller"),
        )
        try:
            reservation = ledger.reserve(
                BudgetUsage(retrieval_calls=1),
                reservation_type="retrieval_cache_hit",
                step_id=step_id,
            )
            ledger.commit(
                reservation,
                BudgetUsage(retrieval_calls=1),
                usage_source=UsageSource.ACTUAL,
            )
        except Exception:
            return self.origin.execute(invocation, **kwargs)
        usage = RetrievalBudgetUsage(retrieval_calls=1)
        try:
            self.origin._emit_started(context, invocation)
        except BaseException as exc:
            return self._event_failure_result(
                invocation,
                started_at=started_at,
                started_monotonic=started_monotonic,
                records=(),
                usage=usage,
                safe_error_code=getattr(
                    exc, "safe_error_code", "RETRIEVAL_EVENT_EMISSION_FAILED"
                ),
            )
        now = datetime.now(UTC)
        lookup_record = RetrievalStageRecord(
            stage=RetrievalStage.RETRIEVE,
            status=RetrievalStageStatus.SUCCEEDED,
            started_at=started_at,
            completed_at=now,
            duration_ms=max(0, int((time.monotonic() - started_monotonic) * 1000)),
            input_count=1,
            output_count=len(chunks),
            budget_usage=usage,
        )
        context_record = RetrievalStageRecord(
            stage=RetrievalStage.CONTEXT_BUILD,
            status=RetrievalStageStatus.SUCCEEDED,
            started_at=now,
            completed_at=now,
            duration_ms=0,
            input_count=len(chunks),
            output_count=len(chunks),
        )
        try:
            self.origin.emit_stage_event(lookup_record, event_emitter=event_emitter)
        except BaseException as exc:
            return self._event_failure_result(
                invocation,
                started_at=started_at,
                started_monotonic=started_monotonic,
                records=(lookup_record,),
                usage=usage,
                safe_error_code=getattr(
                    exc, "safe_error_code", "RETRIEVAL_EVENT_EMISSION_FAILED"
                ),
            )
        result = RetrievalExecutionResult(
            retrieval_id=invocation.retrieval_id,
            status=status,
            rewritten_query_digest=rewritten_digest,
            final_chunks=chunks,
            citations=tuple(chunk.citation for chunk in chunks),
            stage_records=(lookup_record, context_record),
            degraded=bool(reasons),
            degradation_reasons=reasons,
            budget_usage=usage,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            duration_ms=max(0, int((time.monotonic() - started_monotonic) * 1000)),
        )
        if not defer_completed:
            try:
                self.origin.emit_stage_event(context_record, event_emitter=event_emitter)
                self.origin.emit_completed_event(result, event_emitter=event_emitter)
            except BaseException as exc:
                return self._event_failure_result(
                    invocation,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    records=(lookup_record, context_record),
                    usage=usage,
                    safe_error_code=getattr(
                        exc, "safe_error_code", "RETRIEVAL_EVENT_EMISSION_FAILED"
                    ),
                )
        return result

    @staticmethod
    def _event_failure_result(
        invocation: RetrievalInvocation,
        *,
        started_at: datetime,
        started_monotonic: float,
        records: tuple[RetrievalStageRecord, ...],
        usage: RetrievalBudgetUsage,
        safe_error_code: str,
    ) -> RetrievalExecutionResult:
        completed_at = datetime.now(UTC)
        return RetrievalExecutionResult(
            retrieval_id=invocation.retrieval_id,
            status=RetrievalExecutionStatus.FAILED,
            rewritten_query_digest=invocation.query_digest,
            final_chunks=(),
            citations=(),
            stage_records=records,
            degraded=False,
            degradation_reasons=(),
            budget_usage=usage,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=max(0, int((time.monotonic() - started_monotonic) * 1000)),
            error=RetrievalExecutionError(
                RetrievalErrorCategory.INTERNAL,
                safe_error_code,
                "Retrieval Runtime Event 发布失败；已执行阶段不会重跑。",
                records[-1].stage if records else None,
            ),
        )

    def _log(self, outcome: str) -> None:
        try:
            if self.observability is not None and outcome in {"bypass", "error"}:
                self.observability.observe_cache(outcome)
        except Exception:
            pass
        logger.info(
            "RAG cache outcome",
            extra={"component": "retrieval_cache", "cache_outcome": outcome},
        )


__all__ = [
    "CachedRetrievalExecutionService",
    "RetrievalCacheCodec",
    "SyncRedisBridge",
]
