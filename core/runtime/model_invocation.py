#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一的模型调用、预算结算、Fallback 与 Circuit 协调边界。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import asyncio
import inspect
import time
from typing import Awaitable, Callable, Mapping, Protocol, Sequence

from core.runtime.budget import (
    BudgetExceededError,
    BudgetLedger,
    BudgetUsage,
    UsageSource,
)
from core.runtime.cancellation import RunCancelledError
from core.runtime.circuit_breaker import (
    CircuitOpenError,
    ModelCircuitBreakerRegistry,
)
from core.runtime.context import RunContext, RunDeadlineExceededError
from core.runtime.model_routing import (
    ModelFailureCategory,
    ModelRoutingCandidate,
    ModelRoutingDecision,
    ModelRoutingPolicy,
    RoutingAdjustment,
)
from core.runtime.model_selection import ModelProfileId
from core.runtime.retry import RetryExecutor, RetryPolicy
from core.runtime.event_emitter import RunEventEmitter, StepEventEmitter
from core.runtime.execution_aggregate import ModelAttemptState, canonical_payload_digest
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
from core.runtime.events import (
    ModelCompletedPayload,
    ModelStartedPayload,
    RuntimeEventType,
)
from core.runtime.trace_contract import set_span_attributes
from core.runtime.tracing import (
    NoopSpanRecorder,
    current_span_recorder,
    install_span_recorder,
    install_trace_context,
    reset_span_recorder,
    reset_trace_context,
    start_span_safely,
)


class ModelUsageSource(str, Enum):
    ACTUAL = "ACTUAL"
    ESTIMATED = "ESTIMATED"


class CircuitHealthOutcome(str, Enum):
    """Routing 结果映射到独立的 Circuit 健康结论。"""

    NOT_STARTED = "NOT_STARTED"
    HEALTHY_COMPLETION = "HEALTHY_COMPLETION"
    QUALIFYING_PROVIDER_FAILURE = "QUALIFYING_PROVIDER_FAILURE"
    INDETERMINATE_COMPLETION = "INDETERMINATE_COMPLETION"


@dataclass(frozen=True, slots=True)
class NativeToolCall:
    """Provider native tool call 的窄化内部表示。"""

    provider_tool_call_id: str
    tool_name: str
    arguments_json: str


@dataclass(frozen=True, slots=True)
class ModelAdapterResponse:
    output: str
    actual_usage: BudgetUsage | None = None
    native_tool_call: NativeToolCall | None = None
    assistant_message: Mapping[str, object] | None = None


class ModelAdapter(Protocol):
    """一个 Adapter 只执行一个 Profile 的一次调用。"""

    def invoke(
        self, messages: Sequence[Mapping[str, str]], *, max_tokens: int
    ) -> ModelAdapterResponse: ...

    def supports_native_tool_calling(self) -> bool: ...

    async def ainvoke(
        self, messages: Sequence[Mapping[str, str]], *, max_tokens: int
    ) -> ModelAdapterResponse: ...


class ModelAdapterInvocationError(RuntimeError):
    """Adapter 将 Provider 异常转换为安全属性，不保留原始正文。"""

    def __init__(
        self,
        category: ModelFailureCategory,
        *,
        safe_error_code: str | None = None,
        provider_started: bool = True,
        provider_responded: bool | None = None,
        output_started: bool = False,
    ) -> None:
        self.model_failure_category = category
        self.safe_error_code = safe_error_code or category.value
        self.provider_started = provider_started
        self.provider_responded = provider_responded
        self.output_started = output_started
        super().__init__("模型 Adapter 调用失败")


class GeneratorModelAdapter:
    """将既有 ``generate`` 引擎适配为一次非流式调用。"""

    def __init__(self, engine: object) -> None:
        self._engine = engine

    def supports_native_tool_calling(self) -> bool:
        """只接受 Engine 显式声明的 native function calling 能力。"""
        capability = getattr(self._engine, "supports_native_tool_calling", None)
        return bool(capability()) if callable(capability) else False

    def invoke(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        max_tokens: int,
        on_started: Callable[[], None] | None = None,
        generation_options: Mapping[str, object] | None = None,
        run_context: RunContext | None = None,
        on_delta: Callable[[], None] | None = None,
    ) -> ModelAdapterResponse:
        chunks: list[str] = []
        provider_started = False
        try:
            provider_started = True
            if on_started is not None:
                on_started()
            if generation_options and "tools" in generation_options:
                if not self.supports_native_tool_calling():
                    raise ModelAdapterInvocationError(
                        ModelFailureCategory.PROVIDER_CONFIGURATION_ERROR,
                        safe_error_code="NATIVE_TOOL_CALLING_UNSUPPORTED",
                        provider_started=False,
                        provider_responded=False,
                    )
                return self._engine.generate_native(
                    list(messages), max_tokens=max_tokens,
                    **self._engine_options(
                        generation_options, run_context, self._engine.generate_native
                    ),
                )
            stream = self._engine.generate(
                list(messages),
                max_tokens=max_tokens,
                **self._engine_options(
                    generation_options or {}, run_context, self._engine.generate
                ),
            )
            for chunk in stream:
                if chunk:
                    chunks.append(str(chunk))
                    if on_delta is not None:
                        on_delta()
        except Exception as exc:
            category = classify_model_failure(exc)
            raise ModelAdapterInvocationError(
                category,
                safe_error_code=_safe_error_code(exc, category),
                provider_started=bool(
                    getattr(exc, "provider_started", provider_started)
                ),
                provider_responded=getattr(exc, "provider_responded", None),
                output_started=bool(chunks)
                or bool(getattr(exc, "output_started", False)),
            ) from None
        return ModelAdapterResponse("".join(chunks))

    @staticmethod
    def _engine_options(
        options: Mapping[str, object],
        run_context: RunContext | None,
        callable_object: Callable[..., object],
    ) -> dict[str, object]:
        values = dict(options)
        if run_context is not None:
            try:
                parameters = inspect.signature(callable_object).parameters
            except (TypeError, ValueError):
                parameters = {}
            if "run_context" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            ):
                values["run_context"] = run_context
        return values

    async def ainvoke(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        max_tokens: int,
        on_started: Callable[[], None] | None = None,
        generation_options: Mapping[str, object] | None = None,
        run_context: RunContext | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> ModelAdapterResponse:
        """Async provider path; no synchronous HTTP is used here."""
        options = dict(generation_options or {})
        accepted_output = False
        try:
            if on_started is not None:
                on_started()
            if "tools" in options:
                if not self.supports_native_tool_calling():
                    raise ModelAdapterInvocationError(
                        ModelFailureCategory.PROVIDER_CONFIGURATION_ERROR,
                        safe_error_code="NATIVE_TOOL_CALLING_UNSUPPORTED",
                        provider_started=False,
                        provider_responded=False,
                    )
                native = getattr(self._engine, "agenerate_native", None)
                if callable(native):
                    return await native(
                        list(messages),
                        max_tokens=max_tokens,
                        run_context=run_context,
                        **options,
                    )
                return await asyncio.to_thread(
                    self.invoke,
                    messages,
                    max_tokens=max_tokens,
                    generation_options=options,
                    run_context=run_context,
                )
            native_stream = getattr(self._engine, "agenerate", None)
            if not callable(native_stream):
                response = await asyncio.to_thread(
                    self.invoke,
                    messages,
                    max_tokens=max_tokens,
                    generation_options=options,
                    run_context=run_context,
                )
                if response.output and on_delta is not None:
                    await on_delta(response.output)
                    accepted_output = True
                return response
            chunks: list[str] = []
            actual_usage = None
            from core.llm_engine import TextDelta, UsageDelta

            async for delta in native_stream(
                [dict(message) for message in messages],
                max_tokens=max_tokens,
                run_context=run_context,
                **options,
            ):
                if isinstance(delta, TextDelta):
                    chunks.append(delta.text)
                    if on_delta is not None:
                        await on_delta(delta.text)
                        accepted_output = True
                elif isinstance(delta, UsageDelta):
                    from core.runtime.budget import BudgetUsage

                    actual_usage = BudgetUsage(
                        input_tokens=delta.input_tokens,
                        output_tokens=delta.output_tokens,
                        total_tokens=delta.input_tokens + delta.output_tokens,
                    )
            return ModelAdapterResponse("".join(chunks), actual_usage=actual_usage)
        except Exception as exc:
            if isinstance(exc, ModelAdapterInvocationError):
                raise
            category = classify_model_failure(exc)
            raise ModelAdapterInvocationError(
                category,
                safe_error_code=_safe_error_code(exc, category),
                provider_started=bool(getattr(exc, "provider_started", True)),
                provider_responded=getattr(exc, "provider_responded", None),
                # Provider 看到/生成 delta 不等于 Runtime 已接纳输出。这里只
                # 传播成功完成 Runtime acceptance callback 的单调事实。
                output_started=accepted_output,
            ) from None


class ModelAdapterResolver:
    """显式执行 ``profile_id -> ModelAdapter`` 映射。"""

    def __init__(self, adapters: Mapping[ModelProfileId, ModelAdapter]) -> None:
        self._adapters = dict(adapters)

    def resolve(self, profile_id: ModelProfileId) -> ModelAdapter:
        try:
            return self._adapters[profile_id]
        except KeyError as exc:
            raise ModelAdapterResolutionError(profile_id) from exc


class ModelAdapterResolutionError(LookupError):
    error_code = "MODEL_ADAPTER_NOT_CONFIGURED"

    def __init__(self, profile_id: ModelProfileId) -> None:
        self.profile_id = profile_id
        self.provider_started = False
        self.safe_error_code = self.error_code
        super().__init__("所选 Profile 没有显式 Model Adapter")


@dataclass(frozen=True, slots=True)
class ModelInvocationAttempt:
    attempt_index: int
    profile_id: ModelProfileId
    breaker_key: str
    started: bool
    succeeded: bool
    failure_category: ModelFailureCategory | None
    safe_error_code: str | None
    routing_adjustment: RoutingAdjustment
    usage_source: ModelUsageSource | None
    # 新字段保持旧 Snapshot 的位置兼容，且只保存安全元数据。
    candidate_index: int = 0
    retry_index: int = 0
    backoff_before_seconds: float = 0.0
    circuit_state: str | None = None
    provider_kind: str = ""
    model_identity: str = ""


@dataclass(frozen=True, slots=True)
class ModelInvocationResult:
    output: str
    capability_preferred_profile_id: ModelProfileId | None
    initial_selected_profile_id: ModelProfileId | None
    executed_profile_id: ModelProfileId
    attempts: tuple[ModelInvocationAttempt, ...]
    quality_tradeoff_disclosed: bool
    response: ModelAdapterResponse | None = None
    prompt_id: str = ""
    prompt_version: str = ""
    prompt_digest: str = ""
    provider_kind: str = ""
    model_identity: str = ""
    estimated_input_tokens: int = 0
    reserved_output_tokens: int = 0
    context_budget_utilization: float = 0.0
    selected_context_items: int = 0
    dropped_context_items: int = 0
    structured_repair_count: int = 0

    def __post_init__(self) -> None:
        for value, name in (
            (self.prompt_id, "prompt_id"),
            (self.prompt_version, "prompt_version"),
            (self.prompt_digest, "prompt_digest"),
            (self.provider_kind, "provider_kind"),
            (self.model_identity, "model_identity"),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{name} 必须是字符串")
        for value, name in (
            (self.estimated_input_tokens, "estimated_input_tokens"),
            (self.reserved_output_tokens, "reserved_output_tokens"),
            (self.selected_context_items, "selected_context_items"),
            (self.dropped_context_items, "dropped_context_items"),
            (self.structured_repair_count, "structured_repair_count"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if not 0.0 <= self.context_budget_utilization <= 1.0:
            raise ValueError("context_budget_utilization 必须在 [0,1]")


@dataclass(frozen=True, slots=True)
class ModelInvocationFailure:
    capability_preferred_profile_id: ModelProfileId | None
    initial_selected_profile_id: ModelProfileId | None
    executed_profile_id: None
    attempts: tuple[ModelInvocationAttempt, ...]


class ModelInvocationChainError(RuntimeError):
    """候选链失败；只暴露安全分类和 Attempt 元数据。"""

    def __init__(
        self,
        failure: ModelInvocationFailure,
        final_category: ModelFailureCategory,
        safe_error_code: str | None = None,
    ) -> None:
        self.failure = failure
        self.failure_category = final_category
        self.error_code = safe_error_code or f"MODEL_CHAIN_{final_category.value}"
        super().__init__("所有可用模型候选均未成功")


class DurableModelLifecycleError(RuntimeError):
    """Durable model state could not be fenced; provider must not be retried."""


class ModelInvocationConfirmationRequired(RuntimeError):
    error_code = "MODEL_ROUTING_CONFIRMATION_REQUIRED"

    def __init__(self) -> None:
        super().__init__("模型路由需要用户确认")


def _safe_error_code(
    exc: BaseException, category: ModelFailureCategory
) -> str:
    value = getattr(exc, "safe_error_code", None)
    if isinstance(value, str) and value and len(value) <= 80:
        normalized = "".join(
            char for char in value.upper() if char.isalnum() or char == "_"
        )
        if normalized:
            return normalized
    return category.value


def classify_model_failure(exc: BaseException) -> ModelFailureCategory:
    """仅根据异常类型、状态码和显式安全属性分类。"""
    if isinstance(exc, ModelAdapterInvocationError):
        return exc.model_failure_category
    if isinstance(exc, RunCancelledError):
        return ModelFailureCategory.CANCELLED
    if isinstance(exc, RunDeadlineExceededError):
        return ModelFailureCategory.DEADLINE_EXCEEDED
    if isinstance(exc, BudgetExceededError):
        return ModelFailureCategory.BUDGET_EXHAUSTED
    explicit = getattr(exc, "model_failure_category", None)
    if isinstance(explicit, ModelFailureCategory):
        return explicit
    if isinstance(explicit, str):
        try:
            return ModelFailureCategory(explicit)
        except ValueError:
            pass
    if bool(getattr(exc, "safety_refusal", False)):
        return ModelFailureCategory.SAFETY_REFUSAL
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return ModelFailureCategory.RATE_LIMITED
    if status_code in {408, 504}:
        return ModelFailureCategory.PROVIDER_TIMEOUT
    if status_code in {401, 403, 404}:
        return ModelFailureCategory.PROVIDER_CONFIGURATION_ERROR
    if status_code in {413}:
        return ModelFailureCategory.CONTEXT_LIMIT_EXCEEDED
    if isinstance(status_code, int) and 400 <= status_code < 500:
        return ModelFailureCategory.INVALID_REQUEST
    if isinstance(status_code, int) and status_code >= 500:
        return ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE
    if isinstance(exc, TimeoutError):
        return ModelFailureCategory.PROVIDER_TIMEOUT
    if isinstance(exc, ConnectionError):
        return ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE
    return ModelFailureCategory.UNKNOWN_FAILURE


_BREAKER_FAILURES = frozenset(
    {
        ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE,
        ModelFailureCategory.PROVIDER_TIMEOUT,
        ModelFailureCategory.RATE_LIMITED,
    }
)

_MODEL_INJECTED_FAILURES = {
    InjectedFaultCode.INJECTED_TRANSIENT_FAILURE: (
        ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE,
        "MODEL_INJECTED_TRANSIENT_FAILURE",
    ),
    InjectedFaultCode.INJECTED_RATE_LIMIT: (
        ModelFailureCategory.RATE_LIMITED,
        "MODEL_INJECTED_RATE_LIMIT",
    ),
    InjectedFaultCode.INJECTED_TIMEOUT: (
        ModelFailureCategory.PROVIDER_TIMEOUT,
        "MODEL_INJECTED_TIMEOUT",
    ),
    InjectedFaultCode.INJECTED_PERMANENT_FAILURE: (
        ModelFailureCategory.BUSINESS_FAILURE,
        "MODEL_INJECTED_PERMANENT_FAILURE",
    ),
}


def _model_injected_error(error: InjectedFaultError) -> ModelAdapterInvocationError:
    category, safe_error_code = _MODEL_INJECTED_FAILURES.get(
        error.code,
        (
            ModelFailureCategory.BUSINESS_FAILURE,
            "MODEL_INJECTED_UNSUPPORTED_FAILURE",
        ),
    )
    return ModelAdapterInvocationError(
        category,
        safe_error_code=safe_error_code,
        provider_started=False,
        provider_responded=False,
        output_started=False,
    )


class ModelInvocationRouter:
    """统一协调候选、Circuit、预算、取消、截止时间与一次 Adapter 调用。"""

    def __init__(
        self,
        routing_policy: ModelRoutingPolicy | None = None,
        retry_executor: RetryExecutor | None = None,
        span_recorder=None,
    ) -> None:
        self.routing_policy = routing_policy or ModelRoutingPolicy()
        # 已迁移入口仍是同步 Adapter，不能在此阻塞 Event Loop 等待；默认只做
        # 立即重试。生产 backoff 应由调用 async RetryExecutor 的入口显式注入。
        self.retry_executor = retry_executor or RetryExecutor(
            RetryPolicy(base_delay_seconds=0.0, max_delay_seconds=0.0)
        )
        self.span_recorder = span_recorder

    def invoke(
        self,
        *,
        run_context: RunContext,
        budget_ledger: BudgetLedger,
        routing_decision: ModelRoutingDecision,
        messages: Sequence[Mapping[str, str]],
        adapter_resolver: ModelAdapterResolver,
        circuit_breaker_registry: ModelCircuitBreakerRegistry,
        token_estimate: int,
        max_tokens: int,
        output_started: bool = False,
        event_emitter: RunEventEmitter | StepEventEmitter | None = None,
        generation_options: Mapping[str, object] | None = None,
        fault_controller: FaultInjectionController | None = None,
        invocation_evidence: Mapping[str, object] | None = None,
        async_submit: Callable[[object], object] | None = None,
        on_output_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> ModelInvocationResult:
        recorder = current_span_recorder() or self.span_recorder or NoopSpanRecorder()
        handle = start_span_safely(
            recorder,
            trace_id=run_context.trace_id,
            run_id=run_context.run_id,
            component="model_invocation",
            operation="invoke",
            step_id=(
                getattr(event_emitter, "step_id", None)
                if event_emitter is not None
                else None
            ),
        )
        evidence_for_span = invocation_evidence or {}
        set_span_attributes(
            handle,
            prompt_id=evidence_for_span.get("prompt_id"),
            prompt_version=evidence_for_span.get("prompt_version"),
            prompt_digest=evidence_for_span.get("prompt_digest"),
            estimated_input_tokens=evidence_for_span.get("estimated_input_tokens"),
            reserved_output_tokens=evidence_for_span.get("reserved_output_tokens"),
            context_budget_utilization=evidence_for_span.get("context_budget_utilization"),
            selected_context_items=evidence_for_span.get("selected_context_items"),
            dropped_context_items=evidence_for_span.get("dropped_context_items"),
            structured_repair_count=evidence_for_span.get("structured_repair_count"),
        )
        token = install_trace_context(handle.context)
        recorder_token = install_span_recorder(recorder)
        activity_tracker = run_context.activity_tracker
        if activity_tracker is not None:
            activity_tracker.increment("model_attempts_active")
        try:
            try:
                self._execute_fault_point(
                    fault_controller,
                    FaultPoint.MODEL_BEFORE_INVOCATION,
                    run_context=run_context,
                )
            except ModelAdapterInvocationError as exc:
                category = classify_model_failure(exc)
                raise ModelInvocationChainError(
                    ModelInvocationFailure(
                        routing_decision.capability_preferred_profile_id,
                        routing_decision.initial_selected_profile_id,
                        None,
                        (),
                    ),
                    category,
                    exc.safe_error_code,
                ) from None
            result = self._invoke_impl(
                run_context=run_context,
                budget_ledger=budget_ledger,
                routing_decision=routing_decision,
                messages=messages,
                adapter_resolver=adapter_resolver,
                circuit_breaker_registry=circuit_breaker_registry,
                token_estimate=token_estimate,
                max_tokens=max_tokens,
                output_started=output_started,
                event_emitter=event_emitter,
                generation_options=generation_options,
                fault_controller=fault_controller,
                invocation_evidence=invocation_evidence,
                async_submit=async_submit,
                on_output_delta=on_output_delta,
            )
        except RunCancelledError:
            handle.end_cancelled("RUN_CANCELLED")
            raise
        except (RunDeadlineExceededError, TimeoutError):
            handle.end_timed_out()
            raise
        except BudgetExceededError:
            handle.end_error("BUDGET_EXHAUSTED")
            raise
        except ModelInvocationChainError as exc:
            handle.end_error(exc.error_code)
            raise
        except BaseException:
            handle.end_error()
            raise
        else:
            handle.end_ok()
            return result
        finally:
            if activity_tracker is not None:
                activity_tracker.decrement("model_attempts_active")
            reset_trace_context(token)
            reset_span_recorder(recorder_token)

    async def ainvoke(
        self,
        *,
        run_context: RunContext,
        budget_ledger: BudgetLedger,
        routing_decision: ModelRoutingDecision,
        messages: Sequence[Mapping[str, str]],
        adapter_resolver: ModelAdapterResolver,
        circuit_breaker_registry: ModelCircuitBreakerRegistry,
        token_estimate: int,
        max_tokens: int,
        output_started: bool = False,
        generation_options: Mapping[str, object] | None = None,
        invocation_evidence: Mapping[str, object] | None = None,
    ) -> ModelInvocationResult:
        """Async Router path with first-accepted-output retry barrier.

        Event/Journal publication remains owned by the surrounding Runtime;
        this method only coordinates typed Provider deltas and attempt policy.
        """
        attempts: list[ModelInvocationAttempt] = []
        candidates = list(routing_decision.candidates)
        retry_indexes: dict[ModelProfileId, int] = {}
        last_category = ModelFailureCategory.UNKNOWN_FAILURE
        index = 0
        accepted_output = bool(output_started)
        while index < len(candidates):
            candidate = candidates[index]
            retry_index = retry_indexes.get(candidate.profile_id, 0)
            run_context.raise_if_inactive()
            self._check_deadline(run_context, candidate, index)
            breaker = circuit_breaker_registry.get(candidate.breaker_key)
            try:
                permit = breaker.acquire_permission()
            except CircuitOpenError:
                last_category = ModelFailureCategory.CIRCUIT_OPEN
                attempts.append(self._attempt(index, candidate, False, False, last_category, "MODEL_CIRCUIT_OPEN"))
                index += 1
                continue
            reservation = None
            try:
                reservation = budget_ledger.reserve(
                    self._estimated_usage(candidate, token_estimate, max_tokens, retry_index),
                    reservation_type="model_invocation",
                )
                adapter = adapter_resolver.resolve(candidate.profile_id)

                async def accept_delta(_text: str) -> None:
                    nonlocal accepted_output
                    run_context.raise_if_inactive()
                    accepted_output = True

                if callable(getattr(adapter, "ainvoke", None)):
                    response = await adapter.ainvoke(
                        messages,
                        max_tokens=max_tokens,
                        generation_options=generation_options,
                        run_context=run_context,
                        on_delta=accept_delta,
                    )
                else:
                    response = await asyncio.to_thread(
                        adapter.invoke, messages, max_tokens=max_tokens
                    )
                run_context.raise_if_inactive()
                budget_ledger.commit(
                    reservation,
                    response.actual_usage,
                    usage_source=(
                        UsageSource.ACTUAL
                        if response.actual_usage is not None
                        else UsageSource.ESTIMATED
                    ),
                )
                permit.record_success()
                attempts.append(
                    self._attempt(
                        index,
                        candidate,
                        True,
                        True,
                        None,
                        None,
                        UsageSource.ACTUAL
                        if response.actual_usage is not None
                        else UsageSource.ESTIMATED,
                    )
                )
                evidence = dict(invocation_evidence or {})
                return ModelInvocationResult(
                    output=response.output,
                    capability_preferred_profile_id=routing_decision.capability_preferred_profile_id,
                    initial_selected_profile_id=routing_decision.initial_selected_profile_id,
                    executed_profile_id=candidate.profile_id,
                    attempts=self._with_retry_metadata(attempts, routing_decision.candidates),
                    quality_tradeoff_disclosed=(
                        candidate.adjustment == RoutingAdjustment.DOWNGRADE_TO_LOCAL
                        or (
                            routing_decision.quality_tradeoff_disclosed
                            and routing_decision.capability_preferred_profile_id
                            != routing_decision.initial_selected_profile_id
                        )
                    ),
                    response=response,
                    prompt_id=str(evidence.get("prompt_id", "")),
                    prompt_version=str(evidence.get("prompt_version", "")),
                    prompt_digest=str(evidence.get("prompt_digest", "")),
                    provider_kind=candidate.profile.provider_kind,
                    model_identity=candidate.profile.model_identity,
                )
            except asyncio.CancelledError:
                if reservation is not None:
                    budget_ledger.release(reservation)
                permit.abandon()
                raise
            except Exception as exc:
                category = classify_model_failure(exc)
                last_category = category
                # Async Router 只信任其 acceptance callback 已完成的事实；
                # Provider/Adapter 自报的 output_started 不能越过 Runtime barrier。
                partial_output = accepted_output
                started = bool(getattr(exc, "provider_started", True))
                if reservation is not None:
                    if started:
                        budget_ledger.commit(reservation, None, usage_source=UsageSource.ESTIMATED)
                    else:
                        budget_ledger.release(reservation)
                if category in {ModelFailureCategory.CANCELLED, ModelFailureCategory.DEADLINE_EXCEEDED}:
                    permit.record_indeterminate()
                    if category is ModelFailureCategory.CANCELLED:
                        run_context.cancellation_token.raise_if_cancelled()
                        raise RunCancelledError("MODEL_CANCELLED") from None
                    raise RunDeadlineExceededError("model invocation deadline exceeded") from None
                if partial_output:
                    permit.record_indeterminate()
                elif category in _BREAKER_FAILURES:
                    permit.record_failure()
                else:
                    permit.record_indeterminate()
                attempts.append(
                    self._attempt(
                        index,
                        candidate,
                        started,
                        False,
                        category,
                        _safe_error_code(exc, category),
                        UsageSource.ESTIMATED if started else None,
                    )
                )
                decision = self.retry_executor.decide(
                    category=category,
                    retry_index=retry_index + 1,
                    output_started=partial_output,
                    remaining_seconds=run_context.remaining_seconds(),
                    has_fallback=self._has_allowed_next(
                        tuple(candidates), index, candidate, category, partial_output
                    ),
                    estimated_attempt_seconds=self._estimated_latency_seconds(candidate),
                )
                if decision.should_retry:
                    retry_indexes[candidate.profile_id] = retry_index + 1
                    if decision.delay_seconds:
                        await asyncio.sleep(decision.delay_seconds)
                    run_context.raise_if_inactive()
                    candidates.insert(index + 1, candidate)
                    index += 1
                    continue
                if self._has_allowed_next(
                    tuple(candidates), index, candidate, category, partial_output
                ):
                    index += 1
                    continue
                raise ModelInvocationChainError(
                    ModelInvocationFailure(
                        routing_decision.capability_preferred_profile_id,
                        routing_decision.initial_selected_profile_id,
                        None,
                        self._with_retry_metadata(attempts, routing_decision.candidates),
                    ),
                    category,
                    _safe_error_code(exc, category),
                ) from None
            index += 1
        raise ModelInvocationChainError(
            ModelInvocationFailure(
                routing_decision.capability_preferred_profile_id,
                routing_decision.initial_selected_profile_id,
                None,
                self._with_retry_metadata(attempts, routing_decision.candidates),
            ),
            last_category,
        )

    def _invoke_impl(
        self,
        *,
        run_context: RunContext,
        budget_ledger: BudgetLedger,
        routing_decision: ModelRoutingDecision,
        messages: Sequence[Mapping[str, str]],
        adapter_resolver: ModelAdapterResolver,
        circuit_breaker_registry: ModelCircuitBreakerRegistry,
        token_estimate: int,
        max_tokens: int,
        output_started: bool = False,
        event_emitter: RunEventEmitter | StepEventEmitter | None = None,
        generation_options: Mapping[str, object] | None = None,
        fault_controller: FaultInjectionController | None = None,
        invocation_evidence: Mapping[str, object] | None = None,
        async_submit: Callable[[object], object] | None = None,
        on_output_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> ModelInvocationResult:
        if routing_decision.confirmation_required:
            raise ModelInvocationConfirmationRequired()
        if token_estimate < 0:
            raise ValueError("token_estimate 必须是非负整数")
        attempts: list[ModelInvocationAttempt] = []
        seen: set[ModelProfileId] = set()
        last_category = ModelFailureCategory.UNKNOWN_FAILURE
        terminal_error_code: str | None = None
        # list 允许在失败后仅为当前 Profile 插入下一次 Attempt；Router 仍只
        # 负责 Profile 间 Fallback，RetryExecutor 是同 Profile Retry Owner。
        candidates = list(routing_decision.candidates)
        if len({candidate.profile_id for candidate in candidates}) != len(candidates):
            raise RuntimeError("Routing Chain 不允许重复 Profile")
        retry_indexes: dict[ModelProfileId, int] = {}
        accepted_output = bool(output_started)
        for index, candidate in enumerate(candidates):
            retry_index = retry_indexes.get(candidate.profile_id, 0)
            if candidate.profile_id in seen and retry_index == 0:
                raise RuntimeError("Routing Chain 不允许重复 Profile")
            seen.add(candidate.profile_id)
            run_context.raise_if_inactive()
            self._check_deadline(run_context, candidate, index)
            if candidate.profile.context_window < routing_decision.required_context_window:
                last_category = ModelFailureCategory.CONTEXT_LIMIT_EXCEEDED
                attempts.append(
                    self._attempt(
                        index,
                        candidate,
                        False,
                        False,
                        last_category,
                        "MODEL_CONTEXT_WINDOW_INSUFFICIENT",
                    )
                )
                self._record_pre_provider_attempt_span(
                    run_context, event_emitter, candidate, index, retry_index,
                    "MODEL_CONTEXT_WINDOW_INSUFFICIENT",
                )
                if not self._has_allowed_next(
                    candidates,
                    index,
                    candidate,
                    last_category,
                    output_started,
                ):
                    break
                continue
            breaker = circuit_breaker_registry.get(candidate.breaker_key)
            try:
                permit = breaker.acquire_permission()
            except CircuitOpenError:
                last_category = ModelFailureCategory.CIRCUIT_OPEN
                attempts.append(
                    self._attempt(
                        index,
                        candidate,
                        False,
                        False,
                        last_category,
                        "MODEL_CIRCUIT_OPEN",
                    )
                )
                self._record_pre_provider_attempt_span(
                    run_context, event_emitter, candidate, index, retry_index,
                    "MODEL_CIRCUIT_OPEN",
                )
                if not self._has_allowed_next(
                    candidates,
                    index,
                    candidate,
                    last_category,
                    output_started,
                ):
                    break
                continue
            usage = self._estimated_usage(candidate, token_estimate, max_tokens, retry_index)
            try:
                reservation = budget_ledger.reserve(
                    usage,
                    reservation_type="model_invocation",
                )
            except BudgetExceededError as exc:
                permit.abandon()
                attempts.append(
                    self._attempt(
                        index,
                        candidate,
                        False,
                        False,
                        ModelFailureCategory.BUDGET_EXHAUSTED,
                        "BUDGET_EXHAUSTED",
                    )
                )
                self._record_pre_provider_attempt_span(
                    run_context, event_emitter, candidate, index, retry_index,
                    "BUDGET_EXHAUSTED",
                )
                exc.model_attempts = tuple(attempts)
                raise
            try:
                run_context.raise_if_inactive()
                self._check_deadline(run_context, candidate, index)
            except Exception as exc:
                budget_ledger.release(reservation)
                permit.abandon()
                category = classify_model_failure(exc)
                attempts.append(
                    self._attempt(
                        index,
                        candidate,
                        False,
                        False,
                        category,
                        _safe_error_code(exc, category),
                    )
                )
                self._record_pre_provider_attempt_span(
                    run_context, event_emitter, candidate, index, retry_index,
                    _safe_error_code(exc, category),
                    timed_out=isinstance(exc, (RunDeadlineExceededError, TimeoutError)),
                    cancelled=isinstance(exc, RunCancelledError),
                )
                exc.model_attempts = tuple(attempts)
                raise
            attempt_span, attempt_trace_token = self._start_model_attempt_span(
                run_context,
                event_emitter,
                candidate,
                self._candidate_index(routing_decision, candidate.profile_id),
                retry_index,
            )
            try:
                started_event_emitted = False
                durable_attempt = None
                adapter = adapter_resolver.resolve(candidate.profile_id)
                self._execute_fault_point(
                    fault_controller,
                    FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
                    run_context=run_context,
                    attempt_number=len(attempts) + 1,
                )
                try:
                    durable_attempt = self._start_durable_model_attempt(
                        run_context=run_context,
                        event_emitter=event_emitter,
                        candidate=candidate,
                        messages=messages,
                        max_tokens=max_tokens,
                        generation_options=generation_options,
                        async_submit=async_submit,
                        attempts=attempts,
                    )
                except Exception as exc:
                    raise DurableModelLifecycleError(
                        "MODEL_DURABLE_START_FAILED"
                    ) from exc
                attempt_started_monotonic = time.monotonic()
                # Candidate、Context、Circuit、Budget、Cancellation/Deadline 与
                # Adapter resolution 均已成功；进入 invoke 前由 Router 发布唯一
                # MODEL_STARTED。第三方 Adapter 无需实现 callback 也有真实时间语义。
                started_event_emitted = self._emit_attempt_started(
                    event_emitter,
                    candidate=candidate,
                    candidate_index=self._candidate_index(
                        routing_decision, candidate.profile_id
                    ),
                    retry_index=retry_index,
                )

                def on_started() -> None:
                    # GeneratorModelAdapter 保留真实 Provider started callback，
                    # 这里只确认事实，不重复发布第二个 MODEL_STARTED。
                    return None

                if isinstance(adapter, GeneratorModelAdapter) and async_submit is not None:
                    async def accept_delta(text: str) -> None:
                        nonlocal accepted_output
                        run_context.raise_if_inactive()
                        if on_output_delta is not None:
                            try:
                                await on_output_delta(text)
                            except Exception as exc:
                                if bool(getattr(exc, "output_started", False)):
                                    accepted_output = True
                                raise
                        accepted_output = True
                        run_context.raise_if_inactive()

                    response = async_submit(adapter.ainvoke(
                        messages,
                        max_tokens=max_tokens,
                        on_started=on_started,
                        generation_options=generation_options,
                        run_context=run_context,
                        on_delta=accept_delta,
                    ))
                elif isinstance(adapter, GeneratorModelAdapter):
                    response = adapter.invoke(
                        messages,
                        max_tokens=max_tokens,
                        on_started=on_started,
                        generation_options=generation_options,
                        run_context=run_context,
                    )
                else:
                    response = adapter.invoke(messages, max_tokens=max_tokens)
            except DurableModelLifecycleError:
                # A durable STARTED marker is a precondition for provider I/O;
                # repository failure must not be interpreted as a provider
                # failure and must never enter fallback/retry.
                if durable_attempt is not None:
                    budget_ledger.commit(
                        reservation,
                        None,
                        usage_source=UsageSource.ESTIMATED,
                    )
                else:
                    budget_ledger.release(reservation)
                permit.abandon()
                attempt_span.end_error("MODEL_DURABLE_START_FAILED")
                reset_trace_context(attempt_trace_token)
                raise
            except JournalError:
                # Provider 尚未调用；Journal 失败必须终止本次调用且不得 fallback/retry。
                if durable_attempt is not None:
                    self._finish_durable_model_attempt(
                        run_context=run_context,
                        durable_attempt=durable_attempt,
                        state=ModelAttemptState.UNKNOWN,
                        async_submit=async_submit,
                        safe_error="MODEL_STARTED_EVENT_FAILED",
                    )
                    budget_ledger.commit(
                        reservation,
                        None,
                        usage_source=UsageSource.ESTIMATED,
                    )
                else:
                    budget_ledger.release(reservation)
                permit.abandon()
                attempt_span.end_error("MODEL_STARTED_EVENT_FAILED")
                reset_trace_context(attempt_trace_token)
                raise
            except Exception as exc:
                category = classify_model_failure(exc)
                last_category = category
                started = bool(getattr(exc, "provider_started", True))
                partial_output = accepted_output or (
                    bool(getattr(exc, "output_started", False))
                    if async_submit is None
                    else isinstance(adapter, GeneratorModelAdapter)
                    and bool(getattr(exc, "output_started", False))
                )
                if started or durable_attempt is not None:
                    # A durable STARTED row represents an uncertain provider
                    # boundary; keep the reservation consumed even when an
                    # adapter reports that it did not observe the request.
                    budget_ledger.commit(
                        reservation,
                        None,
                        usage_source=UsageSource.ESTIMATED,
                    )
                else:
                    budget_ledger.release(reservation)
                if durable_attempt is not None:
                    # Once the durable STARTED marker is written, every local
                    # provider exception is uncertainty.  Recovery, rather
                    # than this worker, owns any retry decision.
                    self._finish_durable_model_attempt(
                        run_context=run_context,
                        durable_attempt=durable_attempt,
                        state=ModelAttemptState.UNKNOWN,
                        async_submit=async_submit,
                        safe_error=_safe_error_code(exc, category),
                    )
                health_outcome = self._circuit_health_outcome(
                    category=category,
                    provider_started=started,
                    provider_responded=getattr(exc, "provider_responded", None),
                    registry=circuit_breaker_registry,
                )
                if (
                    health_outcome
                    == CircuitHealthOutcome.QUALIFYING_PROVIDER_FAILURE
                ):
                    permit.record_failure()
                elif health_outcome == CircuitHealthOutcome.HEALTHY_COMPLETION:
                    permit.record_success()
                elif health_outcome == CircuitHealthOutcome.NOT_STARTED:
                    permit.abandon()
                else:
                    permit.record_indeterminate()
                attempts.append(
                    self._attempt(
                        index,
                        candidate,
                        started,
                        False,
                        category,
                        _safe_error_code(exc, category),
                        ModelUsageSource.ESTIMATED if started else None,
                    )
                )
                try:
                    if started_event_emitted:
                        self._emit_attempt_completed(
                            event_emitter,
                            candidate=candidate,
                            candidate_index=self._candidate_index(
                                routing_decision, candidate.profile_id
                            ),
                            retry_index=retry_index,
                            succeeded=False,
                            safe_error_code=_safe_error_code(exc, category),
                            duration_ms=max(
                                0,
                                int(
                                    (time.monotonic() - attempt_started_monotonic)
                                    * 1000
                                ),
                            ),
                        )
                finally:
                    if attempt_span.context is not None:
                        attempt_span.set_safe_attribute("provider_started", started)
                    attempt_error_code = _safe_error_code(exc, category)
                    if category is ModelFailureCategory.CANCELLED:
                        attempt_span.end_cancelled(attempt_error_code)
                    elif category is ModelFailureCategory.DEADLINE_EXCEEDED:
                        attempt_span.end_timed_out(attempt_error_code)
                    else:
                        attempt_span.end_error(attempt_error_code)
                    reset_trace_context(attempt_trace_token)
                # 同 Profile 失败后由统一策略决定是否插入一次 Retry。插入的
                # 候选不属于 Fallback，且每次会重新取得 Permit、原子预留预算。
                decision = self.retry_executor.decide(
                    category=category,
                    retry_index=retry_index + 1,
                    output_started=partial_output,
                    remaining_seconds=run_context.remaining_seconds(),
                    has_fallback=self._has_allowed_next(
                        tuple(candidates), index, candidate, category, partial_output
                    ),
                    estimated_attempt_seconds=self._estimated_latency_seconds(candidate),
                )
                if decision.should_retry:
                    retry_indexes[candidate.profile_id] = retry_index + 1
                    # 同步真实入口不能阻塞 Event Loop；当前 Adapter 本身为同步，
                    # 因此只支持零延迟策略，非零 backoff 由 async RetryExecutor 使用。
                    # 记录 delay，调用前再次校验 deadline，避免隐藏 sleep。
                    if decision.delay_seconds == 0:
                        candidates.insert(index + 1, candidate)
                        continue
                    # 同步生产入口不能静默丢弃非零 delay 后立刻调用；明确返回
                    # 安全失败，等待异步入口完成迁移。
                    attempts[-1] = replace(
                        attempts[-1], safe_error_code="SYNC_RETRY_DELAY_UNSUPPORTED"
                    )
                    terminal_error_code = "SYNC_RETRY_DELAY_UNSUPPORTED"
                    break
                if (
                    category == ModelFailureCategory.RATE_LIMITED
                    and self.retry_executor.policy.rate_limit_recovery_mode.value == "STOP"
                ):
                    break
                if category in {
                    ModelFailureCategory.CANCELLED,
                    ModelFailureCategory.DEADLINE_EXCEEDED,
                    ModelFailureCategory.BUDGET_EXHAUSTED,
                }:
                    if isinstance(
                        exc,
                        (
                            RunCancelledError,
                            RunDeadlineExceededError,
                            BudgetExceededError,
                        ),
                    ):
                        exc.model_attempts = tuple(attempts)
                        raise
                    break
                if not self._has_allowed_next(
                    candidates,
                    index,
                    candidate,
                    category,
                    partial_output,
                ):
                    break
                continue
            except BaseException:
                budget_ledger.release(reservation)
                permit.abandon()
                attempt_span.end_cancelled("MODEL_ATTEMPT_ABORTED")
                reset_trace_context(attempt_trace_token)
                raise
            actual = response.actual_usage
            if durable_attempt is not None:
                try:
                    self._finish_durable_model_attempt(
                        run_context=run_context,
                        durable_attempt=durable_attempt,
                        state=ModelAttemptState.COMPLETED,
                        async_submit=async_submit,
                        result=self._durable_model_result(response),
                        usage=self._durable_usage(response.actual_usage),
                    )
                except Exception:
                    # The provider already returned, but a fenced durable
                    # commit failure must never fall through as a successful
                    # invocation.  Preserve the conservative reservation and
                    # leave STARTED for takeover classification.
                    budget_ledger.commit(
                        reservation,
                        None,
                        usage_source=UsageSource.ESTIMATED,
                    )
                    permit.record_indeterminate()
                    attempt_span.end_error("MODEL_DURABLE_COMMIT_FAILED")
                    reset_trace_context(attempt_trace_token)
                    raise
            try:
                budget_ledger.commit(
                    reservation,
                    actual,
                    usage_source=(
                        UsageSource.ACTUAL
                        if actual is not None
                        else UsageSource.ESTIMATED
                    ),
                )
            except BudgetExceededError as exc:
                # Provider 已成功响应，Circuit 视为健康；但实际 Token/Cost
                # 超过原子可补差范围时不得把预算推过上限，也不得返回正文。
                # 以原预留保守结算并向上返回预算失败。
                budget_ledger.commit(
                    reservation,
                    None,
                    usage_source=UsageSource.ESTIMATED,
                )
                permit.record_success()
                attempts.append(
                    self._attempt(
                        index,
                        candidate,
                        True,
                        False,
                        ModelFailureCategory.BUDGET_EXHAUSTED,
                        "BUDGET_EXHAUSTED",
                        ModelUsageSource.ESTIMATED,
                    )
                )
                try:
                    if started_event_emitted:
                        self._emit_attempt_completed(
                            event_emitter,
                            candidate=candidate,
                            candidate_index=self._candidate_index(
                                routing_decision, candidate.profile_id
                            ),
                            retry_index=retry_index,
                            succeeded=False,
                            safe_error_code="BUDGET_EXHAUSTED",
                            duration_ms=max(
                                0,
                                int(
                                    (time.monotonic() - attempt_started_monotonic)
                                    * 1000
                                ),
                            ),
                        )
                finally:
                    if attempt_span.context is not None:
                        attempt_span.set_safe_attribute("provider_started", True)
                    attempt_span.end_error("BUDGET_EXHAUSTED")
                    reset_trace_context(attempt_trace_token)
                exc.model_attempts = tuple(attempts)
                raise
            permit.record_success()
            attempts.append(
                self._attempt(
                    index,
                    candidate,
                    True,
                    True,
                    None,
                    None,
                    (
                        ModelUsageSource.ACTUAL
                        if actual is not None
                        else ModelUsageSource.ESTIMATED
                    ),
                )
            )
            try:
                if started_event_emitted:
                    self._emit_attempt_completed(
                        event_emitter,
                        candidate=candidate,
                        candidate_index=self._candidate_index(
                            routing_decision, candidate.profile_id
                        ),
                        retry_index=retry_index,
                        succeeded=True,
                        safe_error_code=None,
                        duration_ms=max(
                            0,
                            int(
                                (time.monotonic() - attempt_started_monotonic) * 1000
                            ),
                        ),
                    )
            finally:
                if attempt_span.context is not None:
                    attempt_span.set_safe_attribute("provider_started", True)
                attempt_span.end_ok()
                reset_trace_context(attempt_trace_token)
            evidence = dict(invocation_evidence or {})
            def _safe_int(name: str) -> int:
                value = evidence.get(name, 0)
                return value if isinstance(value, int) and not isinstance(value, bool) else 0
            return ModelInvocationResult(
                response.output,
                routing_decision.capability_preferred_profile_id,
                routing_decision.initial_selected_profile_id,
                candidate.profile_id,
                self._with_retry_metadata(attempts, routing_decision.candidates),
                (
                    candidate.adjustment == RoutingAdjustment.DOWNGRADE_TO_LOCAL
                    or (
                        routing_decision.quality_tradeoff_disclosed
                        and routing_decision.capability_preferred_profile_id
                        != routing_decision.initial_selected_profile_id
                    )
                ),
                response,
                str(evidence.get("prompt_id", "")),
                str(evidence.get("prompt_version", "")),
                str(evidence.get("prompt_digest", "")),
                candidate.profile.provider_kind,
                candidate.profile.model_identity,
                _safe_int("estimated_input_tokens"),
                _safe_int("reserved_output_tokens"),
                float(evidence.get("context_budget_utilization", 0.0)) if isinstance(evidence.get("context_budget_utilization", 0.0), (int, float)) and not isinstance(evidence.get("context_budget_utilization", 0.0), bool) else 0.0,
                _safe_int("selected_context_items"),
                _safe_int("dropped_context_items"),
                _safe_int("structured_repair_count"),
            )
        raise ModelInvocationChainError(
            ModelInvocationFailure(
                routing_decision.capability_preferred_profile_id,
                routing_decision.initial_selected_profile_id,
                None,
                self._with_retry_metadata(attempts, routing_decision.candidates),
            ),
            last_category,
            terminal_error_code,
        )

    @staticmethod
    def _execute_fault_point(
        controller: FaultInjectionController | None,
        point: FaultPoint,
        *,
        run_context: RunContext,
        attempt_number: int | None = None,
    ) -> None:
        if controller is None or not controller.enabled:
            return
        context = FaultMatchContext(
            fault_point=point,
            component="model",
            run_id_digest=hashlib.sha256(
                run_context.run_id.encode("utf-8")
            ).hexdigest(),
            attempt_number=attempt_number,
        )
        try:
            result = controller.execute_blocking_if_matched(
                context,
                raise_if_cancelled=run_context.raise_if_inactive,
                allowed_actions={
                    FaultAction.RAISE_TYPED_ERROR,
                    FaultAction.DELAY,
                    FaultAction.BLOCK_UNTIL_RELEASED,
                },
            )
        except (RunCancelledError, RunDeadlineExceededError) as exc:
            # The request stopped while the pre-call seam was waiting.  Preserve
            # the existing typed cancellation/deadline path without charging a
            # Provider call that never began.
            exc.provider_started = False
            exc.provider_responded = False
            exc.output_started = False
            raise
        except InjectedFaultError as exc:
            raise _model_injected_error(exc) from None
        if isinstance(result, InjectedFailureResult):
            raise ModelAdapterInvocationError(
                ModelFailureCategory.BUSINESS_FAILURE,
                safe_error_code="MODEL_INJECTED_ACTION_UNSUPPORTED",
                provider_started=False,
                provider_responded=False,
            )

    @staticmethod
    def _durable_model_request_digest(
        messages: Sequence[Mapping[str, str]],
        *,
        max_tokens: int,
        generation_options: Mapping[str, object] | None,
    ) -> str:
        """Digest the request without persisting prompt or option contents."""
        return canonical_payload_digest(
            {
                "messages": [dict(message) for message in messages],
                "max_tokens": max_tokens,
                "generation_options": dict(generation_options or {}),
            }
        )

    @staticmethod
    def _durable_model_result(response: ModelAdapterResponse) -> dict[str, object]:
        """Return only JSON-safe data needed by a resumed step."""
        result: dict[str, object] = {"output": response.output}
        native = response.native_tool_call
        if native is not None:
            result["native_tool_call"] = {
                "provider_tool_call_id": native.provider_tool_call_id,
                "tool_name": native.tool_name,
                "arguments_json": native.arguments_json,
            }
        return result

    @staticmethod
    def _durable_usage(usage: BudgetUsage | None) -> dict[str, int]:
        if usage is None:
            return {}
        return {
            name: int(getattr(usage, name))
            for name in BudgetUsage.__dataclass_fields__
            if not name.startswith("_")
        }

    def _start_durable_model_attempt(
        self,
        *,
        run_context: RunContext,
        event_emitter: RunEventEmitter | StepEventEmitter | None,
        candidate: ModelRoutingCandidate,
        messages: Sequence[Mapping[str, str]],
        max_tokens: int,
        generation_options: Mapping[str, object] | None,
        async_submit: Callable[[object], object] | None,
        attempts: Sequence[ModelInvocationAttempt],
    ) -> tuple[object, str, int, int] | None:
        """Persist a fenced STARTED marker before entering provider I/O.

        The tuple is ``(repository, step_id, step_attempt, model_attempt)``.
        ``load`` is only used to select the current durable attempt number;
        the mutation itself remains owned by ``start_model_attempt``.
        """
        repository = run_context.execution_repository
        if repository is None:
            return None
        lease = run_context.durable_lease
        step_id = getattr(event_emitter, "step_id", None)
        if lease is None or not isinstance(step_id, str) or not step_id.strip():
            raise RuntimeError(
                "durable model invocation requires durable lease and step_id"
            )
        if not callable(async_submit):
            raise RuntimeError(
                "durable model invocation requires owner-loop async_submit"
            )
        load = getattr(repository, "load", None)
        step_attempt = 1
        model_attempt = len(attempts) + 1
        if callable(load):
            image = async_submit(load(run_context.run_id))
            step_row = next(
                (
                    row
                    for row in getattr(image, "steps", ())
                    if getattr(row, "step_id", None) == step_id
                ),
                None,
            )
            if step_row is not None:
                step_attempt = int(getattr(step_row, "current_attempt", 0) or 0)
                if step_attempt <= 0:
                    step_attempt = 1
            existing = [
                int(getattr(row, "model_attempt_number", 0) or 0)
                for row in getattr(image, "models", ())
                if getattr(row, "step_id", None) == step_id
                and int(getattr(row, "attempt_number", 0) or 0) == step_attempt
            ]
            if existing:
                model_attempt = max(existing) + 1
        start = getattr(repository, "start_model_attempt", None)
        if not callable(start):
            raise RuntimeError("execution repository lacks start_model_attempt")
        async_submit(
            start(
                lease,
                step_id=step_id,
                attempt=step_attempt,
                model_attempt=model_attempt,
                request_digest=self._durable_model_request_digest(
                    messages,
                    max_tokens=max_tokens,
                    generation_options=generation_options,
                ),
                provider_kind=candidate.profile.provider_kind,
                profile_identity=candidate.profile.model_identity
                or candidate.profile_id.value,
            )
        )
        return repository, step_id, step_attempt, model_attempt

    @staticmethod
    def _finish_durable_model_attempt(
        *,
        run_context: RunContext,
        durable_attempt: tuple[object, str, int, int],
        state: ModelAttemptState,
        async_submit: Callable[[object], object] | None,
        result: Mapping[str, object] | None = None,
        usage: Mapping[str, object] | None = None,
        safe_error: str | None = None,
    ) -> None:
        repository, step_id, step_attempt, model_attempt = durable_attempt
        finish = getattr(repository, "finish_model_attempt", None)
        if not callable(finish) or not callable(async_submit):
            raise RuntimeError("execution repository lacks fenced model completion")
        async_submit(
            finish(
                run_context.durable_lease,
                step_id=step_id,
                attempt=step_attempt,
                model_attempt=model_attempt,
                state=state,
                result=result,
                usage=usage,
                safe_error=safe_error,
            )
        )

    def _start_model_attempt_span(
        self,
        run_context: RunContext,
        event_emitter: RunEventEmitter | StepEventEmitter | None,
        candidate: ModelRoutingCandidate,
        candidate_index: int,
        retry_index: int,
    ):
        recorder = current_span_recorder() or self.span_recorder or NoopSpanRecorder()
        handle = start_span_safely(
            recorder,
            trace_id=run_context.trace_id,
            run_id=run_context.run_id,
            component="model_attempt",
            operation="attempt",
            step_id=getattr(event_emitter, "step_id", None),
        )
        if handle.context is not None:
            handle.set_safe_attribute("model_profile", candidate.profile_id.value)
            handle.set_safe_attribute("candidate_index", candidate_index)
            handle.set_safe_attribute("retry_index", retry_index)
            if candidate.profile.provider_kind:
                handle.set_safe_attribute("provider_kind", candidate.profile.provider_kind)
            if candidate.profile.model_identity:
                handle.set_safe_attribute("model_identity", candidate.profile.model_identity)
        return handle, install_trace_context(handle.context)

    def _record_pre_provider_attempt_span(
        self,
        run_context: RunContext,
        event_emitter: RunEventEmitter | StepEventEmitter | None,
        candidate: ModelRoutingCandidate,
        candidate_index: int,
        retry_index: int,
        error_code: str,
        *,
        timed_out: bool = False,
        cancelled: bool = False,
    ) -> None:
        handle, token = self._start_model_attempt_span(
            run_context, event_emitter, candidate, candidate_index, retry_index
        )
        try:
            if handle.context is not None:
                handle.set_safe_attribute("provider_started", False)
            if cancelled:
                handle.end_cancelled(error_code)
            elif timed_out:
                handle.end_timed_out(error_code)
            else:
                handle.end_error(error_code)
        finally:
            reset_trace_context(token)

    @staticmethod
    def _candidate_index(
        routing_decision: ModelRoutingDecision, profile_id: ModelProfileId
    ) -> int:
        for index, candidate in enumerate(routing_decision.candidates):
            if candidate.profile_id == profile_id:
                return index
        raise RuntimeError("Model Attempt 不属于原始 Routing Chain")

    @staticmethod
    def _emit_attempt_completed(
        event_emitter: RunEventEmitter | StepEventEmitter | None,
        *,
        candidate: ModelRoutingCandidate,
        candidate_index: int,
        retry_index: int,
        succeeded: bool,
        safe_error_code: str | None,
        duration_ms: int,
    ) -> None:
        """仅为已成功发布 Started 的 Attempt 发布 Completed。"""
        if event_emitter is None:
            return
        try:
            event_emitter.emit_from_worker(
                RuntimeEventType.MODEL_COMPLETED,
                ModelCompletedPayload(
                    profile_id=candidate.profile_id.value,
                    candidate_index=candidate_index,
                    retry_index=retry_index,
                    succeeded=succeeded,
                    safe_error_code=safe_error_code,
                    duration_ms=duration_ms,
                    provider_kind=candidate.profile.provider_kind,
                    model_identity=candidate.profile.model_identity,
                ),
                component="model_invocation",
            )
        except JournalError:
            # Provider 已完成也不能把持久化失败伪装成成功；同时禁止透明重试。
            raise
        except Exception:
            # Transport 中止或事件发布故障不允许透明重放已发生的 Provider Attempt。
            return

    @staticmethod
    def _emit_attempt_started(
        event_emitter: RunEventEmitter | StepEventEmitter | None,
        *,
        candidate: ModelRoutingCandidate,
        candidate_index: int,
        retry_index: int,
    ) -> bool:
        if event_emitter is None:
            return False
        try:
            event_emitter.emit_from_worker(
                RuntimeEventType.MODEL_STARTED,
                ModelStartedPayload(
                    profile_id=candidate.profile_id.value,
                    candidate_index=candidate_index,
                    retry_index=retry_index,
                    routing_adjustment=candidate.adjustment.value,
                    breaker_key=candidate.breaker_key,
                    provider_kind=candidate.profile.provider_kind,
                    model_identity=candidate.profile.model_identity,
                ),
                component="model_invocation",
            )
            return True
        except JournalError:
            raise
        except Exception:
            # Backpressure/Transport 故障不应让 Provider Attempt 被透明重放。
            return False

    @staticmethod
    def _check_deadline(
        run_context: RunContext,
        candidate: ModelRoutingCandidate,
        attempt_index: int,
    ) -> None:
        run_context.raise_if_inactive()
        remaining = run_context.remaining_seconds()
        if remaining is None:
            return
        metadata = candidate.profile.cost_profile
        if attempt_index > 0 and (
            metadata is None or metadata.estimated_latency_ms <= 0
        ):
            raise RunDeadlineExceededError(
                "Fallback 候选缺少可验证的延迟配置"
            )
        if (
            metadata is not None
            and metadata.estimated_latency_ms > 0
            and remaining * 1000 < metadata.estimated_latency_ms
        ):
            raise RunDeadlineExceededError("剩余时间不足以启动模型候选")

    @staticmethod
    def _estimated_usage(
        candidate: ModelRoutingCandidate,
        input_tokens: int,
        max_tokens: int,
        retry_index: int = 0,
    ) -> BudgetUsage:
        metadata = candidate.profile.cost_profile
        # 未配置成本时使用非零保守占位；生产 Profile 应显式配置并人工确认。
        cost_units = 1
        if metadata is not None:
            cost_units = (
                metadata.fixed_call_cost_units
                + (input_tokens * metadata.input_cost_units_per_1k_tokens + 999)
                // 1000
                + (max_tokens * metadata.output_cost_units_per_1k_tokens + 999)
                // 1000
            )
        return BudgetUsage(
            model_calls=1,
            remote_model_calls=int(candidate.profile.effective_is_remote),
            input_tokens=input_tokens,
            output_tokens=max_tokens,
            total_tokens=input_tokens + max_tokens,
            cost_units=cost_units,
            retries=1 if retry_index > 0 else 0,
        )

    @staticmethod
    def _estimated_latency_seconds(candidate: ModelRoutingCandidate) -> float | None:
        profile = candidate.profile.cost_profile
        if profile is None or profile.estimated_latency_ms <= 0:
            return None
        return profile.estimated_latency_ms / 1000

    @staticmethod
    def _attempt(
        index: int,
        candidate: ModelRoutingCandidate,
        started: bool,
        succeeded: bool,
        category: ModelFailureCategory | None,
        error_code: str | None,
        usage_source: ModelUsageSource | None = None,
    ) -> ModelInvocationAttempt:
        return ModelInvocationAttempt(
            index,
            candidate.profile_id,
            candidate.breaker_key,
            started,
            succeeded,
            category,
            error_code,
            candidate.adjustment,
            usage_source,
            provider_kind=candidate.profile.provider_kind,
            model_identity=candidate.profile.model_identity,
        )

    @staticmethod
    def _with_retry_metadata(
        attempts: list[ModelInvocationAttempt],
        original_candidates: tuple[ModelRoutingCandidate, ...],
    ) -> tuple[ModelInvocationAttempt, ...]:
        """在返回安全记录前，按原始候选链补齐稳定的候选/重试序号。"""
        candidate_indexes = {
            candidate.profile_id: index
            for index, candidate in enumerate(original_candidates)
        }
        retries: dict[ModelProfileId, int] = {}
        normalized: list[ModelInvocationAttempt] = []
        for attempt in attempts:
            retry_index = retries.get(attempt.profile_id, 0)
            retries[attempt.profile_id] = retry_index + 1
            normalized.append(
                replace(
                    attempt,
                    candidate_index=candidate_indexes[attempt.profile_id],
                    retry_index=retry_index,
                )
            )
        return tuple(normalized)

    def _has_allowed_next(
        self,
        candidates: tuple[ModelRoutingCandidate, ...],
        index: int,
        failed: ModelRoutingCandidate,
        category: ModelFailureCategory,
        output_started: bool,
    ) -> bool:
        if index + 1 >= len(candidates):
            return False
        return self.routing_policy.can_fallback(
            category,
            failed_profile=failed.profile,
            next_profile=candidates[index + 1].profile,
            output_started=output_started,
        )

    @staticmethod
    def _circuit_health_outcome(
        *,
        category: ModelFailureCategory,
        provider_started: bool,
        provider_responded: bool | None,
        registry: ModelCircuitBreakerRegistry,
    ) -> CircuitHealthOutcome:
        """Routing Failure 与 Circuit Health 分开判断。"""
        if not provider_started:
            return CircuitHealthOutcome.NOT_STARTED
        qualifying = category in _BREAKER_FAILURES
        if (
            category == ModelFailureCategory.RATE_LIMITED
            and not registry.config.count_rate_limited
        ):
            qualifying = False
        if qualifying:
            return CircuitHealthOutcome.QUALIFYING_PROVIDER_FAILURE
        if category in {
            ModelFailureCategory.SAFETY_REFUSAL,
            ModelFailureCategory.BUSINESS_FAILURE,
            ModelFailureCategory.OUTPUT_VALIDATION_FAILED,
        }:
            return CircuitHealthOutcome.HEALTHY_COMPLETION
        if category in {
            ModelFailureCategory.INVALID_REQUEST,
            ModelFailureCategory.PROVIDER_CONFIGURATION_ERROR,
        }:
            return (
                CircuitHealthOutcome.HEALTHY_COMPLETION
                if provider_responded is True
                else CircuitHealthOutcome.INDETERMINATE_COMPLETION
            )
        if provider_responded is True:
            return CircuitHealthOutcome.HEALTHY_COMPLETION
        return CircuitHealthOutcome.INDETERMINATE_COMPLETION
