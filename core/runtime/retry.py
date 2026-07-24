#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一 Retry 的纯策略与执行器；不记录任何业务内容。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from random import Random
from typing import Awaitable, Callable, FrozenSet, Protocol, TypeVar

from core.runtime.model_routing import ModelFailureCategory
from core.runtime.cancellation import CancellationToken
from core.runtime.context import RunDeadlineExceededError

T = TypeVar("T")


class JitterMode(str, Enum):
    NONE = "NONE"
    FULL = "FULL"
    EQUAL = "EQUAL"


class RateLimitRecoveryMode(str, Enum):
    FALLBACK_FIRST = "FALLBACK_FIRST"
    RETRY_CURRENT_FIRST = "RETRY_CURRENT_FIRST"
    STOP = "STOP"


class RetryableOperationKind(str, Enum):
    MODEL = "MODEL"
    TOOL = "TOOL"
    RAG = "RAG"


class OperationIdempotency(str, Enum):
    READ_ONLY = "READ_ONLY"
    IDEMPOTENT = "IDEMPOTENT"
    IDEMPOTENT_WITH_KEY = "IDEMPOTENT_WITH_KEY"
    NON_IDEMPOTENT = "NON_IDEMPOTENT"
    UNKNOWN = "UNKNOWN"


class RandomSource(Protocol):
    def uniform(self, lower: float, upper: float) -> float: ...


class Sleeper(Protocol):
    async def sleep(self, seconds: float) -> None: ...


class AsyncioSleeper:
    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class CancellableRetrySleeper:
    """等待延迟、取消或 deadline 中最早者；不使用 ``time.sleep``。"""
    def __init__(self, sleeper: Sleeper | None = None) -> None:
        self._sleeper = sleeper or AsyncioSleeper()

    async def sleep(
        self,
        seconds: float,
        *,
        cancellation_token: CancellationToken,
        remaining_seconds: Callable[[], float | None],
    ) -> None:
        cancellation_token.raise_if_cancelled()
        remaining = remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise RunDeadlineExceededError("重试等待前截止时间已到期")
        delay = seconds if remaining is None else min(seconds, remaining)
        delay_task = asyncio.create_task(self._sleeper.sleep(delay))
        cancel_task = asyncio.create_task(cancellation_token.wait_cancelled())
        try:
            done, pending = await asyncio.wait(
                (delay_task, cancel_task), return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done:
                cancellation_token.raise_if_cancelled()
            await delay_task
        finally:
            for task in (delay_task, cancel_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(delay_task, cancel_task, return_exceptions=True)
        if remaining is not None and seconds > remaining:
            raise RunDeadlineExceededError("重试等待期间截止时间已到期")
        cancellation_token.raise_if_cancelled()


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0
    backoff_multiplier: float = 2.0
    jitter_mode: JitterMode = JitterMode.NONE
    jitter_ratio: float = 1.0
    minimum_attempt_seconds: float = 0.1
    retryable_failure_categories: FrozenSet[ModelFailureCategory] = field(
        default_factory=lambda: frozenset((ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE, ModelFailureCategory.PROVIDER_TIMEOUT, ModelFailureCategory.RATE_LIMITED))
    )
    rate_limit_recovery_mode: RateLimitRecoveryMode = RateLimitRecoveryMode.FALLBACK_FIRST

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ValueError("max_attempts 必须是大于等于一的整数")
        for name in ("base_delay_seconds", "max_delay_seconds", "backoff_multiplier", "jitter_ratio", "minimum_attempt_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
                raise ValueError(f"{name} 必须是有限非负数")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds 不得小于 base_delay_seconds")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier 必须大于等于一")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio 必须在零到一之间")
        object.__setattr__(self, "retryable_failure_categories", frozenset(self.retryable_failure_categories))

    def raw_delay(self, retry_index: int) -> float:
        if retry_index < 1:
            raise ValueError("retry_index 必须从一开始")
        value = self.base_delay_seconds
        # 有界迭代避免超大指数溢出。
        for _ in range(retry_index - 1):
            if value >= self.max_delay_seconds / self.backoff_multiplier:
                return self.max_delay_seconds
            value *= self.backoff_multiplier
        return min(value, self.max_delay_seconds)

    def delay(self, retry_index: int, random_source: RandomSource) -> float:
        raw = self.raw_delay(retry_index)
        if self.jitter_mode == JitterMode.NONE:
            return raw
        if self.jitter_mode == JitterMode.FULL:
            return min(raw, random_source.uniform(0, raw * self.jitter_ratio))
        half = raw / 2
        return min(raw, half + random_source.uniform(0, half * self.jitter_ratio))


@dataclass(frozen=True, slots=True)
class RetryDecision:
    should_retry: bool
    reason_code: str
    retry_index: int
    delay_seconds: float
    required_budget: int
    required_time_seconds: float


def retry_allowed_by_idempotency(kind: RetryableOperationKind, idempotency: OperationIdempotency, *, idempotency_key: str | None = None, side_effect_committed: bool = False) -> bool:
    """Tool/RAG 的最小通用契约；MODEL 无外部业务副作用。"""
    if side_effect_committed:
        return False
    if kind == RetryableOperationKind.MODEL:
        return True
    if idempotency in (OperationIdempotency.READ_ONLY, OperationIdempotency.IDEMPOTENT):
        return True
    return idempotency == OperationIdempotency.IDEMPOTENT_WITH_KEY and bool(idempotency_key and idempotency_key.strip())


class RetryExecutor:
    """唯一 Retry Owner。每次回调代表一个真实 Attempt，资源由回调短暂持有。"""
    def __init__(self, policy: RetryPolicy | None = None, *, random_source: RandomSource | None = None, sleeper: Sleeper | None = None) -> None:
        self.policy = policy or RetryPolicy()
        self._random = random_source or Random()
        self._sleeper = sleeper or AsyncioSleeper()

    def decide(self, *, category: ModelFailureCategory, retry_index: int, output_started: bool, remaining_seconds: float | None, has_fallback: bool = False, estimated_attempt_seconds: float | None = None) -> RetryDecision:
        delay = self.policy.delay(retry_index, self._random) if retry_index > 0 else 0.0
        required = delay + max(self.policy.minimum_attempt_seconds, estimated_attempt_seconds or self.policy.minimum_attempt_seconds)
        allowed = category in self.policy.retryable_failure_categories and retry_index < self.policy.max_attempts and not output_started
        if category == ModelFailureCategory.RATE_LIMITED:
            allowed = allowed and self.policy.rate_limit_recovery_mode != RateLimitRecoveryMode.STOP
            if has_fallback and self.policy.rate_limit_recovery_mode == RateLimitRecoveryMode.FALLBACK_FIRST:
                allowed = False
        if remaining_seconds is not None and remaining_seconds <= required:
            allowed = False
            reason = "RETRY_DEADLINE_INSUFFICIENT"
        elif not allowed:
            reason = "RETRY_NOT_ALLOWED"
        else:
            reason = "RETRY_ALLOWED"
        return RetryDecision(allowed, reason, retry_index, delay, 1, required)

    async def execute_async(self, attempt: Callable[[int], Awaitable[T]], *, category_of: Callable[[BaseException], ModelFailureCategory], should_retry: Callable[[ModelFailureCategory, int], RetryDecision], raise_if_cancelled: Callable[[], None]) -> T:
        retry_index = 0
        while True:
            raise_if_cancelled()
            try:
                return await attempt(retry_index)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                category = category_of(exc)
                raise_if_cancelled()
                retry_index += 1
                decision = should_retry(category, retry_index)
                if not decision.should_retry:
                    raise
                raise_if_cancelled()
                await self._sleeper.sleep(decision.delay_seconds)
                raise_if_cancelled()
