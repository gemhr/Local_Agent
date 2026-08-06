#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Deterministic controller and lifecycle scope for test-only fault injection."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Awaitable, Callable, Collection, Mapping, Protocol

from core.runtime.fault_injection_contract import (
    FaultAction,
    FaultConfigurationCode,
    FaultDecision,
    FaultExecutionConfigurationError,
    FaultMatchContext,
    FaultPlan,
    FaultRule,
    FaultTrigger,
    InjectedFailureResult,
    InjectedFaultCode,
    InjectedFaultError,
    NO_FAULT_DECISION,
)
from core.runtime.fault_injection_recording import FaultInjectionRecorder


def evaluate_sync_fault(
    controller: "FaultInjectionController | None",
    *,
    point: FaultPoint,
    component: str,
    run_id: str | None = None,
    step_id: str | None = None,
    operation_kind: str | None = None,
    event_type: str | None = None,
    checkpoint_kind: str | None = None,
) -> None:
    """Run-scoped synchronous fault seam (raise-only).

    Deterministic Stage 2.5 seams (Store, OutputGate, Memory, Executor) run
    inside sync owners. Only RAISE_TYPED_ERROR is supported: DELAY/BLOCK
    would block an asyncio transport and are intentionally not offered here.
    """
    if controller is None:
        return
    controller.execute_blocking_if_matched(
        FaultMatchContext(
            fault_point=point,
            component=component,
            run_id_digest=(
                hashlib.sha256(run_id.encode("utf-8")).hexdigest()
                if run_id is not None
                else None
            ),
            step_id=step_id,
            operation_kind=operation_kind,
            event_type=event_type,
            checkpoint_kind=checkpoint_kind,
        ),
        allowed_actions={FaultAction.RAISE_TYPED_ERROR},
    )


class FaultClock(Protocol):
    def utc_now(self) -> datetime:
        """Return a timezone-aware UTC timestamp."""


class FaultSleeper(Protocol):
    async def sleep(self, delay_seconds: float) -> None:
        """Wait for the requested duration and remain cancellable."""

    async def aclose(self) -> None:
        """Release or cancel any test-owned waiting."""


class SystemFaultClock:
    def utc_now(self) -> datetime:
        return datetime.now(UTC)


class AsyncioFaultSleeper:
    def __init__(self) -> None:
        self._waiters: set[asyncio.Task[None]] = set()
        self._closed = False

    async def sleep(self, delay_seconds: float) -> None:
        if self._closed:
            raise asyncio.CancelledError
        waiter = asyncio.create_task(asyncio.sleep(delay_seconds))
        self._waiters.add(waiter)
        try:
            await waiter
        finally:
            self._waiters.discard(waiter)

    async def aclose(self) -> None:
        self._closed = True
        waiters = tuple(self._waiters)
        for waiter in waiters:
            waiter.cancel()
        if waiters:
            await asyncio.gather(*waiters, return_exceptions=True)


class ControllableFaultSleeper:
    """Deterministic test sleeper; no wall-clock sleeping is required."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.requested_delays: list[float] = []

    async def sleep(self, delay_seconds: float) -> None:
        self.requested_delays.append(delay_seconds)
        self.entered.set()
        await self.release.wait()

    async def aclose(self) -> None:
        self.release.set()


class FaultBlocker:
    """Bounded, cancellable test barrier kept outside immutable plans."""

    def __init__(self, *, timeout_seconds: float = 1.0) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive number")
        self.timeout_seconds = float(timeout_seconds)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._closed = False

    @property
    def timeout(self) -> float:
        return self.timeout_seconds

    async def wait(self) -> None:
        if self._closed:
            return
        self.entered.set()
        try:
            await asyncio.wait_for(
                self.release.wait(),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as exc:
            raise InjectedFaultError(InjectedFaultCode.INJECTED_TIMEOUT) from exc

    def close(self) -> None:
        self._closed = True
        self.release.set()

    def __repr__(self) -> str:
        return (
            "FaultBlocker("
            f"timeout_seconds={self.timeout_seconds}, "
            f"entered={self.entered.is_set()}, "
            f"released={self.release.is_set()}, "
            f"closed={self._closed}"
            ")"
        )


FixtureMutator = Callable[[str], object | Awaitable[object]]


@dataclass(frozen=True, slots=True)
class FaultRuleCounterSnapshot:
    rule_id: str
    match_count: int
    hit_count: int


@dataclass(frozen=True, slots=True)
class FaultControllerSnapshot:
    enabled: bool
    closed: bool
    plan_id: str | None
    plan_digest: str | None
    counters: tuple[FaultRuleCounterSnapshot, ...]


class FaultInjectionController:
    """Owns all mutable rule counters; it has no Runtime state dependency."""

    def __init__(
        self,
        plan: FaultPlan | None = None,
        *,
        enabled: bool | None = None,
        recorder: FaultInjectionRecorder | None = None,
        sleeper: FaultSleeper | None = None,
        blockers: Mapping[str, FaultBlocker] | None = None,
        fixture_mutator: FixtureMutator | None = None,
        clock: FaultClock | None = None,
    ) -> None:
        if plan is not None and not isinstance(plan, FaultPlan):
            raise TypeError("plan must be FaultPlan or None")
        if enabled is not None and not isinstance(enabled, bool):
            raise TypeError("enabled must be bool or None")
        self._plan = plan
        self._enabled = (plan is not None) if enabled is None else enabled
        if self._enabled and plan is None:
            raise ValueError("an enabled controller requires a plan")
        self._recorder = recorder
        self._sleeper = sleeper or AsyncioFaultSleeper()
        self._blockers = dict(blockers or {})
        self._fixture_mutator = fixture_mutator
        self._clock = clock or SystemFaultClock()
        self._match_counts = (
            {rule.rule_id: 0 for rule in plan.rules} if plan is not None else {}
        )
        self._hit_counts = (
            {rule.rule_id: 0 for rule in plan.rules} if plan is not None else {}
        )
        self._closed = False
        self._lock = threading.Lock()

    @classmethod
    def disabled(cls) -> "FaultInjectionController":
        return cls()

    @classmethod
    def for_test(
        cls,
        plan: FaultPlan,
        **kwargs: object,
    ) -> "FaultInjectionController":
        return cls(plan, enabled=True, **kwargs)

    def evaluate(self, context: FaultMatchContext) -> FaultDecision:
        if not isinstance(context, FaultMatchContext):
            raise TypeError("context must be FaultMatchContext")
        decision = NO_FAULT_DECISION
        with self._lock:
            if self._closed or not self._enabled or self._plan is None:
                return decision
            for rule in self._plan.rules:
                if not rule.enabled or not rule.matches(context):
                    continue
                match_ordinal = self._match_counts[rule.rule_id] + 1
                self._match_counts[rule.rule_id] = match_ordinal
                hit_count = self._hit_counts[rule.rule_id]
                if not self._should_trigger(rule, match_ordinal, hit_count):
                    continue
                hit_ordinal = hit_count + 1
                self._hit_counts[rule.rule_id] = hit_ordinal
                decision = FaultDecision(
                    matched=True,
                    rule_id=rule.rule_id,
                    fault_point=rule.fault_point,
                    action=rule.action,
                    match_ordinal=match_ordinal,
                    hit_ordinal=hit_ordinal,
                    safe_fault_code=rule.safe_fault_code,
                    triggered_at=self._clock.utc_now(),
                )
                break
        if decision.matched and self._recorder is not None:
            self._recorder.record(
                plan_id=self._plan.plan_id,
                component=context.component,
                decision=decision,
            )
        return decision

    @property
    def enabled(self) -> bool:
        """Cheap parity guard for explicit request-level seam adapters."""
        with self._lock:
            return self._enabled and not self._closed and self._plan is not None

    @staticmethod
    def _should_trigger(
        rule: FaultRule,
        match_ordinal: int,
        hit_count: int,
    ) -> bool:
        if hit_count >= rule.max_hits:
            return False
        if rule.trigger is FaultTrigger.ALWAYS:
            return True
        if rule.trigger is FaultTrigger.FIRST_MATCH:
            return match_ordinal == 1
        if rule.trigger is FaultTrigger.ON_NTH_MATCH:
            return match_ordinal == rule.match_number
        if rule.trigger is FaultTrigger.AFTER_N_MATCHES:
            return match_ordinal > rule.match_number
        return False

    def execute_blocking_if_matched(
        self,
        context: FaultMatchContext,
        *,
        raise_if_cancelled: Callable[[], None] | None = None,
        poll_interval_seconds: float = 0.01,
        allowed_actions: Collection[FaultAction] | None = None,
    ) -> FaultDecision | InjectedFailureResult:
        """Execute pre-call actions from an existing synchronous Runtime owner.

        Model and Retrieval are currently synchronous contracts.  Their owner
        supplies the cancellation check; this method never creates Retry,
        Fallback, Event, Journal, Trace, or domain state.
        """
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or not math.isfinite(float(poll_interval_seconds))
            or poll_interval_seconds <= 0
        ):
            raise ValueError("poll_interval_seconds must be a positive number")
        decision = self.evaluate(context)
        if not decision.matched:
            return decision
        rule = self._rule(decision.rule_id)
        if allowed_actions is not None and rule.action not in allowed_actions:
            raise FaultExecutionConfigurationError(
                FaultConfigurationCode.UNSUPPORTED_ACTION
            )

        def check_cancelled() -> None:
            if raise_if_cancelled is not None:
                raise_if_cancelled()

        if rule.action is FaultAction.RAISE_TYPED_ERROR:
            raise InjectedFaultError(rule.safe_fault_code)
        if rule.action is FaultAction.DELAY:
            deadline = time.monotonic() + float(rule.delay_seconds)
            while True:
                check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return decision
                time.sleep(min(float(poll_interval_seconds), remaining))
        if rule.action is FaultAction.BLOCK_UNTIL_RELEASED:
            blocker = self._blockers.get(rule.rule_id)
            if blocker is None:
                raise FaultExecutionConfigurationError(
                    FaultConfigurationCode.BLOCKER_REQUIRED
                )
            deadline = time.monotonic() + blocker.timeout
            blocker.entered.set()
            while not blocker.release.is_set():
                check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InjectedFaultError(InjectedFaultCode.INJECTED_TIMEOUT)
                time.sleep(min(float(poll_interval_seconds), remaining))
            return decision
        if rule.action is FaultAction.RETURN_TYPED_FAILURE:
            return InjectedFailureResult(rule.safe_fault_code)
        if rule.action is FaultAction.CORRUPT_TEST_FIXTURE:
            # Fixture mutation remains async-test-only and is deliberately not
            # connected to synchronous Model/Retrieval Runtime seams.
            raise FaultExecutionConfigurationError(
                FaultConfigurationCode.UNSUPPORTED_ACTION
            )
        return decision

    async def execute_if_matched(
        self,
        context: FaultMatchContext,
        *,
        allowed_actions: Collection[FaultAction] | None = None,
    ) -> FaultDecision | InjectedFailureResult:
        decision = self.evaluate(context)
        if not decision.matched:
            return decision
        rule = self._rule(decision.rule_id)
        if allowed_actions is not None and rule.action not in allowed_actions:
            raise FaultExecutionConfigurationError(
                FaultConfigurationCode.UNSUPPORTED_ACTION
            )
        if rule.action is FaultAction.RAISE_TYPED_ERROR:
            raise InjectedFaultError(rule.safe_fault_code)
        if rule.action is FaultAction.DELAY:
            await self._sleeper.sleep(float(rule.delay_seconds))
            return decision
        if rule.action is FaultAction.BLOCK_UNTIL_RELEASED:
            blocker = self._blockers.get(rule.rule_id)
            if blocker is None:
                raise FaultExecutionConfigurationError(
                    FaultConfigurationCode.BLOCKER_REQUIRED
                )
            await blocker.wait()
            return decision
        if rule.action is FaultAction.RETURN_TYPED_FAILURE:
            return InjectedFailureResult(rule.safe_fault_code)
        if rule.action is FaultAction.CORRUPT_TEST_FIXTURE:
            if self._fixture_mutator is None:
                raise FaultExecutionConfigurationError(
                    FaultConfigurationCode.FIXTURE_MUTATOR_REQUIRED
                )
            result = self._fixture_mutator(rule.fixture_mutation)
            if inspect.isawaitable(result):
                await result
            return decision
        return decision

    def _rule(self, rule_id: str) -> FaultRule:
        if self._plan is None:
            raise RuntimeError("FAULT_PLAN_UNAVAILABLE")
        return next(rule for rule in self._plan.rules if rule.rule_id == rule_id)

    def snapshot(self) -> FaultControllerSnapshot:
        with self._lock:
            counters = tuple(
                FaultRuleCounterSnapshot(
                    rule_id=rule_id,
                    match_count=self._match_counts[rule_id],
                    hit_count=self._hit_counts[rule_id],
                )
                for rule_id in sorted(self._match_counts)
            )
            return FaultControllerSnapshot(
                enabled=self._enabled,
                closed=self._closed,
                plan_id=self._plan.plan_id if self._plan else None,
                plan_digest=self._plan.digest if self._plan else None,
                counters=counters,
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def __repr__(self) -> str:
        snapshot = self.snapshot()
        return (
            "FaultInjectionController("
            f"enabled={snapshot.enabled}, "
            f"closed={snapshot.closed}, "
            f"plan_id={snapshot.plan_id}, "
            f"rule_count={len(snapshot.counters)}"
            ")"
        )


class FaultInjectionScope:
    """Explicit test lifecycle owner; never installed in module-global state."""

    def __init__(
        self,
        plan: FaultPlan,
        *,
        recorder_capacity: int = 128,
        blocker_timeout_seconds: float = 1.0,
        sleeper: FaultSleeper | None = None,
        fixture_mutator: FixtureMutator | None = None,
        clock: FaultClock | None = None,
    ) -> None:
        if not isinstance(plan, FaultPlan):
            raise TypeError("plan must be FaultPlan")
        self.recorder = FaultInjectionRecorder(capacity=recorder_capacity)
        self.sleeper = sleeper or AsyncioFaultSleeper()
        self.blockers = {
            rule.rule_id: FaultBlocker(timeout_seconds=blocker_timeout_seconds)
            for rule in plan.rules
            if rule.action is FaultAction.BLOCK_UNTIL_RELEASED
        }
        self.controller = FaultInjectionController.for_test(
            plan,
            recorder=self.recorder,
            sleeper=self.sleeper,
            blockers=self.blockers,
            fixture_mutator=fixture_mutator,
            clock=clock,
        )
        self._closed = False
        self._close_lock = asyncio.Lock()

    def blocker(self, rule_id: str) -> FaultBlocker:
        try:
            return self.blockers[rule_id]
        except KeyError as exc:
            raise KeyError("unknown blocking fault rule") from exc

    async def __aenter__(self) -> "FaultInjectionScope":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self.controller.close()
            for blocker in self.blockers.values():
                blocker.close()
            await self.sleeper.aclose()
            self.recorder.close()

    def __repr__(self) -> str:
        return (
            "FaultInjectionScope("
            f"closed={self._closed}, "
            f"blocker_count={len(self.blockers)}, "
            f"controller={self.controller!r}, "
            f"recorder={self.recorder!r}"
            ")"
        )


__all__ = [
    "AsyncioFaultSleeper",
    "ControllableFaultSleeper",
    "FaultBlocker",
    "FaultClock",
    "FaultControllerSnapshot",
    "FaultInjectionController",
    "FaultInjectionScope",
    "FaultRuleCounterSnapshot",
    "FaultSleeper",
    "FixtureMutator",
    "SystemFaultClock",
]
