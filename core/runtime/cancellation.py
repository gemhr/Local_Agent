#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单次运行的协作式取消组件；首次取消原因不可覆盖。"""

from __future__ import annotations

import threading
import asyncio
from datetime import UTC, datetime
from enum import Enum


class CancellationReason(str, Enum):
    """运行级取消的固定、安全原因。"""

    USER_CANCELLED = "USER_CANCELLED"
    CLIENT_DISCONNECTED = "CLIENT_DISCONNECTED"
    SYSTEM_SHUTDOWN = "SYSTEM_SHUTDOWN"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"


class RunCancelledError(RuntimeError):
    """运行在合作式安全点发现取消请求时引发。"""

    def __init__(self, reason: CancellationReason | str = CancellationReason.USER_CANCELLED) -> None:
        self.reason = reason.value if isinstance(reason, CancellationReason) else str(reason)
        super().__init__(self.reason)


class CancellationToken:
    """取消源的只读视图；不暴露取消能力。"""

    def __init__(self, source: "CancellationSource") -> None:
        self._source = source

    def is_cancelled(self) -> bool:
        """兼容既有调用，返回是否已取消。"""
        return self._source._event.is_set()

    @property
    def reason(self) -> CancellationReason | str | None:
        """返回 first-wins 的取消原因。"""
        with self._source._lock:
            return self._source._reason

    @property
    def cancelled_at(self) -> datetime | None:
        """返回首次取消的 UTC 时间。"""
        with self._source._lock:
            return self._source._cancelled_at

    def raise_if_cancelled(self) -> None:
        """取消后抛出带固定原因的异常。"""
        reason = self.reason
        if reason is not None:
            raise RunCancelledError(reason)

    async def wait_cancelled(self) -> None:
        """异步等待取消，不占用事件循环；短超时确保取消 Task 可回收。"""
        while not self._source._event.is_set():
            await asyncio.to_thread(self._source._event.wait, 0.1)


class CancellationSource:
    """持有取消控制权，使用锁保证跨线程 first-wins。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: CancellationReason | str | None = None
        self._cancelled_at: datetime | None = None
        self.token = CancellationToken(self)

    def cancel(
        self,
        reason: CancellationReason | str = CancellationReason.USER_CANCELLED,
        occurred_at: datetime | None = None,
    ) -> bool:
        """请求取消。仅第一次成功，后续调用不覆盖原因或时间。"""
        # 保留旧调用方传入安全短字符串的兼容性；新运行级路径仅使用 Enum。
        parsed = reason if isinstance(reason, CancellationReason) else str(reason)
        timestamp = occurred_at or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware UTC datetime")
        timestamp = timestamp.astimezone(UTC)
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = parsed
            self._cancelled_at = timestamp
            self._event.set()
            return True
