#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段化 Retrieval Pipeline，统一失败、预算、取消、超时和降级语义。"""

from __future__ import annotations

import concurrent.futures
import hashlib
import inspect
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Callable, Sequence, TypeVar

from core.runtime.budget import (
    BudgetExceededError,
    BudgetLedger,
    BudgetUsage,
    UsageSource,
)
from core.runtime.blocking_executor import (
    DEFAULT_BLOCKING_MAX_PENDING_TASKS,
    DEFAULT_BLOCKING_MAX_WORKERS,
    BlockingExecutorAdmissionTimeout,
    BlockingTaskHandle,
    BlockingTaskKind,
    BlockingTaskWaitState,
    BoundedBlockingExecutor,
    process_blocking_executor,
)
from core.runtime.cancellation import RunCancelledError
from core.runtime.context import RunContext, RunDeadlineExceededError
from core.runtime.event_emitter import StepEventEmitter
from core.runtime.event_journal import JournalError
from core.runtime.fault_injection import FaultInjectionController
from core.runtime.fault_injection_contract import (
    FaultAction,
    FaultMatchContext,
    FaultPoint,
    InjectedFailureResult,
    InjectedFaultCode,
    InjectedFaultError,
)
from core.runtime.retrieval_adapters import (
    QueryEmbedding,
    RetrievalAdapter,
    RetrievalAdapterError,
)
from core.runtime.retrieval_context import (
    RetrievalDeadlineExceededError,
    RetrievalExecutionContext,
)
from core.runtime.retrieval_contract import (
    CitationBinding,
    MaterializedDocument,
    QueryRewriteStrategy,
    RetrievalBudgetUsage,
    RetrievalCandidate,
    RetrievalErrorCategory,
    RetrievalExecutionError,
    RetrievalExecutionResult,
    RetrievalExecutionSpec,
    RetrievalExecutionStatus,
    RetrievalInvocation,
    RetrievalProvenance,
    RetrievalStage,
    RetrievalStageRecord,
    RetrievalStageStatus,
    RetrievalTransformation,
    RetrievedChunk,
    content_digest,
    query_digest,
)
from core.runtime.retrieval_evaluation import (
    RetrievalEvaluationCaptureBuilder,
    RetrievalEvaluationChannel,
    current_retrieval_evaluation_collector,
)
from core.runtime.tracing import (
    NoopSpanRecorder,
    current_span_recorder,
    install_span_recorder,
    install_trace_context,
    reset_span_recorder,
    reset_trace_context,
    start_span_safely,
)


T = TypeVar("T")

# Retrieval strategy wire values（bounded、低基数；WP2 冻结事件/label 语义）。
BASELINE_RETRIEVAL_STRATEGY_VALUE = "BASELINE"
HYBRID_RETRIEVAL_STRATEGY_VALUE = "HYBRID_RRF"


class _StageTimedOut(TimeoutError):
    def __init__(
        self,
        wait_state: BlockingTaskWaitState | None = None,
    ) -> None:
        state = wait_state or BlockingTaskWaitState(True, False, False)
        self.worker_terminated = state.worker_terminated
        self.execution_detached = state.execution_detached
        self.background_work_pending = state.background_work_pending
        super().__init__("retrieval stage timed out")


class _EventEmissionFailed(RuntimeError):
    def __init__(self, safe_error_code: str = "RETRIEVAL_EVENT_EMISSION_FAILED"):
        self.safe_error_code = safe_error_code
        super().__init__("Retrieval Runtime Event 发布失败")


class RetrievalExecutionService:
    """执行真实 Retrieval Adapter；一个服务实例可跨 Run 复用。"""

    def __init__(
        self,
        adapter: RetrievalAdapter,
        *,
        spec: RetrievalExecutionSpec | None = None,
        minimum_score: float = 0.0,
        blocking_executor: BoundedBlockingExecutor | None = None,
        max_sync_workers: int | None = None,
        max_pending_tasks: int | None = None,
        span_recorder=None,
    ) -> None:
        if (
            isinstance(minimum_score, bool)
            or not isinstance(minimum_score, (int, float))
            or not 0.0 <= float(minimum_score) <= 1.0
        ):
            raise ValueError("minimum_score 必须在 0 到 1 之间")
        if max_sync_workers is not None and (
            isinstance(max_sync_workers, bool)
            or not isinstance(max_sync_workers, int)
            or max_sync_workers <= 0
        ):
            raise ValueError("max_sync_workers 必须是正整数或 None")
        if max_pending_tasks is not None and (
            isinstance(max_pending_tasks, bool)
            or not isinstance(max_pending_tasks, int)
            or max_pending_tasks < 0
        ):
            raise ValueError("max_pending_tasks 必须是非负整数或 None")
        if blocking_executor is not None and (
            max_sync_workers is not None or max_pending_tasks is not None
        ):
            raise ValueError("注入 blocking_executor 时不得重复配置容量")
        self.adapter = adapter
        self.spec = spec or RetrievalExecutionSpec()
        self.minimum_score = float(minimum_score)
        if blocking_executor is not None:
            self.blocking_executor = blocking_executor
        elif max_sync_workers is not None or max_pending_tasks is not None:
            self.blocking_executor = BoundedBlockingExecutor(
                max_workers=(
                    max_sync_workers
                    if max_sync_workers is not None
                    else DEFAULT_BLOCKING_MAX_WORKERS
                ),
                max_pending_tasks=(
                    max_pending_tasks
                    if max_pending_tasks is not None
                    else DEFAULT_BLOCKING_MAX_PENDING_TASKS
                ),
                thread_name_prefix="retrieval-stage",
            )
        else:
            self.blocking_executor = process_blocking_executor
        self.span_recorder = span_recorder
        self._trace_lock = threading.Lock()
        self._deferred_retrieval_spans: dict[str, object] = {}
        self._deferred_stage_contexts: dict[int, object] = {}
        # WP2：deferred 完成事件所需的 per-retrieval Hybrid fusion 事实
        # （call-local 结果的 application 级暂存；retrieval_id 唯一）。
        self._deferred_hybrid_fusion: dict[str, object] = {}

    def execute(
        self,
        invocation: RetrievalInvocation,
        *,
        run_context: RunContext,
        step_id: str = "retrieval",
        event_emitter: StepEventEmitter | None = None,
        defer_completed_event: bool = False,
        fault_controller: FaultInjectionController | None = None,
    ) -> RetrievalExecutionResult:
        collector = current_retrieval_evaluation_collector()
        capture_builder: RetrievalEvaluationCaptureBuilder | None = None
        if collector is not None:
            try:
                capture_builder = collector.begin(
                    run_id=run_context.run_id,
                    invocation=invocation,
                    max_context_chars=self.spec.max_context_chars,
                )
            except Exception:  # noqa: BLE001 - evaluation sidecar 必须 failure-isolated
                try:
                    collector.record_failure("RAG_EVALUATION_COLLECTOR_BEGIN_FAILED")
                except Exception:  # noqa: BLE001 - collector 自身不得改变 Runtime
                    collector = None
        recorder = current_span_recorder() or self.span_recorder or NoopSpanRecorder()
        handle = start_span_safely(
            recorder,
            trace_id=run_context.trace_id,
            run_id=run_context.run_id,
            component="retrieval",
            operation="execute",
            step_id=step_id,
        )
        token = install_trace_context(handle.context)
        recorder_token = install_span_recorder(recorder)
        activity_tracker = run_context.activity_tracker
        if activity_tracker is not None:
            activity_tracker.increment("retrievals_active")
        try:
            result = self._execute_impl(
                invocation,
                run_context=run_context,
                step_id=step_id,
                event_emitter=event_emitter,
                defer_completed_event=defer_completed_event,
                fault_controller=fault_controller,
                evaluation_capture=capture_builder,
            )
            if collector is not None and capture_builder is not None:
                try:
                    collector.complete(capture_builder, result)
                except Exception:  # noqa: BLE001 - evaluation sidecar 必须 failure-isolated
                    try:
                        collector.record_failure("RAG_EVALUATION_COLLECTOR_COMPLETE_FAILED")
                    except Exception:  # noqa: BLE001 - collector 自身不得改变 Runtime
                        collector = None
            if handle.context is not None:
                handle.set_safe_attribute("output_count", len(result.final_chunks))
                handle.set_safe_attribute("citation_count", len(result.citations))
                handle.set_safe_attribute("degraded", result.degraded)
            if defer_completed_event:
                with self._trace_lock:
                    self._deferred_retrieval_spans[result.retrieval_id] = handle
            else:
                self._end_retrieval_span(handle, result)
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
            reset_trace_context(token)
            reset_span_recorder(recorder_token)

    def _execute_impl(
        self,
        invocation: RetrievalInvocation,
        *,
        run_context: RunContext,
        step_id: str = "retrieval",
        event_emitter: StepEventEmitter | None = None,
        defer_completed_event: bool = False,
        fault_controller: FaultInjectionController | None = None,
        evaluation_capture: RetrievalEvaluationCaptureBuilder | None = None,
    ) -> RetrievalExecutionResult:
        """返回类型化 Result；业务失败不会伪装成合法 EMPTY。"""
        if not isinstance(defer_completed_event, bool):
            raise TypeError("defer_completed_event 必须是 bool")
        emit_completed_event = not defer_completed_event
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        records: list[RetrievalStageRecord] = []
        degradation_reasons: list[str] = []
        usage = RetrievalBudgetUsage()
        rewritten_query = invocation.original_query
        ledger = run_context.budget_ledger
        if not isinstance(ledger, BudgetLedger):
            return self._terminal_result(
                invocation,
                started_at,
                started_monotonic,
                records,
                usage,
                degradation_reasons,
                RetrievalExecutionStatus.FAILED,
                RetrievalExecutionError(
                    RetrievalErrorCategory.INTERNAL,
                    "RETRIEVAL_BUDGET_LEDGER_MISSING",
                    "Retrieval Runtime 需要 RunContext 绑定 BudgetLedger。",
                ),
                rewritten_query,
                emit_completed=emit_completed_event,
                event_emitter=event_emitter,
            )
        retrieval_usage_baseline = ledger.snapshot().committed_usage
        context = RetrievalExecutionContext.create(
            run_context=run_context,
            step_id=step_id,
            budget_ledger=ledger,
            event_emitter=event_emitter,
            spec=self.spec,
            requested_timeout_seconds=invocation.requested_timeout_seconds,
            fault_controller=fault_controller,
        )

        try:
            context.raise_if_cancelled()
            self._charge_started_call(
                context,
                BudgetUsage(retrieval_calls=1),
                "retrieval_call",
            )
            usage = usage.plus(RetrievalBudgetUsage(retrieval_calls=1))
            self._emit_started(context, invocation)

            # Query Rewrite 是既有模型能力；没有实现时必须明确 SKIPPED。
            if self.adapter.query_rewrite_strategy == QueryRewriteStrategy.NONE:
                self._record_skipped(
                    context,
                    records,
                    RetrievalStage.QUERY_REWRITE,
                    input_count=1,
                    output_count=1,
                )
            else:
                try:
                    rewritten_query = self._run_stage(
                        context,
                        records,
                        RetrievalStage.QUERY_REWRITE,
                        input_count=1,
                        budget_usage=RetrievalBudgetUsage(),
                        operation=lambda timeout: self._invoke_unbudgeted(
                            context,
                            lambda: self._rewrite_query(
                                context,
                                invocation.original_query,
                            ),
                            timeout,
                            BlockingTaskKind.QUERY_REWRITE,
                        ),
                        degradable=True,
                    )
                except RetrievalAdapterError as exc:
                    if exc.category != RetrievalErrorCategory.QUERY_REWRITE_FAILED:
                        raise
                    rewritten_query = invocation.original_query
                    degradation_reasons.append(exc.safe_error_code)
                    self._mark_last_record_degraded(records)

            if evaluation_capture is not None and evaluation_capture.rewrite_fixture is not None:
                rewritten_query = evaluation_capture.replay_rewritten_query(invocation.original_query)

            if evaluation_capture is not None:
                evaluation_capture.capture_rewritten_query(rewritten_query)

            context.raise_if_cancelled()
            queries = tuple(
                dict.fromkeys((rewritten_query, invocation.original_query))
            )
            embeddings: dict[str, QueryEmbedding | None] = {
                query: None for query in queries
            }

            if self.adapter.has_explicit_embedding:
                embedding_usage = RetrievalBudgetUsage(
                    embedding_calls=len(queries)
                )

                def embed_all(stage_timeout: float) -> dict[str, QueryEmbedding]:
                    stage_deadline = time.monotonic() + stage_timeout
                    result: dict[str, QueryEmbedding] = {}
                    for query in queries:
                        context.raise_if_cancelled()
                        timeout = min(
                            max(0.0, stage_deadline - time.monotonic()),
                            context.remaining_seconds(),
                        )
                        result[query] = self._invoke_budgeted(
                            context,
                            BudgetUsage(embedding_calls=1),
                            "retrieval_embedding",
                            lambda query=query: self.adapter.embed_query(query),
                            timeout,
                            BlockingTaskKind.EMBEDDING,
                        )
                    return result

                embeddings.update(
                    self._run_stage(
                        context,
                        records,
                        RetrievalStage.EMBEDDING,
                        input_count=len(queries),
                        budget_usage=embedding_usage,
                        operation=embed_all,
                    )
                )
                usage = usage.plus(embedding_usage)
            else:
                # 仅用于尚未暴露显式 Embedding API 的兼容/测试 Adapter。
                self._record_skipped(
                    context,
                    records,
                    RetrievalStage.EMBEDDING,
                    input_count=len(queries),
                    output_count=0,
                    safe_error_code="EMBEDDING_ADAPTER_MANAGED",
                )

            def retrieve_all(stage_timeout: float) -> list[RetrievalCandidate]:
                stage_deadline = time.monotonic() + stage_timeout
                combined: list[RetrievalCandidate] = []
                for query in queries:
                    context.raise_if_cancelled()
                    timeout = min(
                        max(0.0, stage_deadline - time.monotonic()),
                        context.remaining_seconds(),
                    )
                    vector_candidates = self._invoke_budgeted(
                        context,
                        BudgetUsage(vector_queries=1),
                        "retrieval_vector_query",
                        lambda query=query: self.adapter.retrieve(
                            query,
                            embeddings[query],
                            invocation,
                            max_candidates=self.spec.max_candidates,
                        ),
                        timeout,
                        BlockingTaskKind.VECTOR_QUERY,
                    )
                    if evaluation_capture is not None:
                        if len(queries) == 1:
                            channel = RetrievalEvaluationChannel.VECTOR_ORIGINAL_AND_REWRITTEN
                        elif query == rewritten_query:
                            channel = RetrievalEvaluationChannel.VECTOR_REWRITTEN_QUERY
                        else:
                            channel = RetrievalEvaluationChannel.VECTOR_ORIGINAL_QUERY
                        evaluation_capture.observe_candidates(vector_candidates, channel)
                    combined.extend(vector_candidates)
                terms = self._extract_terms(rewritten_query, invocation.original_query)
                should_keyword = self._should_keyword_retrieve(
                    terms, invocation
                )
                if should_keyword:
                    keyword_candidates = self._invoke_budgeted(
                        context,
                        BudgetUsage(keyword_queries=1),
                        "retrieval_keyword_query",
                        lambda: self.adapter.keyword_retrieve(
                            terms,
                            invocation,
                            max_candidates=self.spec.max_candidates,
                        ),
                        min(
                            max(0.0, stage_deadline - time.monotonic()),
                            context.remaining_seconds(),
                        ),
                        BlockingTaskKind.KEYWORD_QUERY,
                    )
                    if evaluation_capture is not None:
                        evaluation_capture.observe_candidates(
                            keyword_candidates,
                            RetrievalEvaluationChannel.KEYWORD,
                        )
                    combined.extend(keyword_candidates)
                return self._merge_candidates(combined, self.spec.max_candidates)

            terms = self._extract_terms(rewritten_query, invocation.original_query)
            keyword_query_count = int(
                self._should_keyword_retrieve(terms, invocation)
            )
            vector_usage = RetrievalBudgetUsage(
                vector_queries=len(queries),
                keyword_queries=keyword_query_count,
            )
            candidates = self._run_stage(
                context,
                records,
                RetrievalStage.RETRIEVE,
                input_count=len(queries),
                budget_usage=vector_usage,
                operation=retrieve_all,
            )
            usage = usage.plus(vector_usage)
            if evaluation_capture is not None:
                evaluation_capture.capture_retrieved(candidates)
            if not candidates:
                if getattr(self.adapter, "hybrid_rrf", False):
                    raise RetrievalAdapterError(
                        RetrievalErrorCategory.FUSION_FAILED,
                        "HYBRID_DENSE_CHANNEL_EMPTY",
                        "Hybrid Dense channel is empty。",
                    )
                if evaluation_capture is not None:
                    evaluation_capture.capture_ranked(candidates, reranked=False)
                usage = self._retrieval_usage_since(
                    ledger, retrieval_usage_baseline
                )
                self._skip_after(
                    context, records, RetrievalStage.RETRIEVE
                )
                return self._successful_result(
                    invocation,
                    started_at,
                    started_monotonic,
                    records,
                    usage,
                    degradation_reasons,
                    rewritten_query,
                    (),
                    emit_completed=emit_completed_event,
                    event_emitter=event_emitter,
                )

            reranked = False
            hybrid_fusion = None
            is_hybrid = bool(getattr(self.adapter, "hybrid_rrf", False))
            if self.adapter.has_reranker:
                try:
                    if is_hybrid:
                        def hybrid_rerank_operation(timeout: float):
                            return self._invoke_unbudgeted(
                                context,
                                lambda: self._execute_hybrid_fusion(
                                    context, rewritten_query, candidates
                                ),
                                timeout,
                                BlockingTaskKind.RERANK,
                            )

                        hybrid_fusion = self._run_stage(
                            context,
                            records,
                            RetrievalStage.RERANK,
                            input_count=len(candidates),
                            budget_usage=RetrievalBudgetUsage(),
                            operation=hybrid_rerank_operation,
                            output_count_getter=lambda value: len(
                                value.candidates
                            ),
                        )
                        self._validate_rerank(
                            candidates, hybrid_fusion.candidates
                        )
                        candidates = hybrid_fusion.candidates
                        reranked = True
                        with self._trace_lock:
                            self._deferred_hybrid_fusion[
                                invocation.retrieval_id
                            ] = hybrid_fusion
                    else:
                        def rerank_candidates(timeout: float) -> list[RetrievalCandidate]:
                            ranked = self._invoke_unbudgeted(
                                context,
                                lambda: self.adapter.rerank(
                                    rewritten_query,
                                    invocation.original_query,
                                    candidates,
                                ),
                                timeout,
                                BlockingTaskKind.RERANK,
                            )
                            self._validate_rerank(candidates, ranked)
                            return ranked

                        candidates = self._run_stage(
                            context,
                            records,
                            RetrievalStage.RERANK,
                            input_count=len(candidates),
                            budget_usage=RetrievalBudgetUsage(),
                            operation=rerank_candidates,
                            degradable=True,
                        )
                        reranked = True
                except RetrievalAdapterError as exc:
                    if exc.category != RetrievalErrorCategory.RERANK_FAILED:
                        raise
                    degradation_reasons.append(exc.safe_error_code)
                    self._mark_last_record_degraded(records)
                    candidates = sorted(
                        candidates,
                        key=lambda item: (item.original_rank, item.candidate_id),
                    )
            else:
                self._record_skipped(
                    context,
                    records,
                    RetrievalStage.RERANK,
                    input_count=len(candidates),
                    output_count=len(candidates),
                )

            if evaluation_capture is not None:
                if is_hybrid and hybrid_fusion is not None:
                    evaluation_capture.capture_bm25_evidence(
                        hybrid_fusion.bm25_evidence
                    )
                    evaluation_capture.capture_hybrid_ranked(hybrid_fusion)
                else:
                    evaluation_capture.capture_ranked(
                        candidates, reranked=reranked
                    )

            filtered = self._filter_candidates(candidates)
            if not filtered:
                usage = self._retrieval_usage_since(
                    ledger, retrieval_usage_baseline
                )
                self._skip_after(context, records, RetrievalStage.RERANK)
                return self._successful_result(
                    invocation,
                    started_at,
                    started_monotonic,
                    records,
                    usage,
                    degradation_reasons,
                    rewritten_query,
                    (),
                    emit_completed=emit_completed_event,
                    event_emitter=event_emitter,
                    hybrid_fusion=hybrid_fusion,
                )

            read_limit = self.spec.max_document_reads
            if getattr(self.adapter, "hybrid_rrf", False):
                read_limit = min(read_limit, invocation.rerank_top_k)
            read_candidates = filtered[:read_limit]
            document_usage = RetrievalBudgetUsage(
                document_reads=len(read_candidates)
            )

            def load_all(stage_timeout: float) -> tuple[list[MaterializedDocument], int]:
                stage_deadline = time.monotonic() + stage_timeout
                loaded: list[MaterializedDocument] = []
                failed = 0
                for candidate in read_candidates:
                    context.raise_if_cancelled()
                    timeout = min(
                        max(0.0, stage_deadline - time.monotonic()),
                        context.remaining_seconds(),
                    )
                    try:
                        document = self._invoke_budgeted(
                            context,
                            BudgetUsage(document_reads=1),
                            "retrieval_document_read",
                            lambda candidate=candidate: self.adapter.materialize(
                                candidate
                            ),
                            timeout,
                            BlockingTaskKind.DOCUMENT_LOAD,
                        )
                    except RetrievalAdapterError as exc:
                        if (
                            exc.category
                            != RetrievalErrorCategory.DOCUMENT_LOAD_FAILED
                            or not self.spec.allow_partial_document_load
                        ):
                            raise
                        failed += 1
                        continue
                    loaded.append(document)
                if not loaded:
                    raise RetrievalAdapterError(
                        RetrievalErrorCategory.DOCUMENT_LOAD_FAILED,
                        "DOCUMENT_LOAD_ALL_FAILED",
                        "全部候选内容物化失败。",
                    )
                return loaded, failed

            loaded, failed_loads = self._run_stage(
                context,
                records,
                RetrievalStage.DOCUMENT_LOAD,
                input_count=len(read_candidates),
                budget_usage=document_usage,
                operation=load_all,
                output_count_getter=lambda value: len(value[0]),
                degraded_getter=lambda value: bool(value[1]),
            )
            usage = usage.plus(document_usage)
            if failed_loads:
                degradation_reasons.append(
                    f"DOCUMENT_LOAD_PARTIAL_FAILED:{failed_loads}"
                )
                self._mark_last_record_degraded(records)

            context.raise_if_cancelled()
            estimated_context_chars = self._estimate_context_chars(
                loaded, invocation
            )
            context_usage = RetrievalBudgetUsage(
                context_chars=estimated_context_chars
            )

            def build_context_body() -> tuple[RetrievedChunk, ...]:
                reservation = context.budget_ledger.reserve(
                    BudgetUsage(context_chars=estimated_context_chars),
                    reservation_type="retrieval_context",
                    step_id=context.step_id,
                )
                started = False
                settled = False
                try:
                    context.raise_if_cancelled()
                    started = True
                    chunks = self._build_context_chunks(invocation, loaded)
                    actual_chars = sum(len(chunk.text) for chunk in chunks)
                    context.budget_ledger.commit(
                        reservation,
                        BudgetUsage(context_chars=actual_chars),
                        usage_source=UsageSource.ACTUAL,
                    )
                    settled = True
                    return chunks
                finally:
                    if not started:
                        context.budget_ledger.release(reservation)
                    elif not settled:
                        context.budget_ledger.commit(
                            reservation,
                            BudgetUsage(context_chars=estimated_context_chars),
                            usage_source=UsageSource.ESTIMATED,
                        )

            def build_context(stage_timeout: float) -> tuple[RetrievedChunk, ...]:
                return self._invoke_unbudgeted(
                    context,
                    build_context_body,
                    stage_timeout,
                    BlockingTaskKind.CONTEXT_BUILD,
                )

            chunks = self._run_stage(
                context,
                records,
                RetrievalStage.CONTEXT_BUILD,
                input_count=len(loaded),
                budget_usage=context_usage,
                operation=build_context,
                emit_event=not defer_completed_event,
            )
            actual_context_chars = sum(len(chunk.text) for chunk in chunks)
            usage = usage.plus(
                RetrievalBudgetUsage(context_chars=actual_context_chars)
            )
            usage = self._retrieval_usage_since(
                ledger, retrieval_usage_baseline
            )
            if not chunks:
                return self._successful_result(
                    invocation,
                    started_at,
                    started_monotonic,
                    records,
                    usage,
                    degradation_reasons,
                    rewritten_query,
                    (),
                    emit_completed=emit_completed_event,
                    event_emitter=event_emitter,
                    hybrid_fusion=hybrid_fusion,
                )
            context.raise_if_cancelled()
            return self._successful_result(
                invocation,
                started_at,
                started_monotonic,
                records,
                usage,
                degradation_reasons,
                rewritten_query,
                chunks,
                emit_completed=emit_completed_event,
                event_emitter=event_emitter,
                hybrid_fusion=hybrid_fusion,
            )
        except RunCancelledError:
            usage = self._retrieval_usage_since(
                ledger, retrieval_usage_baseline
            )
            return self._terminal_result(
                invocation,
                started_at,
                started_monotonic,
                records,
                usage,
                degradation_reasons,
                RetrievalExecutionStatus.CANCELLED,
                RetrievalExecutionError(
                    RetrievalErrorCategory.CANCELLED,
                    "RETRIEVAL_CANCELLED",
                    "Retrieval 已取消。",
                    records[-1].stage if records else None,
                ),
                rewritten_query,
                emit_completed=emit_completed_event,
                event_emitter=event_emitter,
            )
        except RunDeadlineExceededError:
            usage = self._retrieval_usage_since(
                ledger, retrieval_usage_baseline
            )
            return self._terminal_result(
                invocation,
                started_at,
                started_monotonic,
                records,
                usage,
                degradation_reasons,
                RetrievalExecutionStatus.TIMED_OUT,
                RetrievalExecutionError(
                    RetrievalErrorCategory.DEADLINE_EXCEEDED,
                    "RUN_DEADLINE_EXCEEDED",
                    "Run Deadline 已耗尽。",
                    records[-1].stage if records else None,
                ),
                rewritten_query,
                emit_completed=emit_completed_event,
                event_emitter=event_emitter,
            )
        except (RetrievalDeadlineExceededError, _StageTimedOut):
            usage = self._retrieval_usage_since(
                ledger, retrieval_usage_baseline
            )
            return self._terminal_result(
                invocation,
                started_at,
                started_monotonic,
                records,
                usage,
                degradation_reasons,
                RetrievalExecutionStatus.TIMED_OUT,
                RetrievalExecutionError(
                    RetrievalErrorCategory.TIMEOUT,
                    "RETRIEVAL_TIMEOUT",
                    "Retrieval 或当前阶段执行超时。",
                    records[-1].stage if records else None,
                ),
                rewritten_query,
                emit_completed=emit_completed_event,
                event_emitter=event_emitter,
            )
        except BudgetExceededError as exc:
            usage = self._retrieval_usage_since(
                ledger, retrieval_usage_baseline
            )
            return self._terminal_result(
                invocation,
                started_at,
                started_monotonic,
                records,
                usage,
                degradation_reasons,
                RetrievalExecutionStatus.FAILED,
                RetrievalExecutionError(
                    RetrievalErrorCategory.BUDGET_EXHAUSTED,
                    "BUDGET_EXHAUSTED",
                    "Retrieval 预算不足，阶段未执行。",
                    records[-1].stage if records else None,
                ),
                rewritten_query,
                emit_completed=emit_completed_event,
                event_emitter=event_emitter,
            )
        except RetrievalAdapterError as exc:
            usage = self._retrieval_usage_since(
                ledger, retrieval_usage_baseline
            )
            return self._terminal_result(
                invocation,
                started_at,
                started_monotonic,
                records,
                usage,
                degradation_reasons,
                RetrievalExecutionStatus.FAILED,
                RetrievalExecutionError(
                    exc.category,
                    exc.safe_error_code,
                    exc.safe_message,
                    records[-1].stage if records else None,
                ),
                rewritten_query,
                emit_completed=emit_completed_event,
                event_emitter=event_emitter,
            )
        except _EventEmissionFailed as exc:
            usage = self._retrieval_usage_since(
                ledger, retrieval_usage_baseline
            )
            return self._terminal_result(
                invocation,
                started_at,
                started_monotonic,
                records,
                usage,
                degradation_reasons,
                RetrievalExecutionStatus.FAILED,
                RetrievalExecutionError(
                    RetrievalErrorCategory.INTERNAL,
                    exc.safe_error_code,
                    "Retrieval Runtime Event 发布失败；已执行阶段不会重跑。",
                    records[-1].stage if records else None,
                ),
                rewritten_query,
                emit_completed=False,
                event_emitter=event_emitter,
            )
        except BaseException:
            usage = self._retrieval_usage_since(
                ledger, retrieval_usage_baseline
            )
            return self._terminal_result(
                invocation,
                started_at,
                started_monotonic,
                records,
                usage,
                degradation_reasons,
                RetrievalExecutionStatus.FAILED,
                RetrievalExecutionError(
                    RetrievalErrorCategory.INTERNAL,
                    "RETRIEVAL_INTERNAL",
                    "Retrieval Runtime 发生内部错误。",
                    records[-1].stage if records else None,
                ),
                rewritten_query,
                emit_completed=emit_completed_event,
                event_emitter=event_emitter,
            )

    def _run_stage(
        self,
        context: RetrievalExecutionContext,
        records: list[RetrievalStageRecord],
        stage: RetrievalStage,
        **kwargs,
    ) -> T:
        recorder = current_span_recorder() or self.span_recorder or NoopSpanRecorder()
        handle = start_span_safely(
            recorder,
            trace_id=context.run_context.trace_id,
            run_id=context.run_context.run_id,
            component="retrieval_stage",
            operation=stage.value.lower(),
            step_id=context.step_id,
        )
        if handle.context is not None:
            handle.set_safe_attribute("retrieval_stage", stage.value)
            handle.set_safe_attribute("input_count", kwargs["input_count"])
        token = install_trace_context(handle.context)
        recorder_token = install_span_recorder(recorder)
        try:
            value = self._run_stage_impl(context, records, stage, **kwargs)
            record = records[-1]
            if handle.context is not None:
                handle.set_safe_attribute("output_count", record.output_count)
                handle.set_safe_attribute("degraded", record.degraded)
            deferred = not kwargs.get("emit_event", True) and handle.context is not None
            if deferred:
                with self._trace_lock:
                    self._deferred_stage_contexts[id(record)] = handle
            else:
                handle.end_ok()
            return value
        except RunCancelledError:
            handle.end_cancelled("RETRIEVAL_CANCELLED")
            raise
        except (RunDeadlineExceededError, RetrievalDeadlineExceededError, _StageTimedOut):
            handle.end_timed_out("RETRIEVAL_TIMEOUT")
            raise
        except BaseException:
            error_code = (
                records[-1].safe_error_code
                if records and records[-1].stage is stage and records[-1].safe_error_code
                else "RETRIEVAL_STAGE_FAILED"
            )
            handle.end_error(error_code)
            raise
        finally:
            reset_trace_context(token)
            reset_span_recorder(recorder_token)

    def _run_stage_impl(
        self,
        context: RetrievalExecutionContext,
        records: list[RetrievalStageRecord],
        stage: RetrievalStage,
        *,
        input_count: int,
        budget_usage: RetrievalBudgetUsage,
        operation: Callable[[float], T],
        output_count_getter: Callable[[T], int] | None = None,
        degraded_getter: Callable[[T], bool] | None = None,
        degradable: bool = False,
        emit_event: bool = True,
    ) -> T:
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        stage_usage_baseline = (
            context.budget_ledger.snapshot().committed_usage
        )

        def actual_budget_usage() -> RetrievalBudgetUsage:
            return self._retrieval_usage_since(
                context.budget_ledger, stage_usage_baseline
            )

        try:
            timeout = context.before_stage(stage)
            if stage is RetrievalStage.QUERY_REWRITE:
                self._execute_fault_point(
                    context,
                    FaultPoint.RETRIEVAL_BEFORE_REWRITE,
                )
            elif (
                stage is RetrievalStage.EMBEDDING
                or (
                    stage is RetrievalStage.RETRIEVE
                    and not self.adapter.has_explicit_embedding
                )
            ):
                self._execute_fault_point(
                    context,
                    FaultPoint.RETRIEVAL_BEFORE_SEARCH,
                )
            value = operation(timeout)
            context.raise_if_cancelled()
            output_count = (
                output_count_getter(value)
                if output_count_getter is not None
                else self._output_count(value)
            )
            record = self._make_record(
                stage,
                RetrievalStageStatus.SUCCEEDED,
                started_at,
                started_monotonic,
                input_count,
                output_count,
                actual_budget_usage(),
                degraded=degraded_getter(value) if degraded_getter else False,
            )
            records.append(record)
            if emit_event:
                self._emit_stage(context, record)
            return value
        except RunCancelledError as exc:
            record = self._make_record(
                stage,
                RetrievalStageStatus.CANCELLED,
                started_at,
                started_monotonic,
                input_count,
                0,
                actual_budget_usage(),
                "RETRIEVAL_CANCELLED",
                worker_terminated=getattr(exc, "worker_terminated", True),
                execution_detached=getattr(exc, "execution_detached", False),
                background_work_pending=getattr(
                    exc, "background_work_pending", False
                ),
            )
            records.append(record)
            if emit_event:
                self._emit_stage(context, record, ignore_failure=True)
            raise
        except (
            RunDeadlineExceededError,
            RetrievalDeadlineExceededError,
            _StageTimedOut,
        ) as exc:
            record = self._make_record(
                stage,
                RetrievalStageStatus.TIMED_OUT,
                started_at,
                started_monotonic,
                input_count,
                0,
                actual_budget_usage(),
                "RETRIEVAL_TIMEOUT",
                worker_terminated=getattr(exc, "worker_terminated", True),
                execution_detached=getattr(exc, "execution_detached", False),
                background_work_pending=getattr(
                    exc, "background_work_pending", False
                ),
            )
            records.append(record)
            if emit_event:
                self._emit_stage(context, record, ignore_failure=True)
            raise
        except BudgetExceededError:
            record = self._make_record(
                stage,
                RetrievalStageStatus.FAILED,
                started_at,
                started_monotonic,
                input_count,
                0,
                actual_budget_usage(),
                "BUDGET_EXHAUSTED",
            )
            records.append(record)
            if emit_event:
                self._emit_stage(context, record, ignore_failure=True)
            raise
        except RetrievalAdapterError as exc:
            record = self._make_record(
                stage,
                RetrievalStageStatus.FAILED,
                started_at,
                started_monotonic,
                input_count,
                0,
                actual_budget_usage(),
                exc.safe_error_code,
                degraded=degradable,
            )
            records.append(record)
            if emit_event:
                self._emit_stage(context, record)
            raise
        except _EventEmissionFailed:
            raise
        except BaseException:
            record = self._make_record(
                stage,
                RetrievalStageStatus.FAILED,
                started_at,
                started_monotonic,
                input_count,
                0,
                actual_budget_usage(),
                "RETRIEVAL_STAGE_INTERNAL",
            )
            records.append(record)
            if emit_event:
                self._emit_stage(context, record, ignore_failure=True)
            raise

    @staticmethod
    def _execute_fault_point(
        context: RetrievalExecutionContext,
        point: FaultPoint,
    ) -> None:
        controller = context.fault_controller
        if controller is None or not controller.enabled:
            return
        match_context = FaultMatchContext(
            fault_point=point,
            component="retrieval",
            run_id_digest=hashlib.sha256(
                context.run_context.run_id.encode("utf-8")
            ).hexdigest(),
        )
        try:
            result = controller.execute_blocking_if_matched(
                match_context,
                raise_if_cancelled=context.raise_if_cancelled,
                allowed_actions={
                    FaultAction.RAISE_TYPED_ERROR,
                    FaultAction.DELAY,
                    FaultAction.BLOCK_UNTIL_RELEASED,
                },
            )
        except InjectedFaultError as exc:
            if exc.code is InjectedFaultCode.INJECTED_TIMEOUT:
                raise _StageTimedOut() from None
            if point is FaultPoint.RETRIEVAL_BEFORE_REWRITE:
                raise RetrievalAdapterError(
                    RetrievalErrorCategory.QUERY_REWRITE_FAILED,
                    (
                        "RETRIEVAL_INJECTED_TRANSIENT_FAILURE"
                        if exc.code
                        is InjectedFaultCode.INJECTED_TRANSIENT_FAILURE
                        else "RETRIEVAL_INJECTED_PERMANENT_FAILURE"
                    ),
                    "查询改写未完成。",
                ) from None
            if exc.code in {
                InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
                InjectedFaultCode.INJECTED_PERMANENT_FAILURE,
            }:
                raise RetrievalAdapterError(
                    RetrievalErrorCategory.VECTOR_STORE_FAILED,
                    (
                        "RETRIEVAL_INJECTED_TRANSIENT_FAILURE"
                        if exc.code
                        is InjectedFaultCode.INJECTED_TRANSIENT_FAILURE
                        else "RETRIEVAL_INJECTED_PERMANENT_FAILURE"
                    ),
                    "Retrieval 搜索未完成。",
                ) from None
            raise RetrievalAdapterError(
                RetrievalErrorCategory.INTERNAL,
                "RETRIEVAL_INJECTED_UNSUPPORTED_FAILURE",
                "Retrieval 注入类别不受当前 Adapter 支持。",
            ) from None
        if isinstance(result, InjectedFailureResult):
            raise RetrievalAdapterError(
                RetrievalErrorCategory.INTERNAL,
                "RETRIEVAL_INJECTED_ACTION_UNSUPPORTED",
                "Retrieval 注入动作不受当前接口支持。",
            )

    def _rewrite_query(
        self,
        context: RetrievalExecutionContext,
        query: str,
    ) -> str:
        method = self.adapter.rewrite_query
        parameters = inspect.signature(method).parameters
        if (
            context.fault_controller is not None
            and (
                "fault_controller" in parameters
                or any(
                    item.kind is inspect.Parameter.VAR_KEYWORD
                    for item in parameters.values()
                )
            )
        ):
            return method(
                query,
                run_context=context.run_context,
                event_emitter=context.event_emitter,
                fault_controller=context.fault_controller,
            )
        return method(
            query,
            run_context=context.run_context,
            event_emitter=context.event_emitter,
        )

    def _invoke_budgeted(
        self,
        context: RetrievalExecutionContext,
        budget_usage: BudgetUsage,
        reservation_type: str,
        operation: Callable[[], T],
        timeout_seconds: float,
        task_kind: BlockingTaskKind,
    ) -> T:
        if timeout_seconds <= 0:
            raise _StageTimedOut()
        reservation = context.budget_ledger.reserve(
            budget_usage,
            reservation_type=reservation_type,
            step_id=context.step_id,
        )
        settlement_lock = threading.Lock()
        settlement = "RESERVED"

        def budgeted_operation() -> T:
            nonlocal settlement
            with settlement_lock:
                if settlement == "RELEASED":
                    raise RuntimeError(
                        "blocking operation cancelled before provider start"
                    )
                context.budget_ledger.commit(
                    reservation,
                    budget_usage,
                    usage_source=UsageSource.ACTUAL,
                )
                settlement = "COMMITTED"
            return operation()

        try:
            context.raise_if_cancelled()
            future = self._submit_blocking(
                context,
                budgeted_operation,
                timeout_seconds,
                task_kind,
            )
            value = self._wait_future(
                future, context=context, timeout_seconds=timeout_seconds
            )
            context.raise_if_cancelled()
            return value
        finally:
            with settlement_lock:
                if settlement == "RESERVED":
                    context.budget_ledger.release(reservation)
                    settlement = "RELEASED"

    def _invoke_unbudgeted(
        self,
        context: RetrievalExecutionContext,
        operation: Callable[[], T],
        timeout_seconds: float,
        task_kind: BlockingTaskKind,
    ) -> T:
        if timeout_seconds <= 0:
            raise _StageTimedOut()
        context.raise_if_cancelled()
        future = self._submit_blocking(
            context,
            operation,
            timeout_seconds,
            task_kind,
        )
        return self._wait_future(
            future, context=context, timeout_seconds=timeout_seconds
        )

    def _submit_blocking(
        self,
        context: RetrievalExecutionContext,
        operation: Callable[[], T],
        timeout_seconds: float,
        task_kind: BlockingTaskKind,
    ) -> BlockingTaskHandle[T]:
        admission_deadline = time.monotonic() + timeout_seconds
        try:
            return self.blocking_executor.submit(
                operation,
                kind=task_kind,
                run_id=context.run_context.run_id,
                operation_id=f"{context.step_id}:{task_kind.value}",
                cancellation_check=context.raise_if_cancelled,
                remaining_seconds=lambda: min(
                    context.remaining_seconds(),
                    max(0.0, admission_deadline - time.monotonic()),
                ),
            )
        except BlockingExecutorAdmissionTimeout:
            raise _StageTimedOut() from None

    @staticmethod
    def _wait_future(
        future: BlockingTaskHandle[T],
        *,
        context: RetrievalExecutionContext,
        timeout_seconds: float,
    ) -> T:
        deadline = time.monotonic() + timeout_seconds

        def track_detached(state: BlockingTaskWaitState) -> None:
            if not state.execution_detached:
                return
            tracker = context.run_context.activity_tracker
            if tracker is not None:
                tracker.increment("detached_retrieval_workers")
                future.add_done_callback(
                    lambda: tracker.decrement("detached_retrieval_workers")
                )

        try:
            while True:
                context.raise_if_cancelled()
                remaining = min(
                    max(0.0, deadline - time.monotonic()),
                    context.remaining_seconds(),
                )
                if remaining <= 0:
                    state = future.cancel_or_detach()
                    track_detached(state)
                    raise _StageTimedOut(state)
                try:
                    return future.result(timeout=min(0.05, remaining))
                except concurrent.futures.TimeoutError:
                    continue
        except _StageTimedOut:
            raise
        except BaseException as exc:
            state = future.cancel_or_detach()
            track_detached(state)
            setattr(exc, "worker_terminated", state.worker_terminated)
            setattr(exc, "execution_detached", state.execution_detached)
            setattr(
                exc,
                "background_work_pending",
                state.background_work_pending,
            )
            raise

    def _should_keyword_retrieve(
        self,
        terms: list[str],
        invocation: RetrievalInvocation,
    ) -> bool:
        predicate = getattr(self.adapter, "should_keyword_retrieve", None)
        if callable(predicate):
            return bool(predicate(terms, invocation))
        return bool(
            terms
            and callable(getattr(self.adapter, "keyword_retrieve", None))
        )

    @staticmethod
    def _charge_started_call(
        context: RetrievalExecutionContext,
        budget_usage: BudgetUsage,
        reservation_type: str,
    ) -> None:
        reservation = context.budget_ledger.reserve(
            budget_usage,
            reservation_type=reservation_type,
            step_id=context.step_id,
        )
        context.budget_ledger.commit(
            reservation, budget_usage, usage_source=UsageSource.ACTUAL
        )

    def _execute_hybrid_fusion(
        self,
        context: RetrievalExecutionContext,
        rewritten_query: str,
        dense_candidates: Sequence[RetrievalCandidate],
    ):
        """在既有 RERANK slot 内执行 Hybrid fusion collaborator（WP2 §9/§12）。

        记账回调按"操作开始才计数"语义把 bm25_queries/rrf_fusions/
        document_reads 提交进 Run 预算账本；预算 Ledger 线程安全，可从
        blocking worker 线程调用。
        """
        fuse = getattr(self.adapter, "fuse", None)
        if (
            fuse is None
            or not getattr(self.adapter, "hybrid_rrf", False)
            or not callable(fuse)
        ):
            raise RetrievalAdapterError(
                RetrievalErrorCategory.STRATEGY_UNAVAILABLE,
                "HYBRID_CHANNEL_MISSING",
                "Hybrid fusion collaborator is unavailable.",
            )

        def charge(budget_usage: BudgetUsage) -> None:
            self._charge_started_call(context, budget_usage, "hybrid_fusion")

        return fuse(rewritten_query, dense_candidates, charge=charge)

    @staticmethod
    def _retrieval_usage_since(
        ledger: BudgetLedger,
        baseline: BudgetUsage,
    ) -> RetrievalBudgetUsage:
        current = ledger.snapshot().committed_usage
        return RetrievalBudgetUsage(
            retrieval_calls=current.retrieval_calls - baseline.retrieval_calls,
            embedding_calls=current.embedding_calls - baseline.embedding_calls,
            vector_queries=current.vector_queries - baseline.vector_queries,
            keyword_queries=current.keyword_queries - baseline.keyword_queries,
            bm25_queries=current.bm25_queries - baseline.bm25_queries,
            rrf_fusions=current.rrf_fusions - baseline.rrf_fusions,
            document_reads=current.document_reads - baseline.document_reads,
            context_chars=current.context_chars - baseline.context_chars,
        )

    @staticmethod
    def _merge_candidates(
        candidates: Sequence[RetrievalCandidate], max_candidates: int
    ) -> list[RetrievalCandidate]:
        winners: dict[str, RetrievalCandidate] = {}
        order: list[str] = []
        for candidate in candidates:
            previous = winners.get(candidate.candidate_id)
            if previous is None:
                order.append(candidate.candidate_id)
                winners[candidate.candidate_id] = candidate
            elif candidate.score > previous.score:
                winners[candidate.candidate_id] = replace(
                    candidate, original_rank=previous.original_rank
                )
        merged = [winners[candidate_id] for candidate_id in order]
        return [
            replace(candidate, original_rank=index)
            for index, candidate in enumerate(merged[:max_candidates], start=1)
        ]

    def _filter_candidates(
        self, candidates: Sequence[RetrievalCandidate]
    ) -> list[RetrievalCandidate]:
        if getattr(self.adapter, "hybrid_rrf", False):
            return list(candidates)
        if not candidates:
            return []
        best = max(candidate.effective_score for candidate in candidates)
        floor = max(self.minimum_score, best - 0.20)
        return [
            candidate
            for candidate in candidates
            if candidate.effective_score >= floor
        ]

    def _validate_rerank(
        self,
        original: Sequence[RetrievalCandidate],
        ranked: Sequence[RetrievalCandidate],
    ) -> None:
        if getattr(self.adapter, "hybrid_rrf", False):
            # Hybrid fusion 输出校验：fused rank 连续、raw RRF score 在位、
            # (document_id, chunk_id) 语义身份唯一（WP2 冻结合同 §18）。
            if not ranked:
                raise RetrievalAdapterError(
                    RetrievalErrorCategory.FUSION_FAILED,
                    "RRF_FUSION_FAILED",
                    "Hybrid RRF output is empty.",
                )
            ranks = [item.reranked_rank for item in ranked]
            if (
                any(rank is None for rank in ranks)
                or ranks != list(range(1, len(ranked) + 1))
                or any(item.reranked_score is None for item in ranked)
            ):
                raise RetrievalAdapterError(
                    RetrievalErrorCategory.FUSION_FAILED,
                    "RRF_FUSION_FAILED",
                    "Hybrid RRF output is invalid.",
                )
            identities = [(item.source_id, item.chunk_id) for item in ranked]
            if len(set(identities)) != len(identities):
                raise RetrievalAdapterError(
                    RetrievalErrorCategory.FUSION_FAILED,
                    "RRF_FUSION_FAILED",
                    "Hybrid RRF output contains duplicate identity.",
                )
            return
        original_by_id = {item.candidate_id: item for item in original}
        ranked_by_id = {item.candidate_id: item for item in ranked}
        if (
            not ranked
            or len(original_by_id) != len(original)
            or len(ranked_by_id) != len(ranked)
            or set(original_by_id) != set(ranked_by_id)
        ):
            raise RetrievalAdapterError(
                RetrievalErrorCategory.RERANK_FAILED,
                "RERANK_CANDIDATE_INTEGRITY_INVALID",
                "Rerank 未完整保留候选身份。",
            )
        ranks = []
        for candidate_id, candidate in ranked_by_id.items():
            before = original_by_id[candidate_id]
            if (
                candidate.source_id != before.source_id
                or candidate.chunk_id != before.chunk_id
                or candidate.original_rank != before.original_rank
                or candidate.reranked_score is None
                or candidate.reranked_rank is None
            ):
                raise RetrievalAdapterError(
                    RetrievalErrorCategory.RERANK_FAILED,
                    "RERANK_CANDIDATE_INTEGRITY_INVALID",
                    "Rerank 修改了候选身份或缺少真实排序字段。",
                )
            ranks.append(candidate.reranked_rank)
        if sorted(ranks) != list(range(1, len(ranked) + 1)):
            raise RetrievalAdapterError(
                RetrievalErrorCategory.RERANK_FAILED,
                "RERANK_RANK_INVALID",
                "Rerank Rank 必须连续且唯一。",
            )

    def _estimate_context_chars(
        self,
        documents: Sequence[MaterializedDocument],
        invocation: RetrievalInvocation,
    ) -> int:
        limit = min(
            invocation.rerank_top_k,
            self.spec.max_context_chunks,
            len(documents),
        )
        remaining = self.spec.max_context_chars
        total = 0
        seen: set[str] = set()
        for document in documents:
            normalized = " ".join(document.text.split())
            digest = content_digest(normalized)
            if not normalized or digest in seen:
                continue
            seen.add(digest)
            take = min(len(normalized), self.spec.max_single_chunk_chars, remaining)
            total += take
            remaining -= take
            if len(seen) >= limit or remaining <= 0:
                break
        return total

    def _build_context_chunks(
        self,
        invocation: RetrievalInvocation,
        documents: Sequence[MaterializedDocument],
    ) -> tuple[RetrievedChunk, ...]:
        selected: list[tuple[MaterializedDocument, str, tuple[RetrievalTransformation, ...]]] = []
        seen: dict[str, int] = {}
        remaining = self.spec.max_context_chars
        limit = min(invocation.rerank_top_k, self.spec.max_context_chunks)
        for document in documents:
            original = document.text
            normalized = " ".join(original.split())
            if not normalized:
                continue
            normalized_hash = content_digest(normalized)
            if normalized_hash in seen:
                winner_index = seen[normalized_hash]
                winner_document, winner_text, winner_transforms = selected[winner_index]
                if RetrievalTransformation.DEDUPLICATED not in winner_transforms:
                    selected[winner_index] = (
                        winner_document,
                        winner_text,
                        winner_transforms
                        + (RetrievalTransformation.DEDUPLICATED,),
                    )
                continue
            seen[normalized_hash] = len(selected)
            transformations = [RetrievalTransformation.LOADED]
            if normalized != original:
                transformations.append(RetrievalTransformation.NORMALIZED)
            candidate = document.candidate
            if candidate.reranked_rank is not None:
                # WP2：Hybrid 融合必须与 baseline heuristic rerank 可区分
                # （frozen decision §20：RRF_FUSED vs RERANKED）。
                if getattr(self.adapter, "hybrid_rrf", False):
                    transformations.append(
                        RetrievalTransformation.RRF_FUSED
                    )
                else:
                    transformations.append(RetrievalTransformation.RERANKED)
            allowed = min(self.spec.max_single_chunk_chars, remaining)
            if allowed <= 0:
                break
            text = normalized[:allowed]
            if text != normalized:
                transformations.append(RetrievalTransformation.TRUNCATED)
            transformations.append(RetrievalTransformation.CONTEXT_SELECTED)
            selected.append((document, text, tuple(transformations)))
            remaining -= len(text)
            if len(selected) >= limit or remaining <= 0:
                break

        chunks: list[RetrievedChunk] = []
        for index, (document, text, transformations) in enumerate(
            selected, start=1
        ):
            candidate = document.candidate
            context_hash = content_digest(text)
            block_id = f"context-{index}"
            citation_id = f"R{invocation.retrieval_id[:8]}-{index}"
            source = candidate.source
            label_parts = [source.display_name]
            if source.page is not None:
                label_parts.append(f"p.{source.page}")
            if source.section_path:
                label_parts.append(f"section: {source.section_path}")
            citation = CitationBinding(
                citation_id=citation_id,
                source_id=source.source_id,
                chunk_id=source.chunk_id,
                context_block_id=block_id,
                context_content_hash=context_hash,
                display_label=label_parts[0]
                + (
                    f" ({', '.join(label_parts[1:])})"
                    if len(label_parts) > 1
                    else ""
                ),
                page=source.page,
                section_path=source.section_path,
            )
            provenance = RetrievalProvenance(
                source_id=source.source_id,
                chunk_id=source.chunk_id,
                original_rank=candidate.original_rank,
                reranked_rank=candidate.reranked_rank,
                retrieval_score=candidate.score,
                transformations=transformations,
                original_content_hash=document.original_content_hash,
                context_content_hash=context_hash,
            )
            chunks.append(
                RetrievedChunk(
                    context_block_id=block_id,
                    text=text,
                    source=source,
                    provenance=provenance,
                    citation=citation,
                    trust_level=self._retrieved_trust_level(),
                    score=candidate.effective_score,
                )
            )
        return tuple(chunks)

    @staticmethod
    def _retrieved_trust_level():
        from core.runtime.model_context import ContextTrustLevel

        return ContextTrustLevel.UNTRUSTED_EXTERNAL

    @staticmethod
    def _extract_terms(rewritten_query: str, original_query: str) -> list[str]:
        # 保留旧补召回的轻量术语规则，不新增 Query Expansion。
        import re

        terms = set()
        for source in (rewritten_query, original_query):
            for token in re.findall(
                r"[A-Za-z_][A-Za-z0-9_./-]{1,}|[\u4e00-\u9fff]{2,}", source
            ):
                cleaned = token.strip().lower()
                if len(cleaned) >= 2:
                    terms.add(cleaned)
        return sorted(terms)

    def _successful_result(
        self,
        invocation: RetrievalInvocation,
        started_at: datetime,
        started_monotonic: float,
        records: list[RetrievalStageRecord],
        usage: RetrievalBudgetUsage,
        degradation_reasons: list[str],
        rewritten_query: str,
        chunks: tuple[RetrievedChunk, ...],
        *,
        emit_completed: bool = True,
        event_emitter: StepEventEmitter | None,
        hybrid_fusion=None,
    ) -> RetrievalExecutionResult:
        if chunks:
            status = (
                RetrievalExecutionStatus.DEGRADED
                if degradation_reasons
                else RetrievalExecutionStatus.SUCCEEDED
            )
        else:
            status = RetrievalExecutionStatus.EMPTY
        result = self._build_result(
            invocation,
            started_at,
            started_monotonic,
            records,
            usage,
            degradation_reasons,
            status,
            None,
            rewritten_query,
            chunks,
        )
        if emit_completed:
            self._emit_completed_result(
                result,
                event_emitter=event_emitter,
                ignore_failure=False,
                hybrid_fusion=hybrid_fusion,
            )
        return result

    def _terminal_result(
        self,
        invocation: RetrievalInvocation,
        started_at: datetime,
        started_monotonic: float,
        records: list[RetrievalStageRecord],
        usage: RetrievalBudgetUsage,
        degradation_reasons: list[str],
        status: RetrievalExecutionStatus,
        error: RetrievalExecutionError,
        rewritten_query: str,
        *,
        emit_completed: bool = True,
        event_emitter: StepEventEmitter | None,
        hybrid_fusion=None,
    ) -> RetrievalExecutionResult:
        result = self._build_result(
            invocation,
            started_at,
            started_monotonic,
            records,
            usage,
            degradation_reasons,
            status,
            error,
            rewritten_query,
            (),
        )
        if emit_completed:
            self._emit_completed_result(
                result,
                event_emitter=event_emitter,
                ignore_failure=True,
                hybrid_fusion=hybrid_fusion,
            )
        return result

    @staticmethod
    def _build_result(
        invocation: RetrievalInvocation,
        started_at: datetime,
        started_monotonic: float,
        records: list[RetrievalStageRecord],
        usage: RetrievalBudgetUsage,
        degradation_reasons: list[str],
        status: RetrievalExecutionStatus,
        error: RetrievalExecutionError | None,
        rewritten_query: str,
        chunks: tuple[RetrievedChunk, ...],
    ) -> RetrievalExecutionResult:
        completed_at = datetime.now(UTC)
        return RetrievalExecutionResult(
            retrieval_id=invocation.retrieval_id,
            status=status,
            rewritten_query_digest=query_digest(rewritten_query),
            final_chunks=chunks,
            citations=tuple(chunk.citation for chunk in chunks),
            stage_records=tuple(records),
            degraded=bool(degradation_reasons),
            degradation_reasons=tuple(degradation_reasons),
            budget_usage=usage,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=max(0, int((time.monotonic() - started_monotonic) * 1000)),
            error=error,
        )

    @staticmethod
    def _make_record(
        stage: RetrievalStage,
        status: RetrievalStageStatus,
        started_at: datetime,
        started_monotonic: float,
        input_count: int,
        output_count: int,
        budget_usage: RetrievalBudgetUsage,
        safe_error_code: str | None = None,
        degraded: bool = False,
        worker_terminated: bool = True,
        execution_detached: bool = False,
        background_work_pending: bool = False,
    ) -> RetrievalStageRecord:
        return RetrievalStageRecord(
            stage=stage,
            status=status,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            duration_ms=max(0, int((time.monotonic() - started_monotonic) * 1000)),
            input_count=input_count,
            output_count=output_count,
            safe_error_code=safe_error_code,
            budget_usage=budget_usage,
            degraded=degraded,
            worker_terminated=worker_terminated,
            execution_detached=execution_detached,
            background_work_pending=background_work_pending,
        )

    def _record_skipped(
        self,
        context: RetrievalExecutionContext,
        records: list[RetrievalStageRecord],
        stage: RetrievalStage,
        *,
        input_count: int = 0,
        output_count: int = 0,
        safe_error_code: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        record = RetrievalStageRecord(
            stage=stage,
            status=RetrievalStageStatus.SKIPPED,
            started_at=now,
            completed_at=now,
            duration_ms=0,
            input_count=input_count,
            output_count=output_count,
            safe_error_code=safe_error_code,
        )
        records.append(record)

    def _skip_after(
        self,
        context: RetrievalExecutionContext,
        records: list[RetrievalStageRecord],
        completed_stage: RetrievalStage,
    ) -> None:
        stages = list(RetrievalStage)
        for stage in stages[stages.index(completed_stage) + 1 :]:
            self._record_skipped(context, records, stage)

    @staticmethod
    def _mark_last_record_degraded(records: list[RetrievalStageRecord]) -> None:
        records[-1] = replace(records[-1], degraded=True)

    @staticmethod
    def _output_count(value: object) -> int:
        if value is None:
            return 0
        if isinstance(value, (str, QueryEmbedding)):
            return 1
        try:
            return len(value)  # type: ignore[arg-type]
        except TypeError:
            return 1

    def _emit_started(
        self, context: RetrievalExecutionContext, invocation: RetrievalInvocation
    ) -> None:
        if context.event_emitter is None:
            return
        from core.runtime.events import (
            RetrievalStartedPayload,
            RuntimeEventType,
        )

        try:
            context.event_emitter.emit_from_worker(
                RuntimeEventType.RETRIEVAL_STARTED,
                RetrievalStartedPayload(
                    retrieval_id=invocation.retrieval_id,
                    query_digest=invocation.query_digest,
                    collection_count=len(invocation.collection_names),
                    top_k=invocation.top_k,
                    retrieval_strategy=(
                        HYBRID_RETRIEVAL_STRATEGY_VALUE
                        if getattr(self.adapter, "hybrid_rrf", False)
                        else BASELINE_RETRIEVAL_STRATEGY_VALUE
                    ),
                    generation_id=getattr(
                        self.adapter, "hybrid_generation_id", None
                    ),
                    provenance_sha256=getattr(
                        self.adapter, "hybrid_provenance_sha256", None
                    ),
                ),
                component="retrieval_execution",
            )
        except JournalError as exc:
            raise _EventEmissionFailed(exc.error_code.value) from None
        except BaseException:
            raise _EventEmissionFailed from None

    def _emit_stage(
        self,
        context: RetrievalExecutionContext,
        record: RetrievalStageRecord,
        *,
        ignore_failure: bool = False,
    ) -> None:
        self.emit_stage_event(
            record,
            event_emitter=context.event_emitter,
            ignore_failure=ignore_failure,
        )

    def emit_stage_event(
        self,
        record: RetrievalStageRecord,
        *,
        event_emitter: StepEventEmitter | None,
        ignore_failure: bool = False,
    ) -> None:
        """发布上层延迟 Context Binding 的唯一 Stage 完成事实。"""
        if not isinstance(record, RetrievalStageRecord):
            raise TypeError("record 必须是 RetrievalStageRecord")
        with self._trace_lock:
            deferred_handle = self._deferred_stage_contexts.pop(id(record), None)
        if event_emitter is None:
            if deferred_handle is not None:
                deferred_handle.end_ok()
            return
        trace_token = (
            install_trace_context(deferred_handle.context)
            if deferred_handle is not None
            else None
        )
        from core.runtime.events import (
            RetrievalBudgetPayload,
            RetrievalStageCompletedPayload,
            RuntimeEventType,
        )

        try:
            event_emitter.emit_from_worker(
                RuntimeEventType.RETRIEVAL_STAGE_COMPLETED,
                RetrievalStageCompletedPayload(
                    stage=record.stage.value,
                    status=record.status.value,
                    duration_ms=record.duration_ms,
                    input_count=record.input_count,
                    output_count=record.output_count,
                    degraded=record.degraded,
                    safe_error_code=record.safe_error_code,
                    budget_usage=RetrievalBudgetPayload(
                        **record.budget_usage.to_safe_dict()
                    ),
                    worker_terminated=record.worker_terminated,
                    execution_detached=record.execution_detached,
                    background_work_pending=record.background_work_pending,
                ),
                component="retrieval_execution",
            )
        except JournalError as exc:
            if not ignore_failure:
                raise _EventEmissionFailed(exc.error_code.value) from None
        except BaseException:
            if not ignore_failure:
                raise _EventEmissionFailed from None
        finally:
            if deferred_handle is not None:
                if record.status is RetrievalStageStatus.CANCELLED:
                    deferred_handle.end_cancelled(
                        record.safe_error_code or "RETRIEVAL_CANCELLED"
                    )
                elif record.status is RetrievalStageStatus.TIMED_OUT:
                    deferred_handle.end_timed_out(
                        record.safe_error_code or "RETRIEVAL_TIMEOUT"
                    )
                elif record.status is RetrievalStageStatus.FAILED:
                    deferred_handle.end_error(
                        record.safe_error_code or "RETRIEVAL_STAGE_FAILED"
                    )
                else:
                    deferred_handle.end_ok()
            if trace_token is not None:
                reset_trace_context(trace_token)

    def _emit_completed_result(
        self,
        result: RetrievalExecutionResult,
        *,
        event_emitter: StepEventEmitter | None,
        ignore_failure: bool,
        hybrid_fusion=None,
    ) -> None:
        if event_emitter is None:
            return
        is_hybrid = bool(getattr(self.adapter, "hybrid_rrf", False))
        if is_hybrid:
            with self._trace_lock:
                if hybrid_fusion is None:
                    hybrid_fusion = self._deferred_hybrid_fusion.pop(
                        result.retrieval_id, None
                    )
                else:
                    self._deferred_hybrid_fusion.pop(result.retrieval_id, None)
        from core.runtime.events import (
            RetrievalBudgetPayload,
            RetrievalCompletedPayload,
            RuntimeEventType,
        )

        payload = RetrievalCompletedPayload(
            retrieval_id=result.retrieval_id,
            status=result.status.value,
            duration_ms=result.duration_ms,
            chunk_count=len(result.final_chunks),
            citation_count=len(result.citations),
            degraded=result.degraded,
            safe_error_code=(
                result.error.safe_error_code if result.error is not None else None
            ),
            budget_usage=RetrievalBudgetPayload(
                **result.budget_usage.to_safe_dict()
            ),
            worker_terminated=result.worker_terminated,
            execution_detached=result.execution_detached,
            background_work_pending=result.background_work_pending,
            retrieval_strategy=(
                HYBRID_RETRIEVAL_STRATEGY_VALUE
                if is_hybrid
                else BASELINE_RETRIEVAL_STRATEGY_VALUE
            ),
            generation_id=getattr(
                self.adapter, "hybrid_generation_id", None
            ),
            provenance_sha256=getattr(
                self.adapter, "hybrid_provenance_sha256", None
            ),
            dense_candidate_count=(
                hybrid_fusion.dense_candidate_count
                if hybrid_fusion is not None
                else None
            ),
            bm25_candidate_count=(
                hybrid_fusion.bm25_candidate_count
                if hybrid_fusion is not None
                else None
            ),
            rrf_fused_count=(
                hybrid_fusion.rrf_fused_count
                if hybrid_fusion is not None
                else None
            ),
            dense_latency_ms=next(
                (
                    record.duration_ms
                    for record in result.stage_records
                    if record.stage is RetrievalStage.RETRIEVE
                    and record.status
                    is not RetrievalStageStatus.SKIPPED
                ),
                None,
            ),
            bm25_latency_ms=(
                hybrid_fusion.bm25_latency_ms if hybrid_fusion is not None else None
            ),
            rrf_latency_ms=(
                hybrid_fusion.rrf_latency_ms if hybrid_fusion is not None else None
            ),
        )
        try:
            event_emitter.emit_from_worker(
                RuntimeEventType.RETRIEVAL_COMPLETED,
                payload,
                component="retrieval_execution",
            )
        except JournalError as exc:
            if not ignore_failure:
                raise _EventEmissionFailed(exc.error_code.value) from None
        except BaseException:
            if not ignore_failure:
                raise _EventEmissionFailed from None

    def emit_completed_event(
        self,
        result: RetrievalExecutionResult,
        *,
        event_emitter: StepEventEmitter | None,
    ) -> None:
        """为上层延迟 Context Binding 发布唯一 Retrieval 完成事实。"""
        if not isinstance(result, RetrievalExecutionResult):
            raise TypeError("result 必须是 RetrievalExecutionResult")
        with self._trace_lock:
            handle = self._deferred_retrieval_spans.pop(result.retrieval_id, None)
        trace_token = (
            install_trace_context(handle.context)
            if handle is not None
            else None
        )
        try:
            self._emit_completed_result(
                result,
                event_emitter=event_emitter,
                ignore_failure=False,
            )
        finally:
            if handle is not None:
                self._end_retrieval_span(handle, result)
            if trace_token is not None:
                reset_trace_context(trace_token)

    @staticmethod
    def _end_retrieval_span(handle, result: RetrievalExecutionResult) -> None:
        if result.status is RetrievalExecutionStatus.CANCELLED:
            handle.end_cancelled(
                result.error.safe_error_code if result.error else "RETRIEVAL_CANCELLED"
            )
        elif result.status is RetrievalExecutionStatus.TIMED_OUT:
            handle.end_timed_out(
                result.error.safe_error_code if result.error else "RETRIEVAL_TIMEOUT"
            )
        elif result.status is RetrievalExecutionStatus.FAILED:
            handle.end_error(
                result.error.safe_error_code if result.error else "RETRIEVAL_FAILED"
            )
        else:
            handle.end_ok()


__all__ = ["RetrievalExecutionService"]
