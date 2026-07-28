#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一的模型调用、预算结算、Fallback 与 Circuit 协调边界。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Mapping, Protocol, Sequence

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
from core.runtime.event_emitter import StepEventEmitter
from core.runtime.event_journal import JournalError
from core.runtime.events import (
    ModelCompletedPayload,
    ModelStartedPayload,
    RuntimeEventType,
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
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        max_tokens: int,
        on_started: Callable[[], None] | None = None,
        generation_options: Mapping[str, object] | None = None,
    ) -> ModelAdapterResponse:
        chunks: list[str] = []
        provider_started = False
        try:
            provider_started = True
            if on_started is not None:
                on_started()
            stream = self._engine.generate(
                list(messages),
                max_tokens=max_tokens,
                **dict(generation_options or {}),
            )
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
    # 新字段保持旧 Snapshot 的位置兼容，且只保存安全元数据。
    candidate_index: int = 0
    retry_index: int = 0
    backoff_before_seconds: float = 0.0
    circuit_state: str | None = None


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
        safe_error_code: str | None = None,
    ) -> None:
        self.failure = failure
        self.failure_category = final_category
        self.error_code = safe_error_code or f"MODEL_CHAIN_{final_category.value}"
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

    def __init__(
        self,
        routing_policy: ModelRoutingPolicy | None = None,
        retry_executor: RetryExecutor | None = None,
    ) -> None:
        self.routing_policy = routing_policy or ModelRoutingPolicy()
        # 已迁移入口仍是同步 Adapter，不能在此阻塞 Event Loop 等待；默认只做
        # 立即重试。生产 backoff 应由调用 async RetryExecutor 的入口显式注入。
        self.retry_executor = retry_executor or RetryExecutor(
            RetryPolicy(base_delay_seconds=0.0, max_delay_seconds=0.0)
        )

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
        event_emitter: StepEventEmitter | None = None,
        generation_options: Mapping[str, object] | None = None,
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
                started_event_emitted = False
                adapter = adapter_resolver.resolve(candidate.profile_id)
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

                if isinstance(adapter, GeneratorModelAdapter):
                    response = adapter.invoke(
                        messages,
                        max_tokens=max_tokens,
                        on_started=on_started,
                        generation_options=generation_options,
                    )
                else:
                    response = adapter.invoke(messages, max_tokens=max_tokens)
            except JournalError:
                # Provider 尚未调用；Journal 失败必须终止本次调用且不得 fallback/retry。
                budget_ledger.release(reservation)
                permit.abandon()
                raise
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
                    )
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
            actual = response.actual_usage
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
                    )
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
                )
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
    def _candidate_index(
        routing_decision: ModelRoutingDecision, profile_id: ModelProfileId
    ) -> int:
        for index, candidate in enumerate(routing_decision.candidates):
            if candidate.profile_id == profile_id:
                return index
        raise RuntimeError("Model Attempt 不属于原始 Routing Chain")

    @staticmethod
    def _emit_attempt_completed(
        event_emitter: StepEventEmitter | None,
        *,
        candidate: ModelRoutingCandidate,
        candidate_index: int,
        retry_index: int,
        succeeded: bool,
        safe_error_code: str | None,
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
        event_emitter: StepEventEmitter | None,
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
