#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""非 Event Loop Worker Thread 到 Async Persistence 的有界桥。

为什么需要它：LocalAgent 的 Conversation Summary / Legacy Streaming 与
Agent 执行链是 **同步** 代码，运行在 ``BoundedBlockingExecutor`` 的 worker
线程上（Agent 执行本身要阻塞在模型 HTTP 调用上）。这些调用点必须继续使用
同步签名，而 PostgreSQL 持久化只提供 Awaitable 接口。

本桥把协程提交到 application event loop，并**阻塞 worker 线程**等待结果：

```text
worker thread --run_coroutine_threadsafe--> event loop --await--> asyncpg
```

关键边界（不伪造 coverage）：

* **ASGI event loop 永不执行数据库 I/O**；所有 DB I/O 都在 loop 上 await；
* 桥只在 worker 线程使用；若在 loop 线程被调用会 **fail closed**（否则必然
  自我死锁），错误码为 ``DATABASE_OPERATION_FAILED``；
* 等待有硬上限；超时会取消已提交的协程并抛出 typed timeout，不无限等待；
* 该桥不能让已经开始的 DB 调用被抢占——与既有 ``asyncio.to_thread`` 边界
  具有相同的不可中断限制。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, TypeVar

from core.persistence.errors import (
    DatabaseErrorCode,
    PersistenceError,
)

T = TypeVar("T")

# 默认等待上限：必须大于连接级 statement + lock timeout 之和，否则会在
# 数据库自身超时映射之前先被桥截断，丢失真实失败原因。
DEFAULT_BRIDGE_TIMEOUT_SECONDS = 60.0


class SyncPersistenceBridge:
    """把 async 持久化操作暴露给 worker 线程的有界同步边界。"""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        *,
        default_timeout_seconds: float = DEFAULT_BRIDGE_TIMEOUT_SECONDS,
    ) -> None:
        if (
            isinstance(default_timeout_seconds, bool)
            or not isinstance(default_timeout_seconds, (int, float))
            or not default_timeout_seconds > 0
        ):
            raise ValueError("default_timeout_seconds 必须为正数")
        self._loop = loop
        self._default_timeout_seconds = float(default_timeout_seconds)
        self._loop_thread_id: int | None = None
        if loop is not None:
            self.bind_loop(loop)

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    @property
    def bound(self) -> bool:
        return self._loop is not None and not self._loop.is_closed()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """在 application startup 绑定运行中的 loop 与其线程。"""
        if not isinstance(loop, asyncio.AbstractEventLoop):
            raise TypeError("loop 必须是 AbstractEventLoop")
        if loop.is_closed():
            raise ValueError("不能绑定已关闭的 event loop")
        self._loop = loop
        # ``_thread_id`` 是 CPython asyncio 的既有属性；不可用时退化为
        # 「运行中」判定，仍然 fail closed。
        self._loop_thread_id = getattr(loop, "_thread_id", None)

    def run(
        self,
        coroutine_factory: Callable[[], Any],
        *,
        operation: str,
        timeout_seconds: float | None = None,
    ) -> T:
        """在 loop 上执行 ``coroutine_factory()`` 并阻塞当前 worker 线程等待。"""
        if not callable(coroutine_factory):
            raise TypeError("coroutine_factory 必须可调用")
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("operation 必须是非空字符串")
        loop = self._loop
        if loop is None or loop.is_closed():
            raise PersistenceError(
                DatabaseErrorCode.DATABASE_UNAVAILABLE,
                operation=operation,
            )
        self._reject_loop_thread(operation)

        timeout = (
            self._default_timeout_seconds
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if not timeout > 0:
            raise ValueError("timeout_seconds 必须为正数")

        coroutine = coroutine_factory()
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except Exception:
            coroutine.close()
            raise PersistenceError(
                DatabaseErrorCode.DATABASE_UNAVAILABLE,
                operation=operation,
            ) from None
        try:
            return future.result(timeout=timeout)  # type: ignore[return-value]
        except FutureTimeoutError:
            # 取消已排队的协程；已经开始的 DB 调用仍由连接级 timeout 收敛。
            future.cancel()
            raise PersistenceError(
                DatabaseErrorCode.DATABASE_STATEMENT_TIMEOUT,
                operation=operation,
            ) from None
        except PersistenceError:
            raise
        except Exception as exc:
            from core.persistence.errors import to_persistence_error

            raise to_persistence_error(exc, operation=operation) from None

    def _reject_loop_thread(self, operation: str) -> None:
        """在 event loop 线程同步等待自己会死锁，必须 fail closed。"""
        thread_id = self._loop_thread_id
        if thread_id is not None:
            if threading.get_ident() == thread_id:
                raise PersistenceError(
                    DatabaseErrorCode.DATABASE_OPERATION_FAILED,
                    operation=operation,
                )
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise PersistenceError(
            DatabaseErrorCode.DATABASE_OPERATION_FAILED,
            operation=operation,
        )


__all__ = ["DEFAULT_BRIDGE_TIMEOUT_SECONDS", "SyncPersistenceBridge"]
