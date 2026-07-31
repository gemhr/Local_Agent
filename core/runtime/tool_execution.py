#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tool Attempt 与 Retry 编排；Runtime 是资源、预算、超时和事件 Owner。"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
import inspect
import threading
import time
from typing import Callable
from uuid import uuid4

from core.runtime.budget import (
    BudgetExceededError,
    BudgetLedger,
    BudgetUsage,
    UsageSource,
)
from core.runtime.cancellation import (
    CancellationReason,
    CancellationSource,
    CancellationToken,
    RunCancelledError,
)
from core.runtime.context import RunContext, RunDeadlineExceededError
from core.runtime.event_emitter import StepEventEmitter
from core.runtime.event_journal import JournalError
from core.runtime.events import (
    RuntimeEventType,
    TOOL_EVIDENCE_SCHEMA_VERSION,
    ToolCompletedPayload,
    ToolStartedPayload,
)
from core.runtime.fault_injection import FaultInjectionController
from core.runtime.fault_injection_contract import (
    FaultAction,
    FaultMatchContext,
    FaultPoint,
    InjectedFailureResult,
    InjectedFaultCode,
    InjectedFaultError,
)
from core.runtime.model_routing import ModelFailureCategory
from core.runtime.retry import RetryDecision, RetryExecutor, RetryPolicy
from core.runtime.tool_adapters import (
    ToolAdapter,
    ToolAdapterInvocationError,
    ToolAdapterResponse,
)
from core.runtime.tool_concurrency import (
    ToolConcurrencyController,
    ToolResourceAcquireError,
    ToolResourceLease,
)
from core.runtime.tool_contract import (
    RetryDisposition,
    ToolErrorCategory,
    ToolExecutionError,
    ToolExecutionPhase,
    ToolExecutionResult,
    ToolExecutionSpec,
    ToolExecutionStatus,
    ToolInvocation,
    ToolOutputValidationError,
    ToolSideEffectState,
    build_tool_output,
    retry_disposition_for,
    safe_key_digest,
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


class AttemptSideEffectTracker:
    """线程安全地维护一次 Attempt 的正式副作用状态机。"""

    def __init__(self) -> None:
        self._state = ToolSideEffectState.NOT_STARTED
        self._lock = threading.Lock()

    @property
    def state(self) -> ToolSideEffectState:
        with self._lock:
            return self._state

    def before_side_effect(self) -> None:
        with self._lock:
            if self._state != ToolSideEffectState.NOT_STARTED:
                raise RuntimeError("before_side_effect 不能重复或在非法状态调用")
            self._state = ToolSideEffectState.STARTED

    def observe(self, state: ToolSideEffectState) -> None:
        if not isinstance(state, ToolSideEffectState):
            raise TypeError("state 必须是 ToolSideEffectState")
        with self._lock:
            if self._state == ToolSideEffectState.UNKNOWN:
                return
            if state == ToolSideEffectState.UNKNOWN:
                self._state = state
                return
            if state == ToolSideEffectState.NOT_STARTED:
                return
            if state == ToolSideEffectState.STARTED:
                if self._state == ToolSideEffectState.NOT_STARTED:
                    self._state = state
                return
            if state in {
                ToolSideEffectState.COMMITTED,
                ToolSideEffectState.COMPENSATED,
            }:
                self._state = state

    def resolve_authoritative(self, state: ToolSideEffectState) -> None:
        """Adapter 提供权威结果时允许从 STARTED 收口到明确终态。"""
        if not isinstance(state, ToolSideEffectState):
            raise TypeError("state 必须是 ToolSideEffectState")
        with self._lock:
            self._state = state

    def mark_unknown_if_started(self) -> ToolSideEffectState:
        with self._lock:
            if self._state == ToolSideEffectState.STARTED:
                self._state = ToolSideEffectState.UNKNOWN
            return self._state


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    run_context: RunContext
    step_id: str
    attempt_id: str
    retry_index: int
    budget_ledger: BudgetLedger
    event_emitter: StepEventEmitter | None
    concurrency_controller: ToolConcurrencyController
    effective_deadline_monotonic: float
    attempt_cancellation_token: CancellationToken
    side_effect_tracker: AttemptSideEffectTracker
    before_side_effect_fault: Callable[[], None] | None = None

    def raise_if_cancelled(self) -> None:
        self.run_context.raise_if_inactive()
        self.attempt_cancellation_token.raise_if_cancelled()
        if self.remaining_seconds() <= 0:
            raise RunDeadlineExceededError("Tool Attempt 截止时间已到期")

    def remaining_seconds(self) -> float:
        attempt_remaining = max(
            0.0, self.effective_deadline_monotonic - time.monotonic()
        )
        run_remaining = self.run_context.remaining_seconds()
        if run_remaining is None:
            return attempt_remaining
        return max(0.0, min(attempt_remaining, run_remaining))

    def before_side_effect(self) -> None:
        """提交副作用前重新检查取消和 Deadline，再执行唯一合法状态转换。"""
        self.raise_if_cancelled()
        if self.before_side_effect_fault is not None:
            self.before_side_effect_fault()
        self.raise_if_cancelled()
        self.side_effect_tracker.before_side_effect()

    @property
    def side_effect_state(self) -> ToolSideEffectState:
        return self.side_effect_tracker.state


class ToolAttemptFailed(RuntimeError):
    def __init__(self, error: ToolExecutionError) -> None:
        self.error = error
        super().__init__(error.safe_message)


class ToolExecutionFailed(RuntimeError):
    """给 Step Driver 的安全异常；不暴露参数、原始异常或 Tool Output。"""

    def __init__(self, error: ToolExecutionError) -> None:
        self.error = error
        self.error_code = error.safe_error_code
        self.safe_message = error.safe_message
        super().__init__(error.safe_message)


class _ToolTimedOut(RuntimeError):
    def __init__(self, response: ToolAdapterResponse | None, *, lingering: bool) -> None:
        self.response = response
        self.lingering = lingering
        super().__init__("Tool Attempt timed out")


class ToolAttemptExecutor:
    """一次调用的唯一资源边界；不进行 Retry。"""

    def __init__(
        self,
        *,
        sync_timeout_grace_seconds: float = 0.05,
        sync_workers: int = 16,
        span_recorder=None,
    ) -> None:
        if sync_timeout_grace_seconds < 0:
            raise ValueError("sync_timeout_grace_seconds 必须是非负数")
        self.sync_timeout_grace_seconds = sync_timeout_grace_seconds
        self._sync_executor = ThreadPoolExecutor(
            max_workers=sync_workers, thread_name_prefix="tool-attempt"
        )
        self.span_recorder = span_recorder

    async def execute(
        self,
        *,
        invocation: ToolInvocation,
        adapter: ToolAdapter,
        spec: ToolExecutionSpec,
        run_context: RunContext,
        budget_ledger: BudgetLedger,
        concurrency_controller: ToolConcurrencyController,
        step_id: str,
        retry_index: int,
        event_emitter: StepEventEmitter | None,
        fault_controller: FaultInjectionController | None = None,
    ) -> ToolExecutionResult:
        recorder = self.span_recorder or current_span_recorder() or NoopSpanRecorder()
        handle = start_span_safely(
            recorder,
            trace_id=run_context.trace_id,
            run_id=run_context.run_id,
            component="tool_attempt",
            operation="attempt",
            step_id=step_id,
        )
        if handle.context is not None:
            handle.set_safe_attribute("tool_name", invocation.tool_name)
            handle.set_safe_attribute("retry_index", retry_index)
        token = install_trace_context(handle.context)
        recorder_token = install_span_recorder(recorder)
        activity_tracker = run_context.activity_tracker
        if activity_tracker is not None:
            activity_tracker.increment("tool_attempts_active")
        try:
            result = await self._execute_impl(
                invocation=invocation,
                adapter=adapter,
                spec=spec,
                run_context=run_context,
                budget_ledger=budget_ledger,
                concurrency_controller=concurrency_controller,
                step_id=step_id,
                retry_index=retry_index,
                event_emitter=event_emitter,
                fault_controller=fault_controller,
            )
        except ToolAttemptFailed as failed:
            error = failed.error
            if handle.context is not None:
                handle.set_safe_attribute("provider_started", error.provider_started)
                handle.set_safe_attribute("side_effect_state", error.side_effect_state.value)
                handle.set_safe_attribute("retry_disposition", error.retry_disposition.value)
                handle.set_safe_attribute("execution_detached", error.execution_detached)
            if error.status is ToolExecutionStatus.TIMED_OUT:
                handle.end_timed_out(error.safe_error_code)
            elif error.status is ToolExecutionStatus.CANCELLED:
                handle.end_cancelled(error.safe_error_code)
            else:
                handle.end_error(error.safe_error_code)
            raise
        except RunCancelledError:
            handle.end_cancelled("RUN_CANCELLED")
            raise
        except (RunDeadlineExceededError, TimeoutError):
            handle.end_timed_out()
            raise
        except BaseException:
            handle.end_error()
            raise
        else:
            if handle.context is not None:
                handle.set_safe_attribute("provider_started", True)
                handle.set_safe_attribute("side_effect_state", result.side_effect_state.value)
                handle.set_safe_attribute("retry_disposition", result.retry_disposition.value)
                handle.set_safe_attribute("execution_detached", result.execution_detached)
            handle.end_ok()
            return result
        finally:
            if activity_tracker is not None:
                activity_tracker.decrement("tool_attempts_active")
            reset_trace_context(token)
            reset_span_recorder(recorder_token)

    async def _execute_impl(
        self,
        *,
        invocation: ToolInvocation,
        adapter: ToolAdapter,
        spec: ToolExecutionSpec,
        run_context: RunContext,
        budget_ledger: BudgetLedger,
        concurrency_controller: ToolConcurrencyController,
        step_id: str,
        retry_index: int,
        event_emitter: StepEventEmitter | None,
        fault_controller: FaultInjectionController | None,
    ) -> ToolExecutionResult:
        attempt_id = uuid4().hex
        tracker = AttemptSideEffectTracker()
        attempt_source = CancellationSource()
        effective_timeout = _effective_timeout(invocation, spec, run_context)
        effective_deadline = time.monotonic() + effective_timeout
        before_side_effect_fault = _tool_blocking_fault_callback(
            fault_controller,
            FaultPoint.TOOL_BEFORE_SIDE_EFFECT_COMMIT,
            run_context=run_context,
            invocation=invocation,
            attempt_number=retry_index + 1,
            raise_if_cancelled=lambda: _raise_if_tool_attempt_inactive(
                run_context,
                attempt_source.token,
                effective_deadline,
            ),
        )
        context = ToolExecutionContext(
            run_context=run_context,
            step_id=step_id,
            attempt_id=attempt_id,
            retry_index=retry_index,
            budget_ledger=budget_ledger,
            event_emitter=event_emitter,
            concurrency_controller=concurrency_controller,
            effective_deadline_monotonic=effective_deadline,
            attempt_cancellation_token=attempt_source.token,
            side_effect_tracker=tracker,
            before_side_effect_fault=before_side_effect_fault,
        )
        lease: ToolResourceLease | None = None
        release_deferred = {"value": False}
        reservation = None
        started_event_emitted = False
        provider_started = False
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()

        def elapsed_ms() -> int:
            return max(0, int((time.monotonic() - started_monotonic) * 1000))

        try:
            context.raise_if_cancelled()
            lease = await concurrency_controller.acquire(
                tool_name=spec.tool_name,
                tool_max_concurrency=spec.max_concurrency,
                resource_key=invocation.resource_key,
                cancellation_token=run_context.cancellation_token,
                remaining_seconds=context.remaining_seconds,
            )
            context.raise_if_cancelled()
            usage = BudgetUsage(
                tool_calls=1,
                retries=1 if retry_index > 0 else 0,
            )
            reservation = budget_ledger.reserve(
                usage, reservation_type="tool_attempt", step_id=step_id
            )
            context.raise_if_cancelled()
            if event_emitter is not None:
                await event_emitter.emit(
                    RuntimeEventType.TOOL_STARTED,
                    ToolStartedPayload(
                        tool_name=spec.tool_name,
                        retry_index=retry_index,
                        tool_evidence_schema_version=TOOL_EVIDENCE_SCHEMA_VERSION,
                        invocation_identity_digest=safe_key_digest(
                            invocation.invocation_id
                        ),
                        attempt_identity_digest=safe_key_digest(attempt_id),
                        side_effect_kind=spec.side_effect_kind.value,
                        idempotency_kind=spec.idempotency.value,
                        idempotency_key_digest=safe_key_digest(
                            invocation.idempotency_key
                        ),
                        replay_supported=spec.supports_idempotency_replay,
                        side_effect_state=tracker.state.value,
                        compensation_state="NOT_ATTEMPTED",
                        retry_disposition="PENDING",
                        outcome_classification="PENDING",
                        execution_detached=False,
                        worker_terminated=False,
                        provider_started=False,
                    ),
                    component="tool_attempt_executor",
                )
            started_event_emitted = True
            context.raise_if_cancelled()
            await _execute_tool_fault_point(
                fault_controller,
                FaultPoint.TOOL_BEFORE_PROVIDER_CALL,
                run_context=run_context,
                invocation=invocation,
                attempt_number=retry_index + 1,
                raise_if_cancelled=context.raise_if_cancelled,
                remaining_seconds=context.remaining_seconds,
            )
            context.raise_if_cancelled()
            provider_started = True
            try:
                response = await self._invoke_adapter(
                    adapter=adapter,
                    invocation=invocation,
                    context=context,
                    attempt_source=attempt_source,
                    lease=lease,
                    release_deferred=release_deferred,
                )
            finally:
                budget_ledger.commit(
                    reservation,
                    usage,
                    usage_source=UsageSource.ESTIMATED,
                )
                reservation = None
            if response.side_effect_state_authoritative:
                tracker.resolve_authoritative(response.side_effect_state)
            else:
                tracker.observe(response.side_effect_state)
            await _execute_tool_fault_point(
                fault_controller,
                FaultPoint.TOOL_AFTER_PROVIDER_RETURN,
                run_context=run_context,
                invocation=invocation,
                attempt_number=retry_index + 1,
                raise_if_cancelled=context.raise_if_cancelled,
                remaining_seconds=context.remaining_seconds,
            )
            context.raise_if_cancelled()
            output = build_tool_output(
                response.content, response.content_type, spec.max_output_bytes
            )
            completed_at = datetime.now(UTC)
            result = ToolExecutionResult(
                invocation_id=invocation.invocation_id,
                attempt_id=attempt_id,
                tool_name=spec.tool_name,
                status=response.status,
                output=output,
                safe_summary=response.safe_summary,
                side_effect_state=tracker.state,
                idempotency_replayed=response.idempotency_replayed,
                retry_disposition=RetryDisposition.UNSAFE,
                resource_key_digest=safe_key_digest(invocation.resource_key),
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=max(
                    0, int((time.monotonic() - started_monotonic) * 1000)
                ),
                retry_index=retry_index,
            )
            completed = await self._emit_completed(
                event_emitter,
                spec=spec,
                invocation=invocation,
                result=result,
            )
            if isinstance(completed, ToolExecutionError):
                raise ToolAttemptFailed(completed)
            return result
        except JournalError as exc:
            # Started 写入失败时 Tool 尚未调用；Completed 写入失败时也禁止重试，
            # 避免重复业务副作用。统一返回安全、不可重试的 Journal 错误。
            state = tracker.mark_unknown_if_started()
            raise ToolAttemptFailed(
                ToolExecutionError(
                    invocation_id=invocation.invocation_id,
                    attempt_id=attempt_id,
                    tool_name=spec.tool_name,
                    category=ToolErrorCategory.INTERNAL,
                    safe_error_code=exc.error_code.value,
                    safe_message=exc.safe_message,
                    phase=ToolExecutionPhase.INVOCATION,
                    provider_started=provider_started,
                    side_effect_state=state,
                    retry_disposition=RetryDisposition.UNSAFE,
                    retry_index=retry_index,
                )
            ) from None
        except ToolOutputValidationError as exc:
            error = ToolExecutionError(
                invocation_id=invocation.invocation_id,
                attempt_id=attempt_id,
                tool_name=spec.tool_name,
                category=exc.category,
                safe_error_code=exc.safe_error_code,
                safe_message=exc.safe_message,
                phase=ToolExecutionPhase.OUTPUT,
                provider_started=provider_started,
                side_effect_state=tracker.state,
                retry_disposition=RetryDisposition.UNSAFE,
                partial_result=exc.safe_metadata,
                retry_index=retry_index,
            )
            if started_event_emitted:
                error = await self._emit_completed(
                    event_emitter,
                    spec=spec,
                    invocation=invocation,
                    error=error,
                    duration_ms=elapsed_ms(),
                )
            raise ToolAttemptFailed(error) from None
        except ToolAdapterInvocationError as exc:
            if exc.side_effect_state_authoritative:
                tracker.resolve_authoritative(exc.side_effect_state)
            else:
                tracker.mark_unknown_if_started()
            error = self._adapter_error(
                invocation=invocation,
                attempt_id=attempt_id,
                retry_index=retry_index,
                spec=spec,
                tracker=tracker,
                provider_started=provider_started,
                exc=exc,
            )
            if started_event_emitted:
                error = await self._emit_completed(
                    event_emitter,
                    spec=spec,
                    invocation=invocation,
                    error=error,
                    duration_ms=elapsed_ms(),
                )
            raise ToolAttemptFailed(error) from None
        except _ToolTimedOut as exc:
            if exc.response is not None:
                if exc.response.side_effect_state_authoritative:
                    tracker.resolve_authoritative(exc.response.side_effect_state)
                else:
                    tracker.mark_unknown_if_started()
            else:
                tracker.mark_unknown_if_started()
            if exc.lingering:
                tracker.resolve_authoritative(ToolSideEffectState.UNKNOWN)
            state = tracker.state
            disposition = retry_disposition_for(
                category=ToolErrorCategory.TIMEOUT,
                idempotency=spec.idempotency,
                idempotency_key=invocation.idempotency_key,
                side_effect_state=state,
                supports_idempotency_replay=spec.supports_idempotency_replay,
            )
            error = ToolExecutionError(
                invocation_id=invocation.invocation_id,
                attempt_id=attempt_id,
                tool_name=spec.tool_name,
                category=ToolErrorCategory.TIMEOUT,
                safe_error_code="TOOL_TIMEOUT",
                safe_message="Tool Attempt 超时。",
                phase=ToolExecutionPhase.INVOCATION,
                provider_started=provider_started,
                side_effect_state=state,
                retry_disposition=disposition,
                retry_index=retry_index,
                status=ToolExecutionStatus.TIMED_OUT,
                worker_terminated=not exc.lingering,
                execution_detached=exc.lingering,
                resource_release_pending=exc.lingering,
            )
            if started_event_emitted:
                error = await self._emit_completed(
                    event_emitter,
                    spec=spec,
                    invocation=invocation,
                    error=error,
                    duration_ms=elapsed_ms(),
                )
            raise ToolAttemptFailed(error) from None
        except BudgetExceededError as exc:
            raise ToolAttemptFailed(
                ToolExecutionError(
                    invocation_id=invocation.invocation_id,
                    attempt_id=attempt_id,
                    tool_name=spec.tool_name,
                    category=ToolErrorCategory.BUDGET_EXHAUSTED,
                    safe_error_code=exc.error_code,
                    safe_message="Tool 调用预算不足。",
                    phase=ToolExecutionPhase.BUDGET,
                    provider_started=False,
                    side_effect_state=tracker.state,
                    retry_disposition=RetryDisposition.UNSAFE,
                    retry_index=retry_index,
                )
            ) from None
        except ToolResourceAcquireError as exc:
            raise ToolAttemptFailed(
                ToolExecutionError(
                    invocation_id=invocation.invocation_id,
                    attempt_id=attempt_id,
                    tool_name=spec.tool_name,
                    category=ToolErrorCategory.RESOURCE_CONFLICT,
                    safe_error_code=exc.safe_error_code,
                    safe_message=exc.safe_message,
                    phase=ToolExecutionPhase.RESOURCE_WAIT,
                    provider_started=False,
                    side_effect_state=tracker.state,
                    retry_disposition=retry_disposition_for(
                        category=ToolErrorCategory.RESOURCE_CONFLICT,
                        idempotency=spec.idempotency,
                        idempotency_key=invocation.idempotency_key,
                        side_effect_state=tracker.state,
                        supports_idempotency_replay=spec.supports_idempotency_replay,
                    ),
                    retry_index=retry_index,
                )
            ) from None
        except InjectedFaultError as exc:
            state = tracker.mark_unknown_if_started()
            error = _tool_injected_error(
                exc,
                invocation=invocation,
                spec=spec,
                attempt_id=attempt_id,
                retry_index=retry_index,
                provider_started=provider_started,
                side_effect_state=state,
                post_provider=(
                    provider_started
                    and state
                    in {
                        ToolSideEffectState.COMMITTED,
                        ToolSideEffectState.UNKNOWN,
                    }
                ),
            )
            if started_event_emitted:
                error = await self._emit_completed(
                    event_emitter,
                    spec=spec,
                    invocation=invocation,
                    error=error,
                    duration_ms=elapsed_ms(),
                )
            raise ToolAttemptFailed(error) from None
        except RunCancelledError:
            state = tracker.mark_unknown_if_started()
            detached = release_deferred["value"]
            if detached:
                tracker.resolve_authoritative(ToolSideEffectState.UNKNOWN)
                state = ToolSideEffectState.UNKNOWN
            if started_event_emitted:
                await self._emit_completed(
                    event_emitter,
                    spec=spec,
                    invocation=invocation,
                    duration_ms=elapsed_ms(),
                    error=ToolExecutionError(
                        invocation_id=invocation.invocation_id,
                        attempt_id=attempt_id,
                        tool_name=spec.tool_name,
                        category=ToolErrorCategory.CANCELLED,
                        safe_error_code="TOOL_CANCELLED",
                        safe_message="Tool 调用已取消。",
                        phase=ToolExecutionPhase.INVOCATION,
                        provider_started=provider_started,
                        side_effect_state=state,
                        retry_disposition=(
                            RetryDisposition.OUTCOME_UNKNOWN
                            if state == ToolSideEffectState.UNKNOWN
                            else RetryDisposition.UNSAFE
                        ),
                        retry_index=retry_index,
                        status=ToolExecutionStatus.CANCELLED,
                        worker_terminated=not detached,
                        execution_detached=detached,
                        resource_release_pending=detached,
                    ),
                )
            raise
        except RunDeadlineExceededError:
            state = tracker.mark_unknown_if_started()
            error = ToolExecutionError(
                invocation_id=invocation.invocation_id,
                attempt_id=attempt_id,
                tool_name=spec.tool_name,
                category=ToolErrorCategory.DEADLINE_EXCEEDED,
                safe_error_code="TOOL_DEADLINE_EXCEEDED",
                safe_message="Tool 调用截止时间已到期。",
                phase=(
                    ToolExecutionPhase.INVOCATION
                    if provider_started
                    else ToolExecutionPhase.RESOURCE_WAIT
                ),
                provider_started=provider_started,
                side_effect_state=state,
                retry_disposition=(
                    RetryDisposition.OUTCOME_UNKNOWN
                    if state == ToolSideEffectState.UNKNOWN
                    else RetryDisposition.UNSAFE
                ),
                retry_index=retry_index,
                status=ToolExecutionStatus.TIMED_OUT,
            )
            if started_event_emitted:
                error = await self._emit_completed(
                    event_emitter,
                    spec=spec,
                    invocation=invocation,
                    error=error,
                    duration_ms=elapsed_ms(),
                )
            raise ToolAttemptFailed(error) from None
        except ToolAttemptFailed:
            raise
        except BaseException:
            state = tracker.mark_unknown_if_started()
            error = ToolExecutionError(
                invocation_id=invocation.invocation_id,
                attempt_id=attempt_id,
                tool_name=spec.tool_name,
                category=ToolErrorCategory.INTERNAL,
                safe_error_code="TOOL_INTERNAL_ERROR",
                safe_message="Tool Runtime 内部失败。",
                phase=ToolExecutionPhase.INVOCATION,
                provider_started=provider_started,
                side_effect_state=state,
                retry_disposition=(
                    RetryDisposition.OUTCOME_UNKNOWN
                    if state == ToolSideEffectState.UNKNOWN
                    else RetryDisposition.UNSAFE
                ),
                retry_index=retry_index,
            )
            if started_event_emitted:
                error = await self._emit_completed(
                    event_emitter,
                    spec=spec,
                    invocation=invocation,
                    error=error,
                    duration_ms=elapsed_ms(),
                )
            raise ToolAttemptFailed(error) from None
        finally:
            if reservation is not None:
                budget_ledger.release(reservation)
            if lease is not None and not release_deferred["value"]:
                lease.release()
                concurrency_controller.complete_worker(attempt_id)

    async def _invoke_adapter(
        self,
        *,
        adapter: ToolAdapter,
        invocation: ToolInvocation,
        context: ToolExecutionContext,
        attempt_source: CancellationSource,
        lease: ToolResourceLease,
        release_deferred: dict[str, bool],
    ) -> ToolAdapterResponse:
        if adapter.is_async:
            value = adapter.invoke_once(invocation, context)
            if not inspect.isawaitable(value):
                raise ToolAdapterInvocationError(
                    category=ToolErrorCategory.INTERNAL,
                    safe_error_code="ASYNC_TOOL_ADAPTER_INVALID",
                    safe_message="Async Tool Adapter 返回值无效。",
                )
            task = asyncio.create_task(value)
            return await self._await_async_task(task, context)
        future = self._sync_executor.submit(adapter.invoke_once, invocation, context)
        context.concurrency_controller.register_worker(
            invocation_id=invocation.invocation_id,
            attempt_id=context.attempt_id,
            started_at=datetime.now(UTC),
            tool_name=invocation.tool_name,
            resource_key_digest=safe_key_digest(invocation.resource_key),
        )
        return await self._await_sync_future(
            future=future,
            context=context,
            attempt_source=attempt_source,
            lease=lease,
            release_deferred=release_deferred,
        )

    async def _await_async_task(
        self, task: asyncio.Task[ToolAdapterResponse], context: ToolExecutionContext
    ) -> ToolAdapterResponse:
        try:
            while not task.done():
                context.raise_if_cancelled()
                remaining = context.remaining_seconds()
                if remaining <= 0:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise _ToolTimedOut(None, lingering=False)
                await asyncio.wait((task,), timeout=min(0.01, remaining))
            return await task
        except (RunCancelledError, RunDeadlineExceededError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _await_sync_future(
        self,
        *,
        future: Future[ToolAdapterResponse],
        context: ToolExecutionContext,
        attempt_source: CancellationSource,
        lease: ToolResourceLease,
        release_deferred: dict[str, bool],
    ) -> ToolAdapterResponse:
        wrapped = asyncio.wrap_future(future)
        while not future.done():
            try:
                context.raise_if_cancelled()
            except RunCancelledError:
                attempt_source.cancel(
                    context.run_context.cancellation_token.reason
                    or CancellationReason.USER_CANCELLED
                )
                await self._wait_sync_grace(wrapped)
                if not future.done():
                    self._defer_lease_release(
                        future, lease, release_deferred, context
                    )
                else:
                    await asyncio.gather(wrapped, return_exceptions=True)
                raise
            except RunDeadlineExceededError:
                attempt_source.cancel(CancellationReason.DEADLINE_EXCEEDED)
                await self._wait_sync_grace(wrapped)
                if future.done():
                    response = await _wrapped_result_or_none(wrapped)
                    raise _ToolTimedOut(response, lingering=False)
                self._defer_lease_release(
                    future, lease, release_deferred, context
                )
                raise _ToolTimedOut(None, lingering=True)
            remaining = context.remaining_seconds()
            if remaining <= 0:
                attempt_source.cancel(CancellationReason.DEADLINE_EXCEEDED)
                await self._wait_sync_grace(wrapped)
                if future.done():
                    response = await _wrapped_result_or_none(wrapped)
                    raise _ToolTimedOut(response, lingering=False)
                self._defer_lease_release(
                    future, lease, release_deferred, context
                )
                raise _ToolTimedOut(None, lingering=True)
            await asyncio.wait((wrapped,), timeout=min(0.01, remaining))
        return await wrapped

    async def _wait_sync_grace(self, wrapped: asyncio.Future) -> None:
        if self.sync_timeout_grace_seconds <= 0:
            return
        await asyncio.wait(
            (wrapped,), timeout=self.sync_timeout_grace_seconds
        )

    @staticmethod
    def _defer_lease_release(
        future: Future,
        lease: ToolResourceLease,
        release_deferred: dict[str, bool],
        context: ToolExecutionContext,
    ) -> None:
        if release_deferred["value"]:
            return
        release_deferred["value"] = True
        context.concurrency_controller.mark_worker_detached(context.attempt_id)
        activity_tracker = context.run_context.activity_tracker
        if activity_tracker is not None:
            activity_tracker.increment("detached_tool_workers")

        def cleanup(_done: Future) -> None:
            # Detached Worker 后续只释放全部 Permit 并注销安全 Tracker，
            # 不发布第二个 Completed，也不接触 Run/Step 状态。
            lease.release()
            context.concurrency_controller.complete_worker(context.attempt_id)
            if activity_tracker is not None:
                activity_tracker.decrement("detached_tool_workers")

        future.add_done_callback(cleanup)

    @staticmethod
    def _adapter_error(
        *,
        invocation: ToolInvocation,
        attempt_id: str,
        retry_index: int,
        spec: ToolExecutionSpec,
        tracker: AttemptSideEffectTracker,
        provider_started: bool,
        exc: ToolAdapterInvocationError,
    ) -> ToolExecutionError:
        disposition = retry_disposition_for(
            category=exc.category,
            idempotency=spec.idempotency,
            idempotency_key=invocation.idempotency_key,
            side_effect_state=tracker.state,
            compensation_attempted=exc.compensation_attempted,
            compensation_succeeded=exc.compensation_succeeded,
            supports_idempotency_replay=spec.supports_idempotency_replay,
            output_started=exc.output_started,
        )
        return ToolExecutionError(
            invocation_id=invocation.invocation_id,
            attempt_id=attempt_id,
            tool_name=spec.tool_name,
            category=exc.category,
            safe_error_code=exc.safe_error_code,
            safe_message=exc.safe_message,
            phase=exc.phase,
            provider_started=provider_started,
            side_effect_state=tracker.state,
            retry_disposition=disposition,
            partial_result=exc.partial_result,
            compensation_attempted=exc.compensation_attempted,
            compensation_succeeded=exc.compensation_succeeded,
            retry_index=retry_index,
            output_started=exc.output_started,
        )

    @staticmethod
    async def _emit_completed(
        event_emitter: StepEventEmitter | None,
        *,
        spec: ToolExecutionSpec,
        invocation: ToolInvocation,
        result: ToolExecutionResult | None = None,
        error: ToolExecutionError | None = None,
        duration_ms: int | None = None,
    ):
        if event_emitter is None:
            return result if result is not None else error
        try:
            if result is not None:
                payload = ToolCompletedPayload(
                    tool_name=result.tool_name,
                    succeeded=True,
                    retry_index=result.retry_index,
                    side_effect_state=result.side_effect_state.value,
                    retry_disposition=result.retry_disposition.value,
                    worker_terminated=result.worker_terminated,
                    execution_detached=result.execution_detached,
                    resource_release_pending=result.resource_release_pending,
                    duration_ms=result.duration_ms,
                    status=result.status.value,
                    tool_evidence_schema_version=TOOL_EVIDENCE_SCHEMA_VERSION,
                    invocation_identity_digest=safe_key_digest(
                        result.invocation_id
                    ),
                    attempt_identity_digest=safe_key_digest(result.attempt_id),
                    side_effect_kind=spec.side_effect_kind.value,
                    idempotency_kind=spec.idempotency.value,
                    idempotency_key_digest=safe_key_digest(
                        invocation.idempotency_key
                    ),
                    replay_supported=spec.supports_idempotency_replay,
                    compensation_state="NOT_ATTEMPTED",
                    outcome_classification=result.status.value,
                    provider_started=True,
                )
            else:
                assert error is not None
                if duration_ms is None:
                    raise ValueError("Tool error Completed 必须携带 duration_ms")
                payload = ToolCompletedPayload(
                    tool_name=error.tool_name,
                    succeeded=False,
                    safe_error_code=error.safe_error_code,
                    retry_index=error.retry_index,
                    side_effect_state=error.side_effect_state.value,
                    retry_disposition=error.retry_disposition.value,
                    worker_terminated=error.worker_terminated,
                    execution_detached=error.execution_detached,
                    resource_release_pending=error.resource_release_pending,
                    duration_ms=duration_ms,
                    status=error.status.value,
                    tool_evidence_schema_version=TOOL_EVIDENCE_SCHEMA_VERSION,
                    invocation_identity_digest=safe_key_digest(
                        error.invocation_id
                    ),
                    attempt_identity_digest=safe_key_digest(error.attempt_id),
                    side_effect_kind=spec.side_effect_kind.value,
                    idempotency_kind=spec.idempotency.value,
                    idempotency_key_digest=safe_key_digest(
                        invocation.idempotency_key
                    ),
                    replay_supported=spec.supports_idempotency_replay,
                    compensation_state=(
                        "SUCCEEDED"
                        if error.compensation_attempted
                        and error.compensation_succeeded
                        else (
                            "FAILED"
                            if error.compensation_attempted
                            else "NOT_ATTEMPTED"
                        )
                    ),
                    outcome_classification=error.category.value,
                    provider_started=error.provider_started,
                )
            await event_emitter.emit(
                RuntimeEventType.TOOL_COMPLETED,
                payload,
                component="tool_attempt_executor",
                ignore_run_cancellation=True,
            )
            return result if result is not None else error
        except JournalError:
            raise
        except BaseException:
            # Completed 发布失败发生在 Tool 已执行之后，必须保守停止，绝不透明重试。
            source = result if result is not None else error
            assert source is not None
            return ToolExecutionError(
                invocation_id=source.invocation_id,
                attempt_id=source.attempt_id,
                tool_name=source.tool_name,
                category=ToolErrorCategory.INTERNAL,
                safe_error_code="TOOL_COMPLETED_EVENT_FAILED",
                safe_message="Tool 完成事件发布失败。",
                phase=ToolExecutionPhase.EVENT,
                provider_started=True,
                side_effect_state=source.side_effect_state,
                retry_disposition=RetryDisposition.UNSAFE,
                retry_index=source.retry_index,
                worker_terminated=source.worker_terminated,
                execution_detached=source.execution_detached,
                resource_release_pending=source.resource_release_pending,
            )


class ToolExecutionService:
    """通过既有 RetryExecutor 执行安全 Attempt；不解析 Planner 自由文本。"""

    def __init__(
        self,
        *,
        concurrency_controller: ToolConcurrencyController | None = None,
        retry_executor: RetryExecutor | None = None,
        attempt_executor: ToolAttemptExecutor | None = None,
        span_recorder=None,
    ) -> None:
        self.concurrency_controller = (
            concurrency_controller or ToolConcurrencyController()
        )
        self.retry_executor = retry_executor or RetryExecutor(
            RetryPolicy(base_delay_seconds=0, max_delay_seconds=0)
        )
        self.attempt_executor = attempt_executor or ToolAttemptExecutor()
        self.span_recorder = span_recorder

    async def execute(
        self,
        *,
        invocation: ToolInvocation,
        adapter: ToolAdapter,
        run_context: RunContext,
        step_id: str,
        event_emitter: StepEventEmitter | None = None,
        fault_controller: FaultInjectionController | None = None,
    ) -> ToolExecutionResult | ToolExecutionError:
        recorder = self.span_recorder or current_span_recorder() or NoopSpanRecorder()
        handle = start_span_safely(
            recorder,
            trace_id=run_context.trace_id,
            run_id=run_context.run_id,
            component="tool_invocation",
            operation="invoke",
            step_id=step_id,
        )
        if handle.context is not None:
            handle.set_safe_attribute("tool_name", invocation.tool_name)
        token = install_trace_context(handle.context)
        recorder_token = install_span_recorder(recorder)
        try:
            result = await self._execute_impl(
                invocation=invocation,
                adapter=adapter,
                run_context=run_context,
                step_id=step_id,
                event_emitter=event_emitter,
                fault_controller=fault_controller,
            )
            if isinstance(result, ToolExecutionError):
                if result.status is ToolExecutionStatus.TIMED_OUT:
                    handle.end_timed_out(result.safe_error_code)
                elif result.status is ToolExecutionStatus.CANCELLED:
                    handle.end_cancelled(result.safe_error_code)
                else:
                    handle.end_error(result.safe_error_code)
            else:
                handle.end_ok()
            return result
        except RunCancelledError:
            handle.end_cancelled("RUN_CANCELLED")
            raise
        except (RunDeadlineExceededError, TimeoutError):
            handle.end_timed_out()
            raise
        except BaseException:
            handle.end_error()
            raise
        finally:
            reset_trace_context(token)
            reset_span_recorder(recorder_token)

    async def _execute_impl(
        self,
        *,
        invocation: ToolInvocation,
        adapter: ToolAdapter,
        run_context: RunContext,
        step_id: str,
        event_emitter: StepEventEmitter | None = None,
        fault_controller: FaultInjectionController | None = None,
    ) -> ToolExecutionResult | ToolExecutionError:
        ledger = run_context.budget_ledger
        if not isinstance(ledger, BudgetLedger):
            raise RuntimeError("Tool Execution 需要 RunContext 已绑定 BudgetLedger")
        try:
            spec = adapter.spec_for(invocation)
            _validate_invocation_against_spec(invocation, spec)
        except ToolAdapterInvocationError as exc:
            return ToolExecutionError(
                invocation_id=invocation.invocation_id,
                attempt_id=None,
                tool_name=invocation.tool_name,
                category=exc.category,
                safe_error_code=exc.safe_error_code,
                safe_message=exc.safe_message,
                phase=exc.phase,
                provider_started=False,
                side_effect_state=ToolSideEffectState.NOT_STARTED,
                retry_disposition=RetryDisposition.UNSAFE,
            )
        except (TypeError, ValueError):
            return ToolExecutionError(
                invocation_id=invocation.invocation_id,
                attempt_id=None,
                tool_name=invocation.tool_name,
                category=ToolErrorCategory.VALIDATION,
                safe_error_code="TOOL_CONTRACT_VALIDATION_FAILED",
                safe_message="Tool contract validation failed.",
                phase=ToolExecutionPhase.VALIDATION,
                provider_started=False,
                side_effect_state=ToolSideEffectState.NOT_STARTED,
                retry_disposition=RetryDisposition.UNSAFE,
            )

        try:
            await _execute_tool_fault_point(
                fault_controller,
                FaultPoint.TOOL_BEFORE_INVOCATION,
                run_context=run_context,
                invocation=invocation,
            )
        except InjectedFaultError as exc:
            return _tool_injected_error(
                exc,
                invocation=invocation,
                spec=spec,
                attempt_id=None,
                retry_index=0,
            )

        last_error: ToolExecutionError | None = None

        async def attempt(retry_index: int) -> ToolExecutionResult:
            nonlocal last_error
            try:
                await _execute_tool_fault_point(
                    fault_controller,
                    FaultPoint.TOOL_BEFORE_ATTEMPT,
                    run_context=run_context,
                    invocation=invocation,
                    attempt_number=retry_index + 1,
                )
                return await self.attempt_executor.execute(
                    invocation=invocation,
                    adapter=adapter,
                    spec=spec,
                    run_context=run_context,
                    budget_ledger=ledger,
                    concurrency_controller=self.concurrency_controller,
                    step_id=step_id,
                    retry_index=retry_index,
                    event_emitter=event_emitter,
                    fault_controller=fault_controller,
                )
            except InjectedFaultError as exc:
                last_error = _tool_injected_error(
                    exc,
                    invocation=invocation,
                    spec=spec,
                    attempt_id=None,
                    retry_index=retry_index,
                )
                raise ToolAttemptFailed(last_error) from None
            except ToolAttemptFailed as failed:
                last_error = failed.error
                raise

        def category_of(exc: BaseException) -> ModelFailureCategory:
            if isinstance(exc, ToolAttemptFailed):
                return _model_category(exc.error.category)
            return ModelFailureCategory.UNKNOWN_FAILURE

        def should_retry(
            category: ModelFailureCategory, retry_index: int
        ) -> RetryDecision:
            assert last_error is not None
            decision = self.retry_executor.decide(
                category=category,
                retry_index=retry_index,
                output_started=False,
                remaining_seconds=run_context.remaining_seconds(),
            )
            if last_error.retry_disposition not in {
                RetryDisposition.SAFE,
                RetryDisposition.SAFE_WITH_IDEMPOTENCY_KEY,
            }:
                return RetryDecision(
                    False,
                    "TOOL_RETRY_UNSAFE",
                    retry_index,
                    decision.delay_seconds,
                    decision.required_budget,
                    decision.required_time_seconds,
                )
            return decision

        try:
            return await self.retry_executor.execute_async(
                attempt,
                category_of=category_of,
                should_retry=should_retry,
                raise_if_cancelled=run_context.raise_if_inactive,
            )
        except RunCancelledError:
            raise
        except ToolAttemptFailed as failed:
            return failed.error

    def execute_sync(
        self,
        *,
        invocation: ToolInvocation,
        adapter: ToolAdapter,
        run_context: RunContext,
        step_id: str,
        event_emitter: StepEventEmitter | None = None,
        fault_controller: FaultInjectionController | None = None,
    ) -> ToolExecutionResult | ToolExecutionError:
        coroutine = self.execute(
            invocation=invocation,
            adapter=adapter,
            run_context=run_context,
            step_id=step_id,
            event_emitter=event_emitter,
            fault_controller=fault_controller,
        )
        if event_emitter is not None:
            loop = event_emitter.parent._loop
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                coroutine.close()
                raise RuntimeError("Owner Event Loop 不能同步等待 ToolExecutionService")
            return asyncio.run_coroutine_threadsafe(coroutine, loop).result()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        result: list[ToolExecutionResult | ToolExecutionError] = []
        failure: list[BaseException] = []

        def runner() -> None:
            try:
                result.append(asyncio.run(coroutine))
            except BaseException as exc:
                failure.append(exc)

        thread = threading.Thread(target=runner, name="tool-execution-sync")
        thread.start()
        thread.join()
        if failure:
            raise failure[0]
        return result[0]


async def _execute_tool_fault_point(
    controller: FaultInjectionController | None,
    point: FaultPoint,
    *,
    run_context: RunContext,
    invocation: ToolInvocation,
    attempt_number: int | None = None,
    raise_if_cancelled: Callable[[], None] | None = None,
    remaining_seconds: Callable[[], float | None] | None = None,
) -> None:
    """Run one request-scoped pre-call seam without owning retry or tool state."""
    if controller is None or not controller.enabled:
        return
    context = FaultMatchContext(
        fault_point=point,
        component="tool",
        run_id_digest=safe_key_digest(run_context.run_id),
        invocation_id_digest=safe_key_digest(invocation.invocation_id),
        attempt_number=attempt_number,
    )
    task = asyncio.create_task(
        controller.execute_if_matched(
            context,
            allowed_actions={
                FaultAction.RAISE_TYPED_ERROR,
                FaultAction.DELAY,
                FaultAction.BLOCK_UNTIL_RELEASED,
            },
        )
    )
    check_inactive = raise_if_cancelled or run_context.raise_if_inactive
    get_remaining = remaining_seconds or run_context.remaining_seconds
    try:
        while not task.done():
            check_inactive()
            remaining = get_remaining()
            if remaining is not None and remaining <= 0:
                check_inactive()
                raise RunDeadlineExceededError(
                    "Tool fault seam deadline expired"
                )
            poll_seconds = 0.01 if remaining is None else min(0.01, remaining)
            done, _ = await asyncio.wait((task,), timeout=poll_seconds)
            if done:
                break
        result = await task
    except BaseException:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    if isinstance(result, InjectedFailureResult):
        raise RuntimeError("TOOL_INJECTED_ACTION_UNSUPPORTED")


def _tool_blocking_fault_callback(
    controller: FaultInjectionController | None,
    point: FaultPoint,
    *,
    run_context: RunContext,
    invocation: ToolInvocation,
    attempt_number: int,
    raise_if_cancelled: Callable[[], None],
) -> Callable[[], None] | None:
    """Build the synchronous Adapter-owned seam without owning tool state."""
    if controller is None or not controller.enabled:
        return None

    def execute() -> None:
        result = controller.execute_blocking_if_matched(
            FaultMatchContext(
                fault_point=point,
                component="tool",
                run_id_digest=safe_key_digest(run_context.run_id),
                invocation_id_digest=safe_key_digest(invocation.invocation_id),
                attempt_number=attempt_number,
                side_effect_phase="BEFORE_COMMIT",
            ),
            raise_if_cancelled=raise_if_cancelled,
            allowed_actions={
                FaultAction.RAISE_TYPED_ERROR,
                FaultAction.DELAY,
                FaultAction.BLOCK_UNTIL_RELEASED,
            },
        )
        if isinstance(result, InjectedFailureResult):
            raise RuntimeError("TOOL_INJECTED_ACTION_UNSUPPORTED")

    return execute


def _tool_injected_error(
    exc: InjectedFaultError,
    *,
    invocation: ToolInvocation,
    spec: ToolExecutionSpec,
    attempt_id: str | None,
    retry_index: int,
    provider_started: bool = False,
    side_effect_state: ToolSideEffectState = ToolSideEffectState.NOT_STARTED,
    post_provider: bool = False,
) -> ToolExecutionError:
    """Map controller codes at the Tool seam; never expose rule or input data."""
    if exc.code is InjectedFaultCode.INJECTED_TRANSIENT_FAILURE:
        category = ToolErrorCategory.TRANSIENT
        safe_error_code = "TOOL_INJECTED_TRANSIENT_FAILURE"
        safe_message = "Tool pre-call transient failure."
        status = ToolExecutionStatus.FAILED
    elif exc.code is InjectedFaultCode.INJECTED_TIMEOUT:
        category = ToolErrorCategory.TIMEOUT
        safe_error_code = "TOOL_INJECTED_TIMEOUT"
        safe_message = "Tool pre-call timed out."
        status = ToolExecutionStatus.TIMED_OUT
    elif exc.code is InjectedFaultCode.INJECTED_PERMANENT_FAILURE:
        category = ToolErrorCategory.INTERNAL
        safe_error_code = "TOOL_INJECTED_PERMANENT_FAILURE"
        safe_message = "Tool pre-call permanent failure."
        status = ToolExecutionStatus.FAILED
    else:
        category = ToolErrorCategory.INTERNAL
        safe_error_code = "TOOL_INJECTED_FAULT_UNSUPPORTED"
        safe_message = "Tool pre-call injected fault is unsupported."
        status = ToolExecutionStatus.FAILED
    if post_provider:
        category = ToolErrorCategory.POST_COMMIT_RESPONSE_FAILURE
        safe_error_code = "TOOL_POST_PROVIDER_FAILURE"
        safe_message = "Tool provider returned but Runtime completion failed."
        status = ToolExecutionStatus.FAILED
    elif provider_started:
        safe_message = safe_message.replace("pre-call", "provider boundary")
    disposition = retry_disposition_for(
        category=category,
        idempotency=spec.idempotency,
        idempotency_key=invocation.idempotency_key,
        side_effect_state=side_effect_state,
        supports_idempotency_replay=spec.supports_idempotency_replay,
    )
    return ToolExecutionError(
        invocation_id=invocation.invocation_id,
        attempt_id=attempt_id,
        tool_name=spec.tool_name,
        category=category,
        safe_error_code=safe_error_code,
        safe_message=safe_message,
        phase=ToolExecutionPhase.INVOCATION,
        provider_started=provider_started,
        side_effect_state=side_effect_state,
        retry_disposition=disposition,
        retry_index=retry_index,
        status=status,
    )


def _validate_invocation_against_spec(
    invocation: ToolInvocation, spec: ToolExecutionSpec
) -> None:
    if invocation.tool_name != spec.tool_name:
        raise ValueError("Invocation 与 Spec 的 tool_name 不一致")
    if spec.requires_resource_key and not invocation.resource_key:
        raise ValueError("该 Tool 必须提供稳定 Resource Key")
    if (
        spec.idempotency.value == "IDEMPOTENT_WITH_KEY"
        and not invocation.idempotency_key
    ):
        raise ValueError("IDEMPOTENT_WITH_KEY 必须提供稳定非空 Idempotency Key")


def _effective_timeout(
    invocation: ToolInvocation, spec: ToolExecutionSpec, run_context: RunContext
) -> float:
    values = [spec.default_timeout_seconds]
    if invocation.requested_timeout_seconds is not None:
        values.append(invocation.requested_timeout_seconds)
    run_remaining = run_context.remaining_seconds()
    if run_remaining is not None:
        values.append(run_remaining)
    timeout = min(values)
    if timeout <= 0:
        raise RunDeadlineExceededError("Tool 调用前 Deadline 已到期")
    return timeout


def _raise_if_tool_attempt_inactive(
    run_context: RunContext,
    attempt_token: CancellationToken,
    effective_deadline_monotonic: float,
) -> None:
    run_context.raise_if_inactive()
    attempt_token.raise_if_cancelled()
    if time.monotonic() >= effective_deadline_monotonic:
        raise RunDeadlineExceededError("Tool Attempt 截止时间已到期")


async def _wrapped_result_or_none(
    future: asyncio.Future[ToolAdapterResponse],
) -> ToolAdapterResponse | None:
    try:
        return await future
    except BaseException:
        return None


def _model_category(category: ToolErrorCategory) -> ModelFailureCategory:
    if category == ToolErrorCategory.TRANSIENT:
        return ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE
    if category == ToolErrorCategory.POST_COMMIT_RESPONSE_FAILURE:
        return ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE
    if category == ToolErrorCategory.TIMEOUT:
        return ModelFailureCategory.PROVIDER_TIMEOUT
    if category == ToolErrorCategory.RESOURCE_CONFLICT:
        return ModelFailureCategory.RATE_LIMITED
    return ModelFailureCategory.UNKNOWN_FAILURE


__all__ = [
    "AttemptSideEffectTracker",
    "ToolAttemptExecutor",
    "ToolExecutionContext",
    "ToolExecutionFailed",
    "ToolExecutionService",
]
