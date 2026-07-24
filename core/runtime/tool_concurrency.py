#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""应用级 Tool 并发许可与单进程 Resource Key 租约。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from math import isfinite
import threading
import time
from typing import Callable

from core.runtime.cancellation import CancellationToken


class ToolResourceAcquireError(RuntimeError):
    """资源等待在调用开始前失败；不包含 Resource Key 正文。"""

    def __init__(self, safe_error_code: str, safe_message: str) -> None:
        self.safe_error_code = safe_error_code
        self.safe_message = safe_message
        super().__init__(safe_message)


@dataclass(frozen=True, slots=True)
class ToolWorkerRecord:
    """只保存 Detached Worker 生命周期所需的安全元数据。"""

    invocation_id: str
    attempt_id: str
    started_at: datetime
    tool_name: str
    resource_key_digest: str | None
    cleanup_state: str = "ACTIVE"

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "invocation_id": self.invocation_id,
            "attempt_id": self.attempt_id,
            "started_at": self.started_at.astimezone(UTC).isoformat(),
            "tool_name": self.tool_name,
            "resource_key_digest": self.resource_key_digest,
            "cleanup_state": self.cleanup_state,
        }


@dataclass(slots=True)
class ToolResourceLease:
    """一次 Attempt 的组合许可；release 幂等且可由后台清理线程调用。"""

    _controller: "ToolConcurrencyController"
    _tool_name: str
    _resource_key: str | None
    _tool_semaphore: threading.BoundedSemaphore
    _released: bool = False
    _release_lock: threading.Lock = field(default_factory=threading.Lock)

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self._controller._release(
            self._tool_name, self._resource_key, self._tool_semaphore
        )

    async def __aenter__(self) -> "ToolResourceLease":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()


class ToolConcurrencyController:
    """线程安全、Event Loop 无关的进程内 Controller，不承担调度职责。"""

    def __init__(self, max_concurrency: int = 16, *, poll_seconds: float = 0.01) -> None:
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency <= 0
        ):
            raise ValueError("max_concurrency 必须是正整数")
        self.max_concurrency = max_concurrency
        self._poll_seconds = poll_seconds
        self._global = threading.BoundedSemaphore(max_concurrency)
        self._lock = threading.Lock()
        self._workers_idle = threading.Condition(self._lock)
        self._tool_semaphores: dict[str, tuple[int, threading.BoundedSemaphore]] = {}
        self._held_resources: set[str] = set()
        self._workers: dict[str, ToolWorkerRecord] = {}

    async def acquire(
        self,
        *,
        tool_name: str,
        tool_max_concurrency: int,
        resource_key: str | None,
        cancellation_token: CancellationToken,
        remaining_seconds: Callable[[], float | None],
    ) -> ToolResourceLease:
        tool_semaphore = self._tool_semaphore(tool_name, tool_max_concurrency)
        global_acquired = False
        tool_acquired = False
        resource_acquired = False
        try:
            await self._acquire_semaphore(
                self._global, cancellation_token, remaining_seconds
            )
            global_acquired = True
            await self._acquire_semaphore(
                tool_semaphore, cancellation_token, remaining_seconds
            )
            tool_acquired = True
            if resource_key is not None:
                await self._acquire_resource(
                    resource_key, cancellation_token, remaining_seconds
                )
                resource_acquired = True
            cancellation_token.raise_if_cancelled()
            if _expired(remaining_seconds):
                raise ToolResourceAcquireError(
                    "TOOL_RESOURCE_WAIT_TIMEOUT",
                    "等待 Tool 资源时可用时间已耗尽。",
                )
            return ToolResourceLease(
                self, tool_name, resource_key, tool_semaphore
            )
        except BaseException:
            if resource_acquired:
                with self._lock:
                    self._held_resources.discard(resource_key)
            if tool_acquired:
                tool_semaphore.release()
            if global_acquired:
                self._global.release()
            raise

    def _tool_semaphore(
        self, tool_name: str, maximum: int
    ) -> threading.BoundedSemaphore:
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            raise ValueError("tool_max_concurrency 必须是正整数")
        with self._lock:
            current = self._tool_semaphores.get(tool_name)
            if current is None:
                semaphore = threading.BoundedSemaphore(maximum)
                self._tool_semaphores[tool_name] = (maximum, semaphore)
                return semaphore
            configured, semaphore = current
            if configured != maximum:
                raise ValueError("同一 Tool 的 max_concurrency 声明不一致")
            return semaphore

    async def _acquire_semaphore(
        self,
        semaphore: threading.BoundedSemaphore,
        cancellation_token: CancellationToken,
        remaining_seconds: Callable[[], float | None],
    ) -> None:
        while True:
            cancellation_token.raise_if_cancelled()
            if _expired(remaining_seconds):
                raise ToolResourceAcquireError(
                    "TOOL_RESOURCE_WAIT_TIMEOUT",
                    "等待 Tool 并发许可时可用时间已耗尽。",
                )
            acquired = await asyncio.to_thread(
                semaphore.acquire, True, self._wait_slice(remaining_seconds)
            )
            if acquired:
                return

    async def _acquire_resource(
        self,
        resource_key: str,
        cancellation_token: CancellationToken,
        remaining_seconds: Callable[[], float | None],
    ) -> None:
        while True:
            cancellation_token.raise_if_cancelled()
            if _expired(remaining_seconds):
                raise ToolResourceAcquireError(
                    "TOOL_RESOURCE_CONFLICT",
                    "等待相同 Resource Key 的租约时可用时间已耗尽。",
                )
            with self._lock:
                if resource_key not in self._held_resources:
                    self._held_resources.add(resource_key)
                    return
            await asyncio.sleep(self._wait_slice(remaining_seconds))

    def _wait_slice(self, remaining_seconds: Callable[[], float | None]) -> float:
        remaining = remaining_seconds()
        if remaining is None:
            return self._poll_seconds
        return max(0.0001, min(self._poll_seconds, remaining))

    def _release(
        self,
        tool_name: str,
        resource_key: str | None,
        tool_semaphore: threading.BoundedSemaphore,
    ) -> None:
        if resource_key is not None:
            with self._lock:
                self._held_resources.discard(resource_key)
        tool_semaphore.release()
        self._global.release()

    def is_resource_held(self, resource_key: str) -> bool:
        with self._lock:
            return resource_key in self._held_resources

    def register_worker(
        self,
        *,
        invocation_id: str,
        attempt_id: str,
        started_at: datetime,
        tool_name: str,
        resource_key_digest: str | None,
    ) -> None:
        """登记同步 Worker；不保存 arguments、output 或异常。"""
        record = ToolWorkerRecord(
            invocation_id=invocation_id,
            attempt_id=attempt_id,
            started_at=started_at,
            tool_name=tool_name,
            resource_key_digest=resource_key_digest,
        )
        with self._workers_idle:
            if attempt_id in self._workers:
                raise RuntimeError("同一 Attempt Worker 不能重复登记")
            self._workers[attempt_id] = record

    def mark_worker_detached(self, attempt_id: str) -> bool:
        with self._workers_idle:
            record = self._workers.get(attempt_id)
            if record is None:
                return False
            if record.cleanup_state == "DETACHED":
                return False
            self._workers[attempt_id] = replace(
                record, cleanup_state="DETACHED"
            )
            return True

    def complete_worker(self, attempt_id: str) -> bool:
        """Future callback 可重复调用；只有第一次注销并唤醒等待者。"""
        with self._workers_idle:
            removed = self._workers.pop(attempt_id, None)
            if removed is None:
                return False
            self._workers_idle.notify_all()
            return True

    @property
    def active_worker_count(self) -> int:
        with self._lock:
            return len(self._workers)

    @property
    def detached_worker_count(self) -> int:
        with self._lock:
            return sum(
                record.cleanup_state == "DETACHED"
                for record in self._workers.values()
            )

    def worker_snapshot(self) -> dict[str, object]:
        with self._lock:
            records = tuple(
                sorted(self._workers.values(), key=lambda item: item.attempt_id)
            )
            return {
                "active_worker_count": len(records),
                "detached_worker_count": sum(
                    item.cleanup_state == "DETACHED" for item in records
                ),
                "workers": tuple(item.to_safe_dict() for item in records),
            }

    def wait_until_idle(self, timeout: float | None = None) -> bool:
        """等待全部同步 Worker 结束；Shutdown 可在 RunRegistry 后调用。"""
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("timeout 必须是有限非负数或 None")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._workers_idle:
            while self._workers:
                if deadline is None:
                    self._workers_idle.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._workers_idle.wait(remaining)
            return True


def _expired(remaining_seconds: Callable[[], float | None]) -> bool:
    remaining = remaining_seconds()
    return remaining is not None and remaining <= 0


__all__ = [
    "ToolConcurrencyController",
    "ToolResourceAcquireError",
    "ToolResourceLease",
    "ToolWorkerRecord",
]
