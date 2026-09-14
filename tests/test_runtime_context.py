#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""最小 RunContext 基础组件的单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import math
import unittest

from core.runtime import (
    CancellationSource,
    DEFAULT_SESSION_ID,
    RunCancelledError,
    RunContext,
    RunDeadlineExceededError,
    RunIdentifiers,
    create_run_context,
)


class FakeClock:
    """用于确定性截止时间测试的可控时钟。"""

    def __init__(self) -> None:
        self.now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
        self.ticks = 100.0

    def utc_now(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.ticks

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.ticks += seconds


class RunContextTests(unittest.TestCase):
    def test_identifiers_are_non_empty_and_stable(self) -> None:
        context = RunContext.create(entry_agent_id="core_router", clock=FakeClock())
        self.assertTrue(context.run_id)
        self.assertTrue(context.trace_id)
        self.assertEqual(context.run_id, context.run_id)
        self.assertEqual(context.trace_id, context.trace_id)

    def test_different_runs_have_different_run_ids(self) -> None:
        first = RunContext.create(entry_agent_id="core_router", clock=FakeClock())
        second = RunContext.create(entry_agent_id="core_router", clock=FakeClock())
        self.assertNotEqual(first.run_id, second.run_id)

    def test_default_session_id_uses_legacy_strategy(self) -> None:
        context = RunContext.create(entry_agent_id="core_router", clock=FakeClock())
        self.assertEqual(context.session_id, DEFAULT_SESSION_ID)

    def test_no_deadline_has_no_remaining_seconds(self) -> None:
        context = RunContext.create(entry_agent_id="core_router", clock=FakeClock())
        self.assertIsNone(context.remaining_seconds())
        context.raise_if_inactive()

    def test_deadline_remaining_seconds_uses_fake_clock(self) -> None:
        clock = FakeClock()
        context = RunContext.create(entry_agent_id="core_router", timeout_seconds=10, clock=clock)
        self.assertAlmostEqual(context.remaining_seconds() or 0.0, 10.0)
        clock.advance(3.5)
        self.assertAlmostEqual(context.remaining_seconds() or 0.0, 6.5)

    def test_deadline_expiry_raises_clear_exception(self) -> None:
        clock = FakeClock()
        context = RunContext.create(entry_agent_id="core_router", timeout_seconds=1, clock=clock)
        clock.advance(1.1)
        with self.assertRaises(RunDeadlineExceededError):
            context.raise_if_inactive()

    def test_create_run_context_returns_context_and_source(self) -> None:
        context, source = create_run_context(entry_agent_id="core_router", clock=FakeClock())
        source.cancel("owned by caller")
        with self.assertRaises(RunCancelledError):
            context.raise_if_inactive()

    def test_zero_negative_nan_or_infinite_timeout_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RunContext.create(entry_agent_id="core_router", timeout_seconds=0, clock=FakeClock())
        with self.assertRaises(ValueError):
            RunContext.create(entry_agent_id="core_router", timeout_seconds=-1, clock=FakeClock())
        for invalid_timeout in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                RunContext.create(
                    entry_agent_id="core_router",
                    timeout_seconds=invalid_timeout,
                    clock=FakeClock(),
                )

    def test_cancellation_source_is_idempotent_and_preserves_reason(self) -> None:
        source = CancellationSource()
        self.assertTrue(source.cancel("first"))
        self.assertFalse(source.cancel("second"))
        self.assertTrue(source.token.is_cancelled())
        self.assertEqual(source.token.reason, "first")

    def test_token_raises_after_cancellation(self) -> None:
        source = CancellationSource()
        source.cancel("stop now")
        with self.assertRaises(RunCancelledError):
            source.token.raise_if_cancelled()

    def test_context_data_rejects_invalid_invariants(self) -> None:
        with self.assertRaises(ValueError):
            RunIdentifiers(run_id="", session_id="legacy-default", trace_id="trace")
        with self.assertRaises(ValueError):
            RunContext.create(entry_agent_id="", clock=FakeClock())
        with self.assertRaises(ValueError):
            RunContext.create(entry_agent_id="core_router", trace_id="", clock=FakeClock())

    def test_created_at_and_deadline_are_timezone_aware_utc(self) -> None:
        context = RunContext.create(entry_agent_id="core_router", timeout_seconds=5, clock=FakeClock())
        self.assertEqual(context.data.created_at.tzinfo, UTC)
        self.assertEqual(context.data.deadline_at.tzinfo, UTC)
        payload = context.to_dict()
        self.assertIsInstance(payload["created_at"], str)
        self.assertIsInstance(payload["deadline_at"], str)
        self.assertIn("+00:00", payload["created_at"] or "")

    def test_serialization_excludes_process_local_dependencies(self) -> None:
        context = RunContext.create(entry_agent_id="core_router", timeout_seconds=5, clock=FakeClock())
        payload = context.to_dict()
        self.assertEqual(
            set(payload),
            {"run_id", "session_id", "trace_id", "created_at", "deadline_at", "entry_agent_id"},
        )
        serialized_text = repr(payload).lower()
        self.assertNotIn("clock", serialized_text)
        self.assertNotIn("token", serialized_text)
        self.assertNotIn("event", serialized_text)
        self.assertNotIn("lock", serialized_text)

if __name__ == "__main__":
    unittest.main()
