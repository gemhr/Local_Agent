import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from core.runtime import (
    AsyncioFaultSleeper,
    ControllableFaultSleeper,
    FaultAction,
    FaultInjectionController,
    FaultInjectionRecorder,
    FaultInjectionScope,
    FaultMatchContext,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InjectedFaultCode,
    InjectedFaultError,
    NO_FAULT_DECISION,
    RecorderOverflowPolicy,
)

NOW = datetime(2026, 7, 31, tzinfo=UTC)


def rule(
    rule_id: str = "rule-a",
    *,
    action: FaultAction = FaultAction.RAISE_TYPED_ERROR,
    max_hits: int = 1,
) -> FaultRule:
    return FaultRule(
        rule_id=rule_id,
        fault_point=FaultPoint.EXECUTOR_BEFORE_SUBMIT,
        action=action,
        trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.COMPONENT_SCOPE,
        max_hits=max_hits,
        component="executor",
        safe_fault_code=(
            InjectedFaultCode.INJECTED_TRANSIENT_FAILURE
            if action is FaultAction.RAISE_TYPED_ERROR
            else None
        ),
    )


def plan(*rules: FaultRule) -> FaultPlan:
    return FaultPlan("concurrency-plan", tuple(rules), created_at=NOW)


def context() -> FaultMatchContext:
    return FaultMatchContext(
        fault_point=FaultPoint.EXECUTOR_BEFORE_SUBMIT,
        component="executor",
    )


@pytest.mark.asyncio
async def test_two_tasks_racing_max_hits_one_yield_exactly_one_match() -> None:
    controller = FaultInjectionController.for_test(plan(rule()))
    barrier = asyncio.Barrier(2)

    async def contender():
        await barrier.wait()
        return controller.evaluate(context())

    decisions = await asyncio.gather(contender(), contender())
    assert sorted(item.matched for item in decisions) == [False, True]
    matched = next(item for item in decisions if item.matched)
    assert matched.match_ordinal in {1, 2}
    assert matched.hit_ordinal == 1
    snapshot = controller.snapshot().counters[0]
    assert snapshot.match_count == 2
    assert snapshot.hit_count == 1


def test_threaded_match_and_hit_ordinals_are_monotonic() -> None:
    controller = FaultInjectionController.for_test(plan(rule(max_hits=8)))
    barrier = threading.Barrier(8)

    def contender():
        barrier.wait()
        return controller.evaluate(context())

    with ThreadPoolExecutor(max_workers=8) as executor:
        decisions = list(executor.map(lambda _: contender(), range(8)))
    assert sorted(item.match_ordinal for item in decisions) == list(range(1, 9))
    assert sorted(item.hit_ordinal for item in decisions) == list(range(1, 9))


def test_controllers_have_fully_isolated_counters() -> None:
    first = FaultInjectionController.for_test(plan(rule()))
    second = FaultInjectionController.for_test(plan(rule()))
    assert first.evaluate(context()).matched is True
    assert first.evaluate(context()) is NO_FAULT_DECISION
    assert second.evaluate(context()).matched is True
    assert first.snapshot().counters[0].hit_count == 1
    assert second.snapshot().counters[0].hit_count == 1


def test_close_and_evaluate_race_is_atomic_and_close_wins_future_calls() -> None:
    controller = FaultInjectionController.for_test(plan(rule()))
    barrier = threading.Barrier(2)

    def evaluate():
        barrier.wait()
        return controller.evaluate(context())

    def close():
        barrier.wait()
        controller.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        evaluated = executor.submit(evaluate)
        closed = executor.submit(close)
        decision = evaluated.result()
        closed.result()
    assert decision.matched in {True, False}
    assert controller.snapshot().counters[0].hit_count <= 1
    assert controller.evaluate(context()) is NO_FAULT_DECISION


@pytest.mark.asyncio
async def test_scope_exit_releases_blocker_and_closes_all_owners() -> None:
    fault_rule = rule(action=FaultAction.BLOCK_UNTIL_RELEASED)
    scope = FaultInjectionScope(plan(fault_rule))
    task = asyncio.create_task(scope.controller.execute_if_matched(context()))
    await scope.blocker(fault_rule.rule_id).entered.wait()
    await scope.aclose()
    decision = await asyncio.wait_for(task, timeout=0.2)
    assert decision.matched is True
    assert scope.blocker(fault_rule.rule_id).release.is_set()
    assert scope.controller.snapshot().closed is True
    assert scope.recorder.snapshot().closed is True
    await scope.aclose()


def test_blocker_timeout_is_strictly_finite() -> None:
    from core.runtime import FaultBlocker

    for value in (True, 0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="positive number"):
            FaultBlocker(timeout_seconds=value)
    assert FaultBlocker(timeout_seconds=0.25).timeout == 0.25


@pytest.mark.asyncio
async def test_blocker_timeout_raises_only_fixed_timeout_code() -> None:
    from core.runtime import FaultBlocker

    blocker = FaultBlocker(timeout_seconds=0.001)
    with pytest.raises(InjectedFaultError) as exc:
        await blocker.wait()
    assert exc.value.code is InjectedFaultCode.INJECTED_TIMEOUT


@pytest.mark.asyncio
async def test_default_sleeper_close_cancels_owned_wait_without_wall_clock_delay() -> (
    None
):
    sleeper = AsyncioFaultSleeper()
    task = asyncio.create_task(sleeper.sleep(60))
    await asyncio.sleep(0)
    await sleeper.aclose()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_blocker_wait_is_cancellable_without_leaking_waiter() -> None:
    fault_rule = rule(action=FaultAction.BLOCK_UNTIL_RELEASED)
    async with FaultInjectionScope(plan(fault_rule)) as scope:
        task = asyncio.create_task(scope.controller.execute_if_matched(context()))
        await scope.blocker(fault_rule.rule_id).entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert scope.blocker(fault_rule.rule_id).release.is_set()


@pytest.mark.asyncio
async def test_fake_sleeper_wait_is_cancellable_and_scope_close_releases_it() -> None:
    sleeper = ControllableFaultSleeper()
    delay_rule = FaultRule(
        rule_id="delay-rule",
        fault_point=FaultPoint.EXECUTOR_BEFORE_SUBMIT,
        action=FaultAction.DELAY,
        trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.COMPONENT_SCOPE,
        max_hits=2,
        component="executor",
        delay_seconds=5,
    )
    scope = FaultInjectionScope(plan(delay_rule), sleeper=sleeper)
    cancelled = asyncio.create_task(scope.controller.execute_if_matched(context()))
    await sleeper.entered.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    waiting = asyncio.create_task(scope.controller.execute_if_matched(context()))
    await asyncio.sleep(0)
    await scope.aclose()
    decision = await asyncio.wait_for(waiting, timeout=0.2)
    assert decision.matched is True
    assert sleeper.release.is_set()


def test_recorder_capacity_drop_oldest_and_reject_new_are_deterministic() -> None:
    decisions = []
    controller = FaultInjectionController.for_test(plan(rule(max_hits=3)))
    for _ in range(3):
        decisions.append(controller.evaluate(context()))

    drop = FaultInjectionRecorder(capacity=2)
    reject = FaultInjectionRecorder(
        capacity=2,
        overflow_policy=RecorderOverflowPolicy.REJECT_NEW,
    )
    for decision in decisions:
        assert decision.matched
        drop.record(plan_id="plan", component="executor", decision=decision)
        reject.record(plan_id="plan", component="executor", decision=decision)

    dropped = drop.snapshot()
    rejected = reject.snapshot()
    assert [item.hit_ordinal for item in dropped.records] == [2, 3]
    assert dropped.dropped_count == 1
    assert [item.hit_ordinal for item in rejected.records] == [1, 2]
    assert rejected.rejected_count == 1
    drop.close()
    assert (
        drop.record(
            plan_id="plan",
            component="executor",
            decision=decisions[0],
        )
        is False
    )
