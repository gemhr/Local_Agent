"""WP6 parallel/concurrency verification: serial, overlap, cancel, late worker."""

from __future__ import annotations

import asyncio
import threading

import pytest

from core.runtime import (
    CancellationReason,
    CoordinatedRuntimeFactory,
    RunBudget,
    RunStatus,
    RuntimeEventType,
)
from tests._wp3_fixtures import (
    Wp3RecordingRouter,
    make_wp3_services,
    shape3_planning_json,
)


def _types(services, run_id: str) -> list[RuntimeEventType]:
    return [
        item.event_type
        for item in services.event_journal.read_after(run_id, 0, 1000)
    ]


@pytest.mark.asyncio
async def test_max_concurrency_one_is_serial() -> None:
    """max_concurrency=1：specialist 按策略串行，真实活跃数不超过 1。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter(shape3_planning_json())
    scope = await CoordinatedRuntimeFactory(
        router, services, max_concurrency=1
    ).create_run_scope("core_router", "serial policy")

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert len(router.calls_for("code_expert")) == 1
    assert len(router.calls_for("knowledge_expert")) == 1
    assert len(router.calls_for("synthesis_agent")) == 1
    assert router.max_active <= 1
    await scope.close()


@pytest.mark.asyncio
async def test_budget_max_concurrency_limits_overlap() -> None:
    """budget.max_concurrency=1：即使 policy 允许 2，实际活跃数仍为 1。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter(shape3_planning_json())
    scope = await CoordinatedRuntimeFactory(
        router, services, max_concurrency=2
    ).create_run_scope(
        "core_router",
        "budget concurrency",
        budget=RunBudget(max_concurrency=1),
    )

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert router.max_active <= 1
    await scope.close()


class GatedParallelRouter(Wp3RecordingRouter):
    """两个 specialist 都进入 gate 后等待统一释放。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.entered: dict[str, threading.Event] = {}
        self.release = threading.Event()

    def complete_single_agent(self, agent_id, query, **kwargs):
        if agent_id in {"code_expert", "knowledge_expert"}:
            event = self.entered.setdefault(agent_id, threading.Event())
            event.set()
            self.release.wait(timeout=15)
        return super().complete_single_agent(agent_id, query, **kwargs)


@pytest.mark.asyncio
async def test_cancellation_propagates_to_running_steps() -> None:
    """取消同时传播到多个 running Step：两个 specialist 均进入后取消。"""
    services = make_wp3_services()
    router = GatedParallelRouter(shape3_planning_json())
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("core_router", "cancel both running steps")

    task = asyncio.create_task(scope.execute())
    code_entered = router.entered.setdefault("code_expert", threading.Event())
    knowledge_entered = router.entered.setdefault(
        "knowledge_expert", threading.Event()
    )
    assert await asyncio.to_thread(code_entered.wait, 10)
    assert await asyncio.to_thread(knowledge_entered.wait, 10)
    assert scope.request_cancel(CancellationReason.REQUEST_CANCELLED)
    router.release.set()
    result = await task

    assert result.status is RunStatus.CANCELLED
    assert len(router.calls_for("synthesis_agent")) == 0
    types = _types(services, scope.run_id)
    assert RuntimeEventType.OUTPUT_DELTA not in types
    completed = [
        item
        for item in services.event_journal.read_after(scope.run_id, 0, 1000)
        if item.event_type is RuntimeEventType.STEP_COMPLETED
    ]
    # 取消是协作式：两个 specialist 都已进入运行，Run 终态前可能已提交
    # SUCCEEDED；但绝不留下 RUNNING 泄漏、不调用 synthesis、不发布正文。
    specialist_completed = [
        item
        for item in completed
        if item.step_id in {"task-code", "task-knowledge"}
    ]
    assert specialist_completed
    assert all(
        item.safe_payload.get("status") in {"SUCCEEDED", "CANCELLED"}
        for item in specialist_completed
    )
    assert router.memory_manager.count_messages("core_router") == 0
    await scope.close()


@pytest.mark.asyncio
async def test_late_worker_after_run_terminal_cannot_commit() -> None:
    """迟到 worker：Run 终态后返回的 specialist 不得提交 Store 或输出。"""
    services = make_wp3_services()
    router = GatedParallelRouter(shape3_planning_json())
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("core_router", "late worker")

    task = asyncio.create_task(scope.execute())
    code_entered = router.entered.setdefault("code_expert", threading.Event())
    assert await asyncio.to_thread(code_entered.wait, 10)
    assert scope.request_cancel(CancellationReason.REQUEST_CANCELLED)
    router.release.set()
    result = await task

    assert result.status is RunStatus.CANCELLED
    # 终态后 worker 返回，其结果不得产生第二次正文或 Memory。
    await asyncio.sleep(0.2)
    types = _types(services, scope.run_id)
    assert types.count(RuntimeEventType.OUTPUT_DELTA) == 0
    assert router.memory_manager.count_messages("core_router") == 0
    store = scope.coordinator.step_result_store
    assert store is not None and store.is_cleared is True
    await scope.close()
