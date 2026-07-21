#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单次 LocalAgent 运行使用的协作式取消基础组件。"""

from __future__ import annotations

import threading
from collections.abc import Callable


class RunCancelledError(RuntimeError):
    """运行发现协作式取消请求时引发。"""


class CancellationToken:
    """协作式取消请求的只读视图。"""

    def __init__(self, event: threading.Event, reason_getter: Callable[[], str | None]) -> None:
        self._event = event
        self._reason_getter = reason_getter

    def is_cancelled(self) -> bool:
        """返回是否已请求取消。"""
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        """返回首次提供的取消原因；未提供时返回 None。"""
        reason = self._reason_getter()
        return str(reason) if reason is not None else None

    def raise_if_cancelled(self) -> None:
        """已请求取消时引发异常。"""
        if self.is_cancelled():
            reason = self.reason or "run cancelled"
            raise RunCancelledError(reason)


class CancellationSource:
    """持有请求协作式取消的控制权。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None
        self.token = CancellationToken(self._event, self._get_reason)

    def cancel(self, reason: str | None = None) -> bool:
        """请求一次取消；仅首次请求返回 True。"""
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason or "run cancelled"
            self._event.set()
            return True

    def _get_reason(self) -> str | None:
        with self._lock:
            return self._reason
