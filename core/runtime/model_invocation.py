#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一的模型调用、预算结算、Fallback 与 Circuit 协调边界。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol, Sequence

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
class ModelAdapterResponse:
    output: str
    actual_usage: BudgetUsage | None = None


class ModelAdapter(Protocol):
    """一个 Adapter 只执行一个 Profile 的一次调用。"""

    def invoke(
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

    def invoke(
        self, messages: Sequence[Mapping[str, str]], *, max_tokens: int
    ) -> ModelAdapterResponse:
        chunks: list[str] = []
        provider_started = False
        try:
            provider_started = True
            stream = self._engine.generate(list(messages), max_tokens=max_tokens)
            for chunk in stream:
                if chunk:
                    chunks.append(str(chunk))
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


@dataclass(frozen=True, slots=True)
class ModelInvocationResult:
    output: str
    capability_preferred_profile_id: ModelProfileId | None
    initial_selected_profile_id: ModelProfileId | None
    executed_profile_id: ModelProfileId
    attempts: tuple[ModelInvocationAttempt, ...]
    quality_tradeoff_disclosed: bool


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
    ) -> None:
        self.failure = failure
        self.failure_category = final_category
        self.error_code = f"MODEL_CHAIN_{final_category.value}"
        super().__init__("所有可用模型候选均未成功")


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
    try:
        import requests

        if isinstance(exc, requests.Timeout):
            return ModelFailureCategory.PROVIDER_TIMEOUT
        if isinstance(exc, requests.ConnectionError):
            return ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE
    except ImportError:  # pragma: no cover - requests 是当前项目依赖
        pass
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


class ModelInvocationRouter:
    """统一协调候选、Circuit、预算、取消、截止时间与一次 Adapter 调用。"""

    def __init__(self, routing_policy: ModelRoutingPolicy | None = None) -> None:
        self.routing_policy = routing_policy or ModelRoutingPolicy()

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
    ) -> ModelInvocationResult:
        if routing_decision.confirmation_required:
            raise ModelInvocationConfirmationRequired()
        if token_estimate < 0:
            raise ValueError("token_estimate 必须是非负整数")
        attempts: list[ModelInvocationAttempt] = []
        seen: set[ModelProfileId] = set()
        last_category = ModelFailureCategory.UNKNOWN_FAILURE
        candidates = routing_decision.candidates
        for index, candidate in enumerate(candidates):
            if candidate.profile_id in seen:
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
                if not self._has_allowed_next(
                    candidates,
                    index,
                    candidate,
                    last_category,
                    output_started,
                ):
                    break
                continue
            usage = self._estimated_usage(candidate, token_estimate, max_tokens)
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
                exc.model_attempts = tuple(attempts)
                raise
            try:
                adapter = adapter_resolver.resolve(candidate.profile_id)
                response = adapter.invoke(messages, max_tokens=max_tokens)
            except Exception as exc:
                category = classify_model_failure(exc)
                last_category = category
                started = bool(getattr(exc, "provider_started", True))
                partial_output = output_started or bool(
                    getattr(exc, "output_started", False)
                )
                if started:
                    budget_ledger.commit(
                        reservation,
                        None,
                        usage_source=UsageSource.ESTIMATED,
                    )
                else:
                    budget_ledger.release(reservation)
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
            actual = response.actual_usage
            budget_ledger.commit(
                reservation,
                actual,
                usage_source=(
                    UsageSource.ACTUAL if actual is not None else UsageSource.ESTIMATED
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
                    (
                        ModelUsageSource.ACTUAL
                        if actual is not None
                        else ModelUsageSource.ESTIMATED
                    ),
                )
            )
            return ModelInvocationResult(
                response.output,
                routing_decision.capability_preferred_profile_id,
                routing_decision.initial_selected_profile_id,
                candidate.profile_id,
                tuple(attempts),
                (
                    candidate.adjustment == RoutingAdjustment.DOWNGRADE_TO_LOCAL
                    or (
                        routing_decision.quality_tradeoff_disclosed
                        and routing_decision.capability_preferred_profile_id
                        != routing_decision.initial_selected_profile_id
                    )
                ),
            )
        raise ModelInvocationChainError(
            ModelInvocationFailure(
                routing_decision.capability_preferred_profile_id,
                routing_decision.initial_selected_profile_id,
                None,
                tuple(attempts),
            ),
            last_category,
        )

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
        )

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
        )

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
