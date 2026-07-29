#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Per-run admission gate around the Scheduler's real claim critical section."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import threading
import time
from typing import Iterator

from core.runtime.cancellation import CancellationToken
from core.runtime.checkpoint_contract import (
    SchedulerClaimGateSnapshot,
    SchedulerClaimGateState,
)


class SchedulerClaimGateBusyError(RuntimeError):
    error_code = "CLAIM_GATE_PAUSE_IN_PROGRESS"


class SchedulerClaimGateClosedError(RuntimeError):
    error_code = "CLAIM_GATE_CLOSED"


class SchedulerClaimGate:
    """Pauses only new claims; it never owns Step state or worker cancellation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = SchedulerClaimGateState.OPEN
        self._claim_in_progress = 0
        self._pause_owner = False

    @contextmanager
    def claim(self) -> Iterator[bool]:
        entered = self.enter_claim()
        try:
            yield entered
        finally:
            if entered:
                self.exit_claim()

    def enter_claim(self) -> bool:
        """Enter without blocking an event-loop thread; paused gates reject admission."""
        with self._lock:
            if self._state is SchedulerClaimGateState.CLOSED:
                raise SchedulerClaimGateClosedError("claim gate is closed")
            if self._state is not SchedulerClaimGateState.OPEN:
                return False
            self._claim_in_progress += 1
            return True

    def exit_claim(self) -> None:
        with self._lock:
            if self._claim_in_progress <= 0:
                raise RuntimeError("claim gate exit without matching entry")
            self._claim_in_progress -= 1

    async def pause(
        self,
        *,
        timeout: float | None,
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout < 0
        ):
            raise ValueError("timeout must be a non-negative number or None")
        with self._lock:
            if self._state is SchedulerClaimGateState.CLOSED:
                raise SchedulerClaimGateClosedError("claim gate is closed")
            if self._pause_owner or self._state is not SchedulerClaimGateState.OPEN:
                raise SchedulerClaimGateBusyError("claim gate pause already in progress")
            self._pause_owner = True
            self._state = SchedulerClaimGateState.PAUSING
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        try:
            while True:
                if cancellation_token is not None:
                    cancellation_token.raise_if_cancelled()
                with self._lock:
                    if self._claim_in_progress == 0:
                        self._state = SchedulerClaimGateState.PAUSED
                        return
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("claim gate pause timed out")
                await asyncio.sleep(0.001)
        except BaseException:
            with self._lock:
                if self._state is not SchedulerClaimGateState.CLOSED:
                    self._state = SchedulerClaimGateState.OPEN
                self._pause_owner = False
            raise

    def resume(self) -> None:
        with self._lock:
            if self._state is SchedulerClaimGateState.CLOSED:
                self._pause_owner = False
                return
            if self._state not in {
                SchedulerClaimGateState.PAUSED,
                SchedulerClaimGateState.PAUSING,
            }:
                self._pause_owner = False
                return
            self._state = SchedulerClaimGateState.RESUMING
            self._state = SchedulerClaimGateState.OPEN
            self._pause_owner = False

    def close(self) -> None:
        with self._lock:
            self._state = SchedulerClaimGateState.CLOSED
            self._pause_owner = False

    def snapshot(self) -> SchedulerClaimGateSnapshot:
        with self._lock:
            return SchedulerClaimGateSnapshot(
                state=self._state,
                claim_in_progress=self._claim_in_progress,
                captured_at=datetime.now(UTC),
            )


__all__ = [
    "SchedulerClaimGate",
    "SchedulerClaimGateBusyError",
    "SchedulerClaimGateClosedError",
]
