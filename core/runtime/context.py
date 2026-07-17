#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Minimal run context data and deadline handling for LocalAgent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
import math
import time
import uuid

from core.runtime.cancellation import CancellationSource, CancellationToken

LEGACY_DEFAULT_SESSION_ID = "legacy-default"


class RunDeadlineExceededError(TimeoutError):
    """Raised when a run deadline has expired."""


class Clock(Protocol):
    """Small clock abstraction used to test deadline calculations without sleep."""

    def utc_now(self) -> datetime:
        """Return the current timezone-aware UTC timestamp."""

    def monotonic(self) -> float:
        """Return the current monotonic clock value in seconds."""


class SystemClock:
    """Clock implementation backed by the Python standard library."""

    def utc_now(self) -> datetime:
        """Return the current timezone-aware UTC timestamp."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """Return the current monotonic clock value in seconds."""
        return time.monotonic()


@dataclass(frozen=True)
class RunIdentifiers:
    """Non-sensitive identifiers that distinguish run, session, and trace scopes."""

    run_id: str
    session_id: str
    trace_id: str

    def __post_init__(self) -> None:
        """Reject empty identifiers before they enter a run context."""
        if not self.run_id:
            raise ValueError("run_id must not be empty")
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if not self.trace_id:
            raise ValueError("trace_id must not be empty")


@dataclass(frozen=True)
class RunContextData:
    """Serializable run metadata safe to persist or emit in diagnostics."""

    identifiers: RunIdentifiers
    created_at: datetime
    deadline_at: datetime | None
    entry_agent_id: str

    def __post_init__(self) -> None:
        """Validate serializable run data invariants."""
        _ensure_utc_datetime(self.created_at, "created_at")
        if self.deadline_at is not None:
            _ensure_utc_datetime(self.deadline_at, "deadline_at")
        if not self.entry_agent_id:
            raise ValueError("entry_agent_id must not be empty")

    def to_dict(self) -> dict[str, str | None]:
        """Serialize only explicit data fields and never process-local dependencies."""
        return {
            "run_id": self.identifiers.run_id,
            "session_id": self.identifiers.session_id,
            "trace_id": self.identifiers.trace_id,
            "created_at": self.created_at.isoformat(),
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
            "entry_agent_id": self.entry_agent_id,
        }


class Deadline:
    """Pairs a serializable UTC deadline with a process-local monotonic deadline."""

    def __init__(self, timeout_seconds: float | None, clock: Clock) -> None:
        self._clock = clock
        if timeout_seconds is None:
            self.deadline_at: datetime | None = None
            self._monotonic_deadline: float | None = None
            return
        if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            raise ValueError("timeout_seconds must be a positive finite number when provided")
        self.deadline_at = clock.utc_now() + timedelta(seconds=timeout_seconds)
        self._monotonic_deadline = clock.monotonic() + timeout_seconds

    def remaining_seconds(self) -> float | None:
        """Return remaining seconds, None when no deadline exists, or zero after expiry."""
        if self._monotonic_deadline is None:
            return None
        return max(0.0, self._monotonic_deadline - self._clock.monotonic())

    def raise_if_expired(self) -> None:
        """Raise a clear exception when the deadline has already expired."""
        remaining = self.remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise RunDeadlineExceededError("run deadline exceeded")


class RunContext:
    """Per-run context carrying serializable data and explicit in-process dependencies."""

    def __init__(
        self,
        data: RunContextData,
        deadline: Deadline,
        cancellation_token: CancellationToken,
        clock: Clock,
    ) -> None:
        self.data = data
        self._deadline = deadline
        self._cancellation_token = cancellation_token
        self._clock = clock

    @classmethod
    def create(
        cls,
        *,
        entry_agent_id: str,
        session_id: str = LEGACY_DEFAULT_SESSION_ID,
        trace_id: str | None = None,
        timeout_seconds: float | None = None,
        cancellation_source: CancellationSource | None = None,
        clock: Clock | None = None,
    ) -> "RunContext":
        """Create a context only; prefer create_run_context when source ownership matters."""
        context, _source = create_run_context(
            entry_agent_id=entry_agent_id,
            session_id=session_id,
            trace_id=trace_id,
            timeout_seconds=timeout_seconds,
            cancellation_source=cancellation_source,
            clock=clock,
        )
        return context

    @property
    def run_id(self) -> str:
        """Return this run's unique identifier."""
        return self.data.identifiers.run_id

    @property
    def session_id(self) -> str:
        """Return the compatibility session identifier."""
        return self.data.identifiers.session_id

    @property
    def trace_id(self) -> str:
        """Return the end-to-end trace correlation identifier."""
        return self.data.identifiers.trace_id

    def remaining_seconds(self) -> float | None:
        """Return remaining deadline seconds or None when the run has no deadline."""
        return self._deadline.remaining_seconds()

    def raise_if_inactive(self) -> None:
        """Raise if cancellation was requested or the deadline expired."""
        self._cancellation_token.raise_if_cancelled()
        self._deadline.raise_if_expired()

    def to_dict(self) -> dict[str, str | None]:
        """Serialize only safe run metadata, excluding token, clock, locks, and events."""
        return self.data.to_dict()


def _ensure_utc_datetime(value: datetime, field_name: str) -> None:
    """Validate that a datetime is timezone-aware UTC."""
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be a timezone-aware UTC datetime")


def create_run_context(
    *,
    entry_agent_id: str,
    session_id: str = LEGACY_DEFAULT_SESSION_ID,
    trace_id: str | None = None,
    timeout_seconds: float | None = None,
    cancellation_source: CancellationSource | None = None,
    clock: Clock | None = None,
) -> tuple[RunContext, CancellationSource]:
    """Create a RunContext and return its CancellationSource to the caller-owner."""
    if not entry_agent_id:
        raise ValueError("entry_agent_id must not be empty")
    if not session_id:
        raise ValueError("session_id must not be empty")
    if trace_id is not None and not trace_id:
        raise ValueError("trace_id must not be empty")
    active_clock = clock or SystemClock()
    source = cancellation_source or CancellationSource()
    deadline = Deadline(timeout_seconds=timeout_seconds, clock=active_clock)
    identifiers = RunIdentifiers(
        run_id=uuid.uuid4().hex,
        session_id=session_id,
        trace_id=trace_id or uuid.uuid4().hex,
    )
    data = RunContextData(
        identifiers=identifiers,
        created_at=active_clock.utc_now(),
        deadline_at=deadline.deadline_at,
        entry_agent_id=entry_agent_id,
    )
    return (
        RunContext(data=data, deadline=deadline, cancellation_token=source.token, clock=active_clock),
        source,
    )
