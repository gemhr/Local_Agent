#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Runtime Observability 的窄接口与本地故障健康计数。"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol


class RuntimeInfrastructureMetricsHook(Protocol):
    """基础设施可安全调用的非递归 Metrics Hook。"""

    def journal_append_succeeded(
        self, *, duration_seconds: float, duplicate: bool
    ) -> None: ...

    def journal_append_failed(self, *, duration_seconds: float) -> None: ...

    def observability_record_dropped(self) -> None: ...

    def event_duplicate_observed(self, *, component: str) -> None: ...

    def blocking_executor_wait_observed(
        self, *, duration_seconds: float
    ) -> None: ...


class RuntimeGaugeProvider(Protocol):
    """返回无高基数 Label 的单进程实时 Gauge。"""

    def snapshot(self) -> dict[str, float]: ...


class NoopRuntimeInfrastructureMetricsHook:
    def journal_append_succeeded(
        self, *, duration_seconds: float, duplicate: bool
    ) -> None:
        return None

    def journal_append_failed(self, *, duration_seconds: float) -> None:
        return None

    def observability_record_dropped(self) -> None:
        return None

    def event_duplicate_observed(self, *, component: str) -> None:
        return None

    def blocking_executor_wait_observed(
        self, *, duration_seconds: float
    ) -> None:
        return None


class NoopRuntimeGaugeProvider:
    def snapshot(self) -> dict[str, float]:
        return {}


@dataclass(frozen=True, slots=True)
class ObservabilityHealthSnapshot:
    dropped_records: int
    logger_failures: int
    metrics_failures: int
    worker_failures: int
    duplicate_records: int
    record_failures: int
    flush_failures: int
    status: str
    last_safe_error_code: str | None


class ObservabilityHealth:
    """不依赖 Logger 或 Metrics sink 的进程内安全自健康计数。"""

    _KNOWN = {
        "dropped_records",
        "logger_failures",
        "metrics_failures",
        "worker_failures",
        "duplicate_records",
        "record_failures",
        "flush_failures",
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values = {name: 0 for name in self._KNOWN}
        self._last_safe_error_code: str | None = None

    def increment(self, name: str) -> None:
        if name not in self._KNOWN:
            raise ValueError("未知 Observability health counter")
        with self._lock:
            self._values[name] += 1

    def record_failure(self, name: str, safe_error_code: str) -> None:
        if name not in self._KNOWN:
            raise ValueError("unknown Observability health counter")
        if (
            not isinstance(safe_error_code, str)
            or not safe_error_code
            or len(safe_error_code) > 64
            or any(
                char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
                for char in safe_error_code
            )
        ):
            raise ValueError("safe_error_code must be a fixed safe token")
        with self._lock:
            self._values[name] += 1
            self._last_safe_error_code = safe_error_code

    def snapshot(self) -> ObservabilityHealthSnapshot:
        with self._lock:
            degraded = any(
                value
                for name, value in self._values.items()
                if name != "duplicate_records"
            )
            return ObservabilityHealthSnapshot(
                **self._values,
                status="DEGRADED" if degraded else "HEALTHY",
                last_safe_error_code=self._last_safe_error_code,
            )
