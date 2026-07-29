#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Per-run, content-free runtime activity tracking."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import threading
from typing import Iterator

from core.runtime.checkpoint_contract import RuntimeActivitySnapshot


_TRACKED_FIELDS = frozenset(
    {
        "model_attempts_active",
        "tool_attempts_active",
        "retrievals_active",
        "detached_tool_workers",
        "detached_retrieval_workers",
        "step_workers_active",
    }
)


class RuntimeActivityTracker:
    """One instance belongs to one run and never stores operation identifiers."""

    def __init__(self, run_id: str) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        self.run_id = run_id
        self._lock = threading.Lock()
        self._counts = {name: 0 for name in _TRACKED_FIELDS}
        self._unknown_sources: set[str] = set()

    @contextmanager
    def track(self, field_name: str) -> Iterator[None]:
        self.increment(field_name)
        try:
            yield
        finally:
            self.decrement(field_name)

    def increment(self, field_name: str) -> None:
        self._validate_field(field_name)
        with self._lock:
            self._counts[field_name] += 1

    def decrement(self, field_name: str) -> None:
        self._validate_field(field_name)
        with self._lock:
            if self._counts[field_name] <= 0:
                raise RuntimeError("runtime activity counter underflow")
            self._counts[field_name] -= 1

    def mark_unknown(self, source: str) -> None:
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be a non-empty string")
        with self._lock:
            self._unknown_sources.add(source)

    def clear_unknown(self, source: str) -> None:
        with self._lock:
            self._unknown_sources.discard(source)

    def counts(self) -> tuple[dict[str, int], bool]:
        with self._lock:
            return dict(self._counts), bool(self._unknown_sources)

    @staticmethod
    def _validate_field(field_name: str) -> None:
        if field_name not in _TRACKED_FIELDS:
            raise ValueError("unsupported runtime activity field")


class RuntimeActivityProvider:
    """Combines real owners into one immutable, per-run activity snapshot."""

    def __init__(
        self,
        *,
        run_id: str,
        tracker: RuntimeActivityTracker,
        claim_gate,
        agent_state,
        budget_ledger,
        event_channel=None,
    ) -> None:
        if tracker.run_id != run_id:
            raise ValueError("activity tracker run_id mismatch")
        self.run_id = run_id
        self.tracker = tracker
        self.claim_gate = claim_gate
        self.agent_state = agent_state
        self.budget_ledger = budget_ledger
        self.event_channel = event_channel

    def capture(self) -> RuntimeActivitySnapshot:
        state_copy = self.agent_state.snapshot_copy()
        budget = self.budget_ledger.snapshot()
        gate = self.claim_gate.snapshot()
        counts, unknown = self.tracker.counts()
        publications = (
            self.event_channel.publications_in_flight
            if self.event_channel is not None
            else 0
        )
        if self.event_channel is None:
            unknown = True
        return RuntimeActivitySnapshot(
            claim_in_progress=gate.claim_in_progress,
            running_step_count=sum(
                step.status.value == "RUNNING" for step in state_copy.steps.values()
            ),
            budget_reservation_count=budget.active_reservation_count,
            model_attempts_active=counts["model_attempts_active"],
            tool_attempts_active=counts["tool_attempts_active"],
            retrievals_active=counts["retrievals_active"],
            detached_tool_workers=counts["detached_tool_workers"],
            detached_retrieval_workers=counts["detached_retrieval_workers"],
            event_publications_in_flight=publications,
            step_workers_active=counts["step_workers_active"],
            activity_unknown=unknown,
            captured_at=datetime.now(UTC),
        )


__all__ = ["RuntimeActivityProvider", "RuntimeActivityTracker"]
