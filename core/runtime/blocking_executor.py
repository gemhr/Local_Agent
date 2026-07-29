#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""应用级有界同步调用执行器；Tracker 只保存安全生命周期元数据。"""

from __future__ import annotations

import concurrent.futures
import contextvars
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Callable, Generic, TypeVar
from uuid import uuid4

from core.runtime.observability import (
    NoopRuntimeInfrastructureMetricsHook,
    RuntimeInfrastructureMetricsHook,
)

T = TypeVar("T")


class BlockingTaskKind(str, Enum):
    QUERY_REWRITE = "QUERY_REWRITE"
    EMBEDDING = "EMBEDDING"
    VECTOR_QUERY = "VECTOR_QUERY"
    KEYWORD_QUERY = "KEYWORD_QUERY"
    RERANK = "RERANK"
    DOCUMENT_LOAD = "DOCUMENT_LOAD"
    CONTEXT_BUILD = "CONTEXT_BUILD"


class BlockingTaskState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"


class BlockingExecutorClosedError(RuntimeError):
    """执行器已停止接收新任务。"""


class BlockingExecutorAdmissionTimeout(TimeoutError):
    """等待准入期间耗尽了调用方 Deadline。"""


class BlockingExecutorNestedSubmissionError(RuntimeError):
    """同一执行器的 Worker 不得同步提交子任务。"""


@dataclass(frozen=True, slots=True)
class BlockingTaskRecord:
    task_id: str
    kind: BlockingTaskKind
    run_id: str
    operation_id: str
    state: BlockingTaskState
    submitted_at: datetime
    started_at: datetime | None = None
    detached: bool = False


@dataclass(frozen=True, slots=True)
class BlockingExecutorSnapshot:
    max_workers: int
    max_pending_tasks: int
    active_count: int
    pending_count: int
    detached_count: int
    tasks: tuple[BlockingTaskRecord, ...]

    @property
    def admitted_count(self) -> int:
        return self.active_count + self.pending_count


@dataclass(frozen=True, slots=True)
class BlockingTaskWaitState:
    worker_terminated: bool
    execution_detached: bool
    background_work_pending: bool


class BlockingTaskHandle(Generic[T]):
    """Future 的受控视图；调用方不能绕过 Tracker 清理语义。"""

    def __init__(
        self,
        owner: "BoundedBlockingExecutor",
        task_id: str,
        future: concurrent.futures.Future[T],
    ) -> None:
        self._owner = owner
        self.task_id = task_id
        self._future = future

    def result(self, timeout: float | None = None) -> T:
        return self._future.result(timeout=timeout)

    def cancel_or_detach(self) -> BlockingTaskWaitState:
        if self._future.cancel():
            return BlockingTaskWaitState(True, False, False)
        if self._future.done():
            return BlockingTaskWaitState(True, False, False)
        self._owner._mark_detached(self.task_id)
        return BlockingTaskWaitState(False, True, True)

    def add_done_callback(self, callback: Callable[[], None]) -> None:
        """Register content-free cleanup work without exposing the Future."""
        self._future.add_done_callback(lambda _future: callback())


class BoundedBlockingExecutor:
    """同时限制运行中与排队中的同步调用数量。"""

    _worker_state = threading.local()

    def __init__(
        self,
        *,
        max_workers: int = 4,
        max_pending_tasks: int = 8,
        thread_name_prefix: str = "runtime-blocking",
        metrics_hook: RuntimeInfrastructureMetricsHook | None = None,
    ) -> None:
        for value, name in (
            (max_workers, "max_workers"),
            (max_pending_tasks, "max_pending_tasks"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if max_workers == 0:
            raise ValueError("max_workers 必须是正整数")
        self.max_workers = max_workers
        self.max_pending_tasks = max_pending_tasks
        self._admission = threading.BoundedSemaphore(
            max_workers + max_pending_tasks
        )
        self._lock = threading.RLock()
        self._idle = threading.Condition(self._lock)
        self._records: dict[str, BlockingTaskRecord] = {}
        self._accepting = True
        self._metrics_hook = metrics_hook or NoopRuntimeInfrastructureMetricsHook()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )

    def submit(
        self,
        operation: Callable[[], T],
        *,
        kind: BlockingTaskKind,
        run_id: str,
        operation_id: str,
        cancellation_check: Callable[[], None],
        remaining_seconds: Callable[[], float],
    ) -> BlockingTaskHandle[T]:
        """取得准入 Permit 后才提交；等待过程持续响应取消和 Deadline。"""
        if getattr(self._worker_state, "owner", None) is self:
            raise BlockingExecutorNestedSubmissionError(
                "BLOCKING_EXECUTOR_NESTED_SUBMISSION"
            )
        if not isinstance(kind, BlockingTaskKind):
            raise TypeError("kind 必须是 BlockingTaskKind")
        if not run_id.strip() or not operation_id.strip():
            raise ValueError("run_id 和 operation_id 不能为空")
        wait_started = time.perf_counter()
        while True:
            cancellation_check()
            remaining = remaining_seconds()
            if remaining <= 0:
                raise BlockingExecutorAdmissionTimeout(
                    "blocking executor admission timed out"
                )
            with self._lock:
                if not self._accepting:
                    raise BlockingExecutorClosedError("执行器已关闭")
            if self._admission.acquire(timeout=min(0.05, remaining)):
                break
        try:
            self._metrics_hook.blocking_executor_wait_observed(
                duration_seconds=time.perf_counter() - wait_started
            )
        except Exception:
            pass

        task_id = uuid4().hex
        record = BlockingTaskRecord(
            task_id=task_id,
            kind=kind,
            run_id=run_id,
            operation_id=operation_id,
            state=BlockingTaskState.PENDING,
            submitted_at=datetime.now(UTC),
        )
        with self._lock:
            if not self._accepting:
                self._admission.release()
                raise BlockingExecutorClosedError("执行器已关闭")
            self._records[task_id] = record
        try:
            # ThreadPoolExecutor does not propagate ContextVar automatically.
            # Capture at submission so worker logs/events retain the submitting span.
            captured_context = contextvars.copy_context()
            future = self._executor.submit(
                self._run, task_id, lambda: captured_context.run(operation)
            )
        except BaseException:
            with self._idle:
                self._records.pop(task_id, None)
                self._idle.notify_all()
            self._admission.release()
            raise
        future.add_done_callback(lambda _future: self._complete(task_id))
        return BlockingTaskHandle(self, task_id, future)

    def set_metrics_hook(
        self, metrics_hook: RuntimeInfrastructureMetricsHook
    ) -> None:
        """应用装配期注入 Hook；Hook 失败不影响任务执行。"""
        self._metrics_hook = metrics_hook

    def snapshot(self) -> BlockingExecutorSnapshot:
        with self._lock:
            tasks = tuple(
                sorted(self._records.values(), key=lambda item: item.task_id)
            )
            return BlockingExecutorSnapshot(
                max_workers=self.max_workers,
                max_pending_tasks=self.max_pending_tasks,
                active_count=sum(
                    item.state == BlockingTaskState.RUNNING for item in tasks
                ),
                pending_count=sum(
                    item.state == BlockingTaskState.PENDING for item in tasks
                ),
                detached_count=sum(item.detached for item in tasks),
                tasks=tasks,
            )

    def wait_until_idle(self, timeout: float) -> bool:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout < 0
        ):
            raise ValueError("timeout 必须是非负数")
        deadline = time.monotonic() + float(timeout)
        with self._idle:
            while self._records:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle.wait(remaining)
            return True

    def shutdown(self, *, wait: bool = True, timeout: float = 30.0) -> bool:
        """停止准入；可有界等待真实 Worker 结束并完成 Permit Cleanup。"""
        with self._lock:
            self._accepting = False
        idle = self.wait_until_idle(timeout) if wait else not self._records
        self._executor.shutdown(wait=idle and wait, cancel_futures=False)
        return idle

    def _run(self, task_id: str, operation: Callable[[], T]) -> T:
        with self._lock:
            record = self._records.get(task_id)
            if record is not None:
                self._records[task_id] = replace(
                    record,
                    state=BlockingTaskState.RUNNING,
                    started_at=datetime.now(UTC),
                )
        previous_owner = getattr(self._worker_state, "owner", None)
        self._worker_state.owner = self
        try:
            return operation()
        finally:
            self._worker_state.owner = previous_owner

    def _mark_detached(self, task_id: str) -> None:
        with self._lock:
            record = self._records.get(task_id)
            if record is not None:
                self._records[task_id] = replace(record, detached=True)

    def _complete(self, task_id: str) -> None:
        with self._idle:
            removed = self._records.pop(task_id, None)
            if removed is not None:
                self._admission.release()
            self._idle.notify_all()


DEFAULT_BLOCKING_MAX_WORKERS = 4
DEFAULT_BLOCKING_MAX_PENDING_TASKS = 8
process_blocking_executor = BoundedBlockingExecutor(
    max_workers=DEFAULT_BLOCKING_MAX_WORKERS,
    max_pending_tasks=DEFAULT_BLOCKING_MAX_PENDING_TASKS,
)


__all__ = [
    "BlockingExecutorAdmissionTimeout",
    "BlockingExecutorClosedError",
    "BlockingExecutorNestedSubmissionError",
    "BlockingExecutorSnapshot",
    "BlockingTaskHandle",
    "BlockingTaskKind",
    "BlockingTaskRecord",
    "BlockingTaskState",
    "BlockingTaskWaitState",
    "BoundedBlockingExecutor",
    "DEFAULT_BLOCKING_MAX_PENDING_TASKS",
    "DEFAULT_BLOCKING_MAX_WORKERS",
    "process_blocking_executor",
]
