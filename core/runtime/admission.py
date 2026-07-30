"""Application-level admission control for new runtime requests."""

from __future__ import annotations

from contextlib import contextmanager
from enum import Enum
import threading
import time
import math
from typing import Iterator


class RuntimeAdmissionState(str, Enum):
    ACCEPTING = "ACCEPTING"
    DRAINING = "DRAINING"
    CLOSED = "CLOSED"


class RuntimeAdmissionRejectedError(RuntimeError):
    """Raised before any per-run object is created while shutdown is active."""

    error_code = "RUNTIME_SHUTTING_DOWN"

    def __init__(self) -> None:
        super().__init__(self.error_code)


class RuntimeAdmissionGate:
    """Thread-safe, idempotent application admission state machine.

    An admission lease covers the short construction/registration window.  A
    shutdown owner closes admission first and can then wait for these windows
    to settle before snapshotting the RunRegistry.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.Lock())
        self._state = RuntimeAdmissionState.ACCEPTING
        self._pending_admissions = 0

    @property
    def state(self) -> RuntimeAdmissionState:
        with self._condition:
            return self._state

    @property
    def accepts_new_runs(self) -> bool:
        return self.state is RuntimeAdmissionState.ACCEPTING

    @property
    def pending_admissions(self) -> int:
        with self._condition:
            return self._pending_admissions

    def acquire(self) -> None:
        with self._condition:
            if self._state is not RuntimeAdmissionState.ACCEPTING:
                raise RuntimeAdmissionRejectedError()
            self._pending_admissions += 1

    def release(self) -> None:
        with self._condition:
            if self._pending_admissions <= 0:
                raise RuntimeError("runtime admission lease is not active")
            self._pending_admissions -= 1
            if self._pending_admissions == 0:
                self._condition.notify_all()

    @contextmanager
    def admission(self) -> Iterator[None]:
        self.acquire()
        try:
            yield
        finally:
            self.release()

    def close_admission(self) -> bool:
        """Enter DRAINING once; repeated calls are harmless."""
        with self._condition:
            if self._state is not RuntimeAdmissionState.ACCEPTING:
                return False
            self._state = RuntimeAdmissionState.DRAINING
            self._condition.notify_all()
            return True

    def mark_closed(self) -> bool:
        with self._condition:
            if self._state is RuntimeAdmissionState.CLOSED:
                return False
            self._state = RuntimeAdmissionState.CLOSED
            self._condition.notify_all()
            return True

    def wait_until_settled(self, timeout: float) -> bool:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout < 0
        ):
            raise ValueError("timeout must be a non-negative finite number")
        deadline = time.monotonic() + float(timeout)
        with self._condition:
            while self._pending_admissions:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


__all__ = [
    "RuntimeAdmissionGate",
    "RuntimeAdmissionRejectedError",
    "RuntimeAdmissionState",
]
