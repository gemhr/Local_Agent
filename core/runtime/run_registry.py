#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单进程活跃 Run 注册表，不保存任何业务正文。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from core.runtime.cancellation import CancellationReason, CancellationSource
from core.runtime.state import AgentState


@dataclass
class RunHandle:
    """定位同一运行的最小进程内句柄。"""

    run_id: str
    cancellation_source: CancellationSource
    agent_state: AgentState
    owner: str
    registered_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def snapshot(self) -> dict[str, str | None]:
        """输出仅含安全标识和生命周期数据的快照。"""
        return {"run_id": self.run_id, "owner": self.owner, "registered_at": self.registered_at.isoformat(),
                "cancelled": str(self.cancellation_source.token.is_cancelled()),
                "cancellation_reason": (self.cancellation_source.token.reason.value if isinstance(self.cancellation_source.token.reason, CancellationReason) else self.cancellation_source.token.reason)}


class RunRegistry:
    """进程级、线程安全、非持久化的活跃 Run 注册表。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._empty = threading.Condition(self._lock)
        self._handles: dict[str, RunHandle] = {}

    def register(self, handle: RunHandle) -> RunHandle:
        with self._lock:
            if handle.run_id in self._handles:
                raise ValueError("active run_id already registered")
            self._handles[handle.run_id] = handle
            return handle

    def get(self, run_id: str) -> RunHandle | None:
        with self._lock:
            return self._handles.get(run_id)

    def snapshot(self, run_id: str | None = None):
        with self._lock:
            if run_id is not None:
                handle = self._handles.get(run_id)
                return handle.snapshot() if handle else None
            return {key: value.snapshot() for key, value in self._handles.items()}

    def observability_snapshot(self) -> dict[str, int]:
        """返回低基数生命周期 Gauge，不暴露任何 run_id。"""
        with self._lock:
            return {
                "active_runs": len(self._handles),
                "active_steps": sum(
                    len(handle.agent_state.active_step_ids)
                    for handle in self._handles.values()
                ),
            }

    def cancel(self, run_id: str, reason: CancellationReason) -> bool | None:
        handle = self.get(run_id)
        return None if handle is None else handle.cancellation_source.cancel(reason)

    def unregister(self, run_id: str) -> bool:
        with self._lock:
            removed = self._handles.pop(run_id, None) is not None
            if not self._handles:
                self._empty.notify_all()
            return removed

    def cancel_all(self, reason: CancellationReason) -> tuple[str, ...]:
        with self._lock:
            handles = tuple(self._handles.items())
        return tuple(run_id for run_id, handle in handles if handle.cancellation_source.cancel(reason))

    def wait_until_empty(self, timeout_seconds: float) -> tuple[str, ...]:
        """有界等待注销完成；返回超时后仍活跃的安全 run_id。"""
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must not be negative")
        deadline = time.monotonic() + timeout_seconds
        with self._empty:
            while self._handles:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._empty.wait(remaining)
            return tuple(sorted(self._handles))


process_run_registry = RunRegistry()
