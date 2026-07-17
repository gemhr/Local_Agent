#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Cooperative cancellation primitives for a single LocalAgent run."""

from __future__ import annotations

import threading
from collections.abc import Callable


class RunCancelledError(RuntimeError):
    """Raised when a run observes a cooperative cancellation request."""


class CancellationToken:
    """Read-only view of a cooperative cancellation request."""

    def __init__(self, event: threading.Event, reason_getter: Callable[[], str | None]) -> None:
        self._event = event
        self._reason_getter = reason_getter

    def is_cancelled(self) -> bool:
        """Return whether cancellation was requested."""
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        """Return the first cancellation reason, if one was supplied."""
        reason = self._reason_getter()
        return str(reason) if reason is not None else None

    def raise_if_cancelled(self) -> None:
        """Raise when cancellation has been requested."""
        if self.is_cancelled():
            reason = self.reason or "run cancelled"
            raise RunCancelledError(reason)


class CancellationSource:
    """Owns the authority to request cooperative cancellation."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None
        self.token = CancellationToken(self._event, self._get_reason)

    def cancel(self, reason: str | None = None) -> bool:
        """Request cancellation once and return True only for the first request."""
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason or "run cancelled"
            self._event.set()
            return True

    def _get_reason(self) -> str | None:
        with self._lock:
            return self._reason
