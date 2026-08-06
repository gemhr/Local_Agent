"""WP6 planning failure matrix: injected seams + lifecycle cancellation."""

from __future__ import annotations

import asyncio

import pytest

from core.runtime import (
    CancellationReason,
    CoordinatedRuntimeFactory,
    FaultPoint,
    RunStatus,
    RuntimeEventType,
    StopReason,
)
from core.runtime.checkpoint_contract import CheckpointKind
from tests._stage2_5_wp6_fixtures import GatedPlanningRouter, wp6_controller
from tests._wp3_fixtures import Wp3RecordingRouter, make_wp3_services


def _records(services, run_id: str):
    return services.event_journal.read_after(run_id, 0, 1000)


def _types(services, run_id: str) -> list[RuntimeEventType]:
    return [item.event_type for item in _records(services, run_id)]


async def _wait_for_model_entry(router, task) -> None:
    """Yield to the event loop while the planner model enters its gate."""
    deadline = asyncio.get_event_loop().time() + 5.0
    while not router.entered.is_set():
        if task.done():
            return
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("planner model 未进入阻塞点")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_planning_resolve_injected_fault_is_planning_failed() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter()
    controller = wp6_controller(
        FaultPoint.PLANNING_BEFORE_RESOLVE,
        component="run_coordinator",
        operation_kind="PLANNING_RESOLVE",
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        "planning resolve injected fault",
        fault_controller=controller,
    )

    result = await scope.execute()

    assert result.status is RunStatus.FAILED
    assert result.stop_reason is StopReason.PLANNING_FAILED
    assert result.error_code == "PLANNING_MODEL_FAILED"
    types = _types(services, scope.run_id)
    assert RuntimeEventType.PLAN_CREATED not in types
    assert RuntimeEventType.STEP_STARTED not in types
    assert RuntimeEventType.OUTPUT_DELTA not in types
    assert types.count(RuntimeEventType.RUN_COMPLETED) == 1
    assert router.memory_manager.count_messages("core_router") == 0
    assert result.succeeded_step_ids == ()
    await scope.close()


@pytest.mark.asyncio
async def test_plan_created_publication_failure_has_no_steps() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter()
    controller = wp6_controller(
        FaultPoint.PLANNING_BEFORE_PLAN_CREATED,
        component="run_coordinator",
        operation_kind="PLAN_CREATED_EVENT",
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        "plan created publication fault",
        fault_controller=controller,
    )

    result = await scope.execute()

    assert result.status is RunStatus.FAILED
    # Plan 已冻结但 PLAN_CREATED 事实发布失败：基础设施错误，不进入执行。
    assert result.error_code == "COORDINATOR_INFRASTRUCTURE_ERROR"
    types = _types(services, scope.run_id)
    assert RuntimeEventType.PLAN_CREATED not in types
    assert RuntimeEventType.STEP_STARTED not in types
    assert RuntimeEventType.OUTPUT_DELTA not in types
    assert types.count(RuntimeEventType.RUN_COMPLETED) == 1
    assert router.memory_manager.count_messages("core_router") == 0
    await scope.close()


@pytest.mark.asyncio
async def test_post_plan_checkpoint_failure_fails_closed() -> None:
    services = make_wp3_services(snapshot_enabled=True)
    router = Wp3RecordingRouter()
    controller = wp6_controller(
        FaultPoint.SNAPSHOT_BEFORE_SAVE,
        component="checkpoint_coordinator",
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        "post plan checkpoint fault",
        fault_controller=controller,
    )

    result = await scope.execute()

    assert result.status is RunStatus.FAILED
    assert result.error_code == "POST_PLAN_PRE_EXECUTION_CHECKPOINT_FAILED"
    types = _types(services, scope.run_id)
    assert RuntimeEventType.STEP_STARTED not in types
    assert RuntimeEventType.OUTPUT_DELTA not in types
    assert types.count(RuntimeEventType.RUN_COMPLETED) == 1
    assert router.memory_manager.count_messages("core_router") == 0
    await scope.close()


@pytest.mark.asyncio
async def test_plan_created_journal_append_failure_has_no_steps() -> None:
    """4.1 必选：PLAN_CREATED journal append 失败（EVENT_BEFORE_JOURNAL_APPEND）。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter()
    controller = wp6_controller(
        FaultPoint.EVENT_BEFORE_JOURNAL_APPEND,
        component="event_channel",
        event_type=RuntimeEventType.PLAN_CREATED,
        operation_kind="JOURNAL_APPEND",
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        "plan created journal fault",
        fault_controller=controller,
    )

    result = await scope.execute()

    assert result.status is RunStatus.FAILED
    types = _types(services, scope.run_id)
    assert RuntimeEventType.PLAN_CREATED not in types
    assert RuntimeEventType.STEP_STARTED not in types
    assert RuntimeEventType.OUTPUT_DELTA not in types
    assert types.count(RuntimeEventType.RUN_COMPLETED) == 1
    assert router.memory_manager.count_messages("core_router") == 0
    await scope.close()


@pytest.mark.asyncio
async def test_client_disconnect_during_planning_is_cancelled() -> None:
    services = make_wp3_services()
    router = GatedPlanningRouter(
        '{"schema_version":1,"decision":"DIRECT_ANSWER",'
        '"agent_id":"core_router","reason_code":"MODEL_DIRECT"}',
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("core_router", "client disconnect during planning")

    task = __import__("asyncio").ensure_future(scope.execute())
    await _wait_for_model_entry(router, task)
    assert scope.request_cancel(CancellationReason.CLIENT_DISCONNECTED)
    router.release.set()
    result = await task

    assert result.status is RunStatus.CANCELLED
    assert result.stop_reason is StopReason.CLIENT_DISCONNECTED
    assert result.error_code == "CLIENT_DISCONNECTED"
    types = _types(services, scope.run_id)
    assert RuntimeEventType.PLAN_CREATED not in types
    assert RuntimeEventType.STEP_STARTED not in types
    assert types.count(RuntimeEventType.RUN_COMPLETED) == 1
    await scope.close()


@pytest.mark.asyncio
async def test_shutdown_during_planning_is_cancelled() -> None:
    services = make_wp3_services()
    router = GatedPlanningRouter(
        '{"schema_version":1,"decision":"DIRECT_ANSWER",'
        '"agent_id":"core_router","reason_code":"MODEL_DIRECT"}',
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("core_router", "shutdown during planning")

    task = __import__("asyncio").ensure_future(scope.execute())
    await _wait_for_model_entry(router, task)
    assert scope.request_cancel(CancellationReason.SERVER_SHUTDOWN)
    router.release.set()
    result = await task

    assert result.status is RunStatus.CANCELLED
    assert result.stop_reason is StopReason.SYSTEM_SHUTDOWN
    assert result.error_code == "SERVER_SHUTDOWN"
    types = _types(services, scope.run_id)
    assert RuntimeEventType.PLAN_CREATED not in types
    assert RuntimeEventType.STEP_STARTED not in types
    assert types.count(RuntimeEventType.RUN_COMPLETED) == 1
    await scope.close()
