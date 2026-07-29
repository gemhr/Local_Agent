#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""绑定 Run/Step 身份并发布强类型 Runtime Event。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import Any

from core.runtime.event_channel import RuntimeEventChannel
from core.runtime.events import (
    RuntimeEvent,
    RuntimeEventDraft,
    RuntimeEventPayload,
    RuntimeEventType,
)
from core.runtime.tracing import current_trace_context


class EventEmitterSyncError(RuntimeError):
    """同步 Emitter 被错误线程或不可用 Event Loop 调用。"""

    def __init__(self, error_code: str, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code})")


class RunEventEmitter:
    """固定 run_id、trace_id、Channel 和事件循环的 Publisher。"""

    def __init__(
        self,
        *,
        run_id: str,
        trace_id: str,
        channel: RuntimeEventChannel,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        if channel.run_id != run_id:
            raise ValueError("Emitter 与 Channel 的 run_id 必须一致")
        self.run_id = run_id
        self.trace_id = trace_id
        self.channel = channel
        self._loop = loop or asyncio.get_running_loop()
        self._steps: dict[str, StepEventEmitter] = {}
        self._steps_lock = threading.Lock()

    def for_step(self, step_id: str) -> "StepEventEmitter":
        """同一 Step 始终返回同一个序列所有者。"""
        # 这里只保护缓存身份，不调用业务代码，也不等待 Queue backpressure。
        with self._steps_lock:
            emitter = self._steps.get(step_id)
            if emitter is None:
                emitter = StepEventEmitter(parent=self, step_id=step_id)
                self._steps[step_id] = emitter
            return emitter

    async def emit(
        self,
        event_type: RuntimeEventType,
        payload: RuntimeEventPayload,
        *,
        component: str,
        ignore_run_cancellation: bool = False,
    ) -> RuntimeEvent:
        span = current_trace_context()
        return await self.channel.publish(
            RuntimeEventDraft(
                run_id=self.run_id,
                trace_id=self.trace_id,
                event_type=event_type,
                component=component,
                payload=payload,
                span_id=span.span_id if span else None,
                parent_span_id=span.parent_span_id if span else None,
            ),
            ignore_run_cancellation=ignore_run_cancellation,
        )

    def emit_from_worker(
        self,
        event_type: RuntimeEventType,
        payload: RuntimeEventPayload,
        *,
        component: str,
    ) -> RuntimeEvent:
        """同步 Worker 线程通过所属 Event Loop 参与同一 backpressure。"""
        return self._submit_from_worker(
            lambda: self.emit(event_type, payload, component=component)
        )

    def _submit_from_worker(
        self,
        coroutine_factory: Callable[[], Coroutine[Any, Any, RuntimeEvent]],
    ) -> RuntimeEvent:
        """仅允许非 Owner Loop 线程同步提交，避免等待自身形成死锁。"""
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is not None:
            raise EventEmitterSyncError(
                (
                    "EMITTER_OWNER_LOOP_SYNC_CALL"
                    if running_loop is self._loop
                    else "EMITTER_ASYNC_LOOP_SYNC_CALL"
                ),
                "Event Loop 线程中不能使用同步 emit API",
            )
        if self._loop.is_closed() or not self._loop.is_running():
            raise EventEmitterSyncError(
                "EMITTER_EVENT_LOOP_UNAVAILABLE",
                "Event Emitter 所属 Event Loop 不可用",
            )

        coroutine = coroutine_factory()
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except RuntimeError as exc:
            coroutine.close()
            raise EventEmitterSyncError(
                "EMITTER_EVENT_LOOP_UNAVAILABLE",
                "Event Emitter 所属 Event Loop 不可用",
            ) from exc
        try:
            return future.result()
        except RuntimeError as exc:
            if self._loop.is_closed() or not self._loop.is_running():
                raise EventEmitterSyncError(
                    "EMITTER_EVENT_LOOP_UNAVAILABLE",
                    "Event Emitter 所属 Event Loop 不可用",
                ) from exc
            raise


class StepEventEmitter:
    """固定 Step 身份并严格递增 step_sequence。"""

    def __init__(self, *, parent: RunEventEmitter, step_id: str) -> None:
        if not isinstance(step_id, str) or not step_id.strip():
            raise ValueError("step_id 必须是非空字符串")
        self.parent = parent
        self.step_id = step_id
        self._sequence = 0
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def emit(
        self,
        event_type: RuntimeEventType,
        payload: RuntimeEventPayload,
        *,
        component: str,
        close: bool = False,
        ignore_run_cancellation: bool = False,
    ) -> RuntimeEvent:
        async with self._lock:
            if self._closed:
                raise RuntimeError("StepEventEmitter 已关闭")
            step_sequence = self._sequence + 1
            span = current_trace_context()
            event = await self.parent.channel.publish(
                RuntimeEventDraft(
                    run_id=self.parent.run_id,
                    trace_id=self.parent.trace_id,
                    event_type=event_type,
                    component=component,
                    payload=payload,
                    step_id=self.step_id,
                    step_sequence=step_sequence,
                    span_id=span.span_id if span else None,
                    parent_span_id=span.parent_span_id if span else None,
                ),
                ignore_run_cancellation=ignore_run_cancellation,
            )
            self._sequence = step_sequence
            if close:
                self._closed = True
            return event

    def emit_from_worker(
        self,
        event_type: RuntimeEventType,
        payload: RuntimeEventPayload,
        *,
        component: str,
    ) -> RuntimeEvent:
        return self.parent._submit_from_worker(
            lambda: self.emit(event_type, payload, component=component)
        )
