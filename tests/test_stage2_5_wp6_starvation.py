"""WP6 planning-executor starvation capacity verification (P2)."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from core.runtime import (
    BlockingTaskKind,
    CancellationReason,
    CoordinatedRuntimeFactory,
    InMemoryMetricsRecorder,
    RecorderInfrastructureMetricsHook,
    RunStatus,
    RuntimeEventType,
    StopReason,
    process_blocking_executor,
)
from tests._stage2_5_wp6_fixtures import GatedPlanningRouter
from tests._wp3_fixtures import make_wp3_services


PLANNING_JSON = (
    '{"schema_version":1,"decision":"DIRECT_ANSWER",'
    '"agent_id":"core_router","reason_code":"MODEL_DIRECT"}'
)


def _fill_executor() -> threading.Event:
    """占满全部 worker + pending 上限；返回释放 gate。"""
    filler_gate = threading.Event()
    count = (
        process_blocking_executor.max_workers
        + process_blocking_executor.max_pending_tasks
    )
    for index in range(count):
        process_blocking_executor.submit(
            filler_gate.wait,
            kind=BlockingTaskKind.RUNTIME_STEP,
            run_id="starvation-fixture",
            operation_id=f"filler-{index}",
            cancellation_check=lambda: None,
            remaining_seconds=lambda: 30.0,
        )
    return filler_gate


async def _wait_filled(timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while (
        process_blocking_executor.snapshot().active_count
        < process_blocking_executor.max_workers
        or process_blocking_executor.snapshot().pending_count
        < process_blocking_executor.max_pending_tasks
    ):
        assert time.monotonic() < deadline, "executor 未在时限内占满"
        await asyncio.sleep(0.01)


async def _run_scope(services, router, **kwargs):
    return await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("core_router", "starvation capacity run", **kwargs)


@pytest.mark.asyncio
async def test_planning_starvation_queues_bounded_then_recovers() -> None:
    """占满 worker -> 新 Run planning 排队等待（pending 有界）-> 指标可观测
    -> 释放后系统恢复：无死锁、无重复执行、无错误分类。"""
    services = make_wp3_services()
    recorder = InMemoryMetricsRecorder()
    hook = RecorderInfrastructureMetricsHook(recorder)
    previous_hook = process_blocking_executor._metrics_hook
    process_blocking_executor.set_metrics_hook(hook)
    filler_gate = threading.Event()
    router = GatedPlanningRouter(PLANNING_JSON)
    try:
        filler_gate = _fill_executor()
        await _wait_filled()
        scope = await _run_scope(services, router)
        task = asyncio.ensure_future(scope.execute())

        # planning 在准入队列等待：worker 全忙、pending 已达上限，
        # planner model 未进入（饥饿可观测但队列有界）。
        await asyncio.sleep(0.1)
        snapshot = process_blocking_executor.snapshot()
        assert snapshot.active_count == process_blocking_executor.max_workers
        assert snapshot.pending_count == snapshot.max_pending_tasks
        assert router.entered.is_set() is False

        filler_gate.set()
        router.release.set()
        result = await asyncio.wait_for(task, timeout=20)

        assert result.status is RunStatus.SUCCEEDED
        assert router.planning_calls == 1
        types = [
            item.event_type
            for item in services.event_journal.read_after(
                scope.run_id, 0, 1000
            )
        ]
        assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
        await scope.close()
    finally:
        filler_gate.set()
        router.release.set()
        process_blocking_executor.set_metrics_hook(previous_hook)
        assert process_blocking_executor.wait_until_idle(10.0)

    # 等待时长指标可观测（P2 保留依据）。
    waits = recorder.snapshot().histogram(
        "runtime_blocking_executor_wait_seconds"
    )
    assert waits, "planning 排队等待未被指标记录"


@pytest.mark.asyncio
async def test_starvation_cancel_converges_from_same_loop() -> None:
    """WP6 修复验证：占满 worker 且无 deadline 时，同一事件循环上的取消
    仍能传播（准入等待不再占住 loop），Run 收敛为 CANCELLED。"""
    services = make_wp3_services()
    previous_hook = process_blocking_executor._metrics_hook
    process_blocking_executor.set_metrics_hook(
        RecorderInfrastructureMetricsHook(InMemoryMetricsRecorder())
    )
    filler_gate = threading.Event()
    router = GatedPlanningRouter(PLANNING_JSON)
    try:
        filler_gate = _fill_executor()
        await _wait_filled()
        scope = await _run_scope(services, router)
        task = asyncio.ensure_future(scope.execute())
        await asyncio.sleep(0.1)

        assert scope.request_cancel(CancellationReason.REQUEST_CANCELLED)
        result = await asyncio.wait_for(task, timeout=20)

        assert result.status is RunStatus.CANCELLED
        assert result.stop_reason is StopReason.USER_CANCELLED
        assert result.error_code == "REQUEST_CANCELLED"
        types = [
            item.event_type
            for item in services.event_journal.read_after(
                scope.run_id, 0, 1000
            )
        ]
        assert RuntimeEventType.PLAN_CREATED not in types
        assert RuntimeEventType.OUTPUT_DELTA not in types
        assert types.count(RuntimeEventType.RUN_COMPLETED) == 1
        await scope.close()
    finally:
        filler_gate.set()
        router.release.set()
        process_blocking_executor.set_metrics_hook(previous_hook)
        assert process_blocking_executor.wait_until_idle(10.0)


@pytest.mark.asyncio
async def test_starvation_deadline_is_classified_correctly() -> None:
    """占满 worker + Run deadline：planning 准入超时归类为 DEADLINE_EXCEEDED
    （不是 AGENT/Planning 错误），无 PLAN_CREATED、无 STEP。"""
    services = make_wp3_services()
    previous_hook = process_blocking_executor._metrics_hook
    process_blocking_executor.set_metrics_hook(
        RecorderInfrastructureMetricsHook(InMemoryMetricsRecorder())
    )
    filler_gate = threading.Event()
    router = GatedPlanningRouter(PLANNING_JSON)
    try:
        filler_gate = _fill_executor()
        await _wait_filled()
        scope = await _run_scope(
            services, router, timeout_seconds=0.3
        )

        result = await scope.execute()

        assert result.status is RunStatus.FAILED
        assert result.stop_reason is StopReason.DEADLINE_EXCEEDED
        assert result.error_code == "DEADLINE_EXCEEDED"
        types = [
            item.event_type
            for item in services.event_journal.read_after(
                scope.run_id, 0, 1000
            )
        ]
        assert RuntimeEventType.PLAN_CREATED not in types
        assert RuntimeEventType.STEP_STARTED not in types
        assert types.count(RuntimeEventType.RUN_COMPLETED) == 1
        await scope.close()
    finally:
        filler_gate.set()
        router.release.set()
        process_blocking_executor.set_metrics_hook(previous_hook)
        assert process_blocking_executor.wait_until_idle(10.0)
