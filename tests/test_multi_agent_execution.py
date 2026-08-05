"""Real multi-agent E2E through Scheduler -> Executor -> Driver -> Store ->
Synthesis; external Agent implementation is faked only."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from core.runtime import (
    CoordinatedRuntimeFactory,
    RunStatus,
    StopReason,
)
from core.runtime.cancellation import CancellationReason
from core.runtime.events import RuntimeEventType
from tests._wp3_fixtures import (
    Wp3RecordingRouter,
    make_wp3_services,
    shape2_planning_json,
    shape3_planning_json,
)


def event_records(services, run_id: str):
    return services.event_journal.read_after(run_id, 0, 1000)


def event_types(services, run_id: str) -> list[RuntimeEventType]:
    return [record.event_type for record in event_records(services, run_id)]


def completed_steps(services, run_id: str) -> list[tuple[str, str, str]]:
    return [
        (record.step_id, record.safe_payload["status"], record.safe_payload["safe_error_code"])
        for record in event_records(services, run_id)
        if record.event_type is RuntimeEventType.STEP_COMPLETED
    ]


async def run_scope(router, services, *, query="coordinate two reviews", **factory_kwargs):
    timeout_seconds = factory_kwargs.pop("timeout_seconds", None)
    scope = await CoordinatedRuntimeFactory(
        router, services, **factory_kwargs
    ).create_run_scope(
        "core_router",
        query,
        timeout_seconds=timeout_seconds,
    )
    result = await scope.execute()
    return scope, result


@pytest.mark.asyncio
async def test_shape2_code_then_synthesis_executes_once_each() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(shape2_planning_json())
    scope, result = await run_scope(router, services)

    assert result.status is RunStatus.FAILED
    assert result.stop_reason is StopReason.UNHANDLED_ERROR
    assert result.error_code == "FINAL_OUTPUT_PIPELINE_NOT_READY"
    assert len(router.calls_for("code_expert")) == 1
    assert len(router.calls_for("synthesis_agent")) == 1
    assert len(router.calls_for("data_analyst")) == 0
    assert all(flag is False for flag in router.persist_flags())

    prompt = router.prompts_for("synthesis_agent")[0]
    assert "result-code_expert" in prompt
    assert "data_analyst" not in prompt
    assert "knowledge_expert" not in prompt

    steps = completed_steps(services, scope.run_id)
    assert ("task-code", "SUCCEEDED", None) in steps
    assert ("synthesis", "SUCCEEDED", None) in steps
    assert RuntimeEventType.OUTPUT_DELTA not in event_types(services, scope.run_id)
    assert scope.coordinator._final_result_ready() is False  # store cleared
    store = scope.coordinator.step_result_store
    assert store is not None
    assert store.is_cleared is True
    assert store.entry_count() == 0
    await scope.close()


@pytest.mark.asyncio
async def test_shape3_specialists_overlap_and_synthesis_waits() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        shape3_planning_json(),
        barrier_agents=("code_expert", "knowledge_expert"),
        barrier=threading.Barrier(2),
    )
    scope, result = await run_scope(router, services)

    assert result.error_code == "FINAL_OUTPUT_PIPELINE_NOT_READY"
    assert len(router.calls_for("code_expert")) == 1
    assert len(router.calls_for("knowledge_expert")) == 1
    assert len(router.calls_for("synthesis_agent")) == 1

    # Real overlap, proven by events, not wall-clock totals:
    # 1) the two-party barrier released (both specialists were inside it at
    #    the same time) and 2) the shared active counter reached 2.
    assert router.max_active >= 2
    specialist_enters = [
        (position, agent)
        for position, (agent, phase) in enumerate(router.order)
        if phase == "enter"
        and agent in ("code_expert", "knowledge_expert")
    ]
    specialist_exits = [
        (position, agent)
        for position, (agent, phase) in enumerate(router.order)
        if phase == "exit"
        and agent in ("code_expert", "knowledge_expert")
    ]
    assert len(specialist_enters) == 2
    assert max(position for position, _ in specialist_enters) < min(
        position for position, _ in specialist_exits
    )
    # Synthesis only starts after every specialist result is READABLE.
    synthesis_enter = next(
        position
        for position, (agent, phase) in enumerate(router.order)
        if agent == "synthesis_agent" and phase == "enter"
    )
    assert synthesis_enter > max(position for position, _ in specialist_exits)

    steps = completed_steps(services, scope.run_id)
    assert ("task-code", "SUCCEEDED", None) in steps
    assert ("task-knowledge", "SUCCEEDED", None) in steps
    assert ("synthesis", "SUCCEEDED", None) in steps
    assert RuntimeEventType.OUTPUT_DELTA not in event_types(services, scope.run_id)
    store = scope.coordinator.step_result_store
    assert store is not None and store.is_cleared is True
    await scope.close()


@pytest.mark.asyncio
async def test_specialist_failure_blocks_synthesis_fail_closed() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        shape3_planning_json(),
        fail_agents=("knowledge_expert",),
    )
    scope, result = await run_scope(router, services)

    assert result.status is RunStatus.FAILED
    assert result.error_code == "REQUIRED_DEPENDENCY_FAILED"
    assert len(router.calls_for("synthesis_agent")) == 0
    assert len(router.calls_for("core_router")) == 0
    assert RuntimeEventType.OUTPUT_DELTA not in event_types(services, scope.run_id)
    store = scope.coordinator.step_result_store
    assert store is not None and store.is_cleared is True
    await scope.close()


@pytest.mark.asyncio
async def test_specialist_deadline_blocks_synthesis() -> None:
    services = make_wp3_services()

    class SlowRouter(Wp3RecordingRouter):
        def complete_single_agent(self, agent_id, query, **kwargs):
            if agent_id == "code_expert":
                time.sleep(2)
            return super().complete_single_agent(agent_id, query, **kwargs)

    router = SlowRouter(shape3_planning_json())
    scope, result = await run_scope(
        router,
        services,
        timeout_seconds=0.2,
    )
    assert result.stop_reason is StopReason.DEADLINE_EXCEEDED
    assert result.error_code == "DEADLINE_EXCEEDED"
    assert len(router.calls_for("synthesis_agent")) == 0
    assert RuntimeEventType.OUTPUT_DELTA not in event_types(services, scope.run_id)
    store = scope.coordinator.step_result_store
    assert store is not None and store.is_cleared is True
    await scope.close()


@pytest.mark.asyncio
async def test_user_cancellation_blocks_synthesis() -> None:
    services = make_wp3_services()
    started = threading.Event()
    release = threading.Event()

    class GatedRouter(Wp3RecordingRouter):
        def complete_single_agent(self, agent_id, query, **kwargs):
            if agent_id == "code_expert":
                started.set()
                release.wait(timeout=15)
            return super().complete_single_agent(agent_id, query, **kwargs)

    router = GatedRouter(shape3_planning_json())
    scope = await CoordinatedRuntimeFactory(router, services).create_run_scope(
        "core_router", "coordinate two reviews"
    )
    task = asyncio.create_task(scope.execute())
    assert await asyncio.to_thread(started.wait, 10)
    scope.request_cancel(CancellationReason.REQUEST_CANCELLED)
    release.set()
    result = await task

    assert result.status is RunStatus.CANCELLED
    assert len(router.calls_for("synthesis_agent")) == 0
    assert RuntimeEventType.OUTPUT_DELTA not in event_types(services, scope.run_id)
    store = scope.coordinator.step_result_store
    assert store is not None and store.is_cleared is True
    await scope.close()


@pytest.mark.asyncio
async def test_result_too_large_fails_prepare_closed() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(shape3_planning_json())
    scope, result = await run_scope(
        router,
        services,
        step_result_per_result_chars=5,
        step_result_run_total_chars=20,
    )
    assert result.status is RunStatus.FAILED
    assert result.error_code == "STEP_RESULT_PREPARE_FAILED"
    assert len(router.calls_for("synthesis_agent")) == 0
    assert RuntimeEventType.OUTPUT_DELTA not in event_types(services, scope.run_id)
    store = scope.coordinator.step_result_store
    assert store is not None and store.is_cleared is True
    await scope.close()


@pytest.mark.asyncio
async def test_synthesis_failure_has_no_fallback() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        shape3_planning_json(),
        fail_agents=("synthesis_agent",),
    )
    scope, result = await run_scope(router, services)

    assert result.status is RunStatus.FAILED
    assert result.error_code == "SYNTHESIS_FAILED"
    assert len(router.calls_for("core_router")) == 0
    assert RuntimeEventType.OUTPUT_DELTA not in event_types(services, scope.run_id)
    store = scope.coordinator.step_result_store
    assert store is not None and store.is_cleared is True
    await scope.close()
