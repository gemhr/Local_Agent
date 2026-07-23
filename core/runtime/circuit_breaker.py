#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""进程内、线程安全的模型 Circuit Breaker 与共享 Registry。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Lock
from time import monotonic
from typing import Callable
from uuid import uuid4


class ModelCircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True, slots=True)
class ModelCircuitBreakerConfig:
    failure_threshold: int = 3
    recovery_timeout_seconds: float = 30.0
    half_open_max_calls: int = 1
    count_rate_limited: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.failure_threshold, bool) or self.failure_threshold <= 0:
            raise ValueError("failure_threshold 必须是正整数")
        if (
            isinstance(self.recovery_timeout_seconds, bool)
            or self.recovery_timeout_seconds <= 0
        ):
            raise ValueError("recovery_timeout_seconds 必须是正数")
        if isinstance(self.half_open_max_calls, bool) or self.half_open_max_calls <= 0:
            raise ValueError("half_open_max_calls 必须是正整数")
        if type(self.count_rate_limited) is not bool:
            raise ValueError("count_rate_limited 必须是 bool")


@dataclass(frozen=True, slots=True)
class ModelCircuitBreakerSnapshot:
    breaker_key: str
    state: ModelCircuitState
    consecutive_failures: int
    half_open_active_calls: int
    failure_threshold: int
    recovery_timeout_seconds: float
    half_open_max_calls: int


class CircuitOpenError(RuntimeError):
    """Circuit 拒绝调用时使用的安全异常。"""

    error_code = "MODEL_CIRCUIT_OPEN"

    def __init__(self, breaker_key: str) -> None:
        self.breaker_key = breaker_key
        super().__init__("模型服务 Circuit 当前不可用")


class CircuitPermitStateError(RuntimeError):
    """Permit 被重复完成或不再属于 Breaker 时引发。"""


class CircuitPermit:
    """一次 Circuit 调用许可；只能以一种 Health Outcome 完成一次。"""

    def __init__(
        self,
        breaker: "ModelCircuitBreaker",
        permit_id: str,
        *,
        half_open_probe: bool,
    ) -> None:
        self._breaker = breaker
        self._permit_id = permit_id
        self.half_open_probe = half_open_probe
        self._lock = Lock()
        self._completed = False

    def _complete(self, outcome: str) -> None:
        with self._lock:
            if self._completed:
                raise CircuitPermitStateError("Circuit Permit 已完成")
            self._completed = True
        self._breaker._complete_permit(self._permit_id, outcome)

    def record_success(self) -> None:
        self._complete("success")

    def record_failure(self) -> None:
        self._complete("failure")

    def record_indeterminate(self) -> None:
        """Provider 已开始但健康结果不确定时完成 Permit。"""
        self._complete("indeterminate")

    def abandon(self) -> None:
        """仅用于 Provider 从未开始的调用。"""
        self._complete("abandon")


class ModelCircuitBreaker:
    """使用连续合格失败计数的单进程 Circuit Breaker。"""

    def __init__(
        self,
        breaker_key: str,
        config: ModelCircuitBreakerConfig | None = None,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not breaker_key or not breaker_key.strip():
            raise ValueError("breaker_key 必须是非空字符串")
        self.breaker_key = breaker_key
        self.config = config or ModelCircuitBreakerConfig()
        self._clock = clock
        self._lock = Lock()
        self._state = ModelCircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._active_permits: dict[str, bool] = {}

    def _transition_if_recovered_locked(self, now: float) -> None:
        if (
            self._state == ModelCircuitState.OPEN
            and self._opened_at is not None
            and now - self._opened_at >= self.config.recovery_timeout_seconds
        ):
            self._state = ModelCircuitState.HALF_OPEN

    def acquire_permission(self) -> CircuitPermit:
        """同步取得许可；锁内不执行 Provider 调用。"""
        with self._lock:
            self._transition_if_recovered_locked(self._clock())
            if self._state == ModelCircuitState.OPEN:
                raise CircuitOpenError(self.breaker_key)
            probe = self._state == ModelCircuitState.HALF_OPEN
            if probe:
                active_probes = sum(self._active_permits.values())
                if active_probes >= self.config.half_open_max_calls:
                    raise CircuitOpenError(self.breaker_key)
            permit_id = uuid4().hex
            self._active_permits[permit_id] = probe
        return CircuitPermit(self, permit_id, half_open_probe=probe)

    def _complete_permit(self, permit_id: str, outcome: str) -> None:
        with self._lock:
            probe = self._active_permits.pop(permit_id, None)
            if probe is None:
                raise CircuitPermitStateError("Circuit Permit 未知或已完成")
            if outcome == "abandon":
                return
            if outcome == "indeterminate":
                # CLOSED 不修改连续基础设施失败；HALF_OPEN 无法证明恢复，
                # 释放 Probe 后保守回到 OPEN，避免永久占用或误判健康。
                if probe or self._state == ModelCircuitState.HALF_OPEN:
                    self._state = ModelCircuitState.OPEN
                    self._opened_at = self._clock()
                return
            if outcome == "success":
                self._state = ModelCircuitState.CLOSED
                self._consecutive_failures = 0
                self._opened_at = None
                return
            if outcome != "failure":
                raise CircuitPermitStateError("Circuit Permit 结果无效")
            if probe or self._state == ModelCircuitState.HALF_OPEN:
                self._state = ModelCircuitState.OPEN
                self._opened_at = self._clock()
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.config.failure_threshold:
                self._state = ModelCircuitState.OPEN
                self._opened_at = self._clock()

    def snapshot(self) -> ModelCircuitBreakerSnapshot:
        with self._lock:
            self._transition_if_recovered_locked(self._clock())
            return ModelCircuitBreakerSnapshot(
                breaker_key=self.breaker_key,
                state=self._state,
                consecutive_failures=self._consecutive_failures,
                half_open_active_calls=sum(self._active_permits.values()),
                failure_threshold=self.config.failure_threshold,
                recovery_timeout_seconds=self.config.recovery_timeout_seconds,
                half_open_max_calls=self.config.half_open_max_calls,
            )


class ModelCircuitBreakerRegistry:
    """应用生命周期持有的 Breaker Registry，可被多个 Run 共享。"""

    def __init__(
        self,
        config: ModelCircuitBreakerConfig | None = None,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.config = config or ModelCircuitBreakerConfig()
        self._clock = clock
        self._lock = Lock()
        self._breakers: dict[str, ModelCircuitBreaker] = {}

    def get(self, breaker_key: str) -> ModelCircuitBreaker:
        with self._lock:
            breaker = self._breakers.get(breaker_key)
            if breaker is None:
                breaker = ModelCircuitBreaker(
                    breaker_key,
                    self.config,
                    clock=self._clock,
                )
                self._breakers[breaker_key] = breaker
            return breaker

    def snapshots(
        self, breaker_keys: tuple[str, ...]
    ) -> dict[str, ModelCircuitBreakerSnapshot]:
        return {key: self.get(key).snapshot() for key in dict.fromkeys(breaker_keys)}
