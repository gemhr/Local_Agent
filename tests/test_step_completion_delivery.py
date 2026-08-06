"""WP4 final Step execution/delivery layering and failure classification."""

from __future__ import annotations

import asyncio

import pytest

from core.runtime import (
    CoordinatedRuntimeFactory,
    DANGEROUS_FAULT_POINTS,
    FaultAction,
    FaultInjectionController,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InjectedFaultCode,
    RunStatus,
    RuntimeEventType,
    StepStatus,
)
from datetime import UTC, datetime
from tests._event_fault_fixtures import event_controller
from tests._wp3_fixtures import (
    Wp3RecordingRouter,
    make_wp3_services,
    shape2_planning_json,
    shape3_planning_json,
)


def records(services, run_id: str):
    return services.event_journal.read_after(run_id, 0, 1000)


def step_completed(services, run_id: str, step_id: str):
    return [
        item
        for item in records(services, run_id)
        if item.event_type is RuntimeEventType.STEP_COMPLETED
        and item.step_id == step_id
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault_point", "expected_code"),
    [
        (FaultPoint.EVENT_BEFORE_JOURNAL_APPEND, "FINAL_OUTPUT_DELIVERY_FAILED"),
        (FaultPoint.EVENT_AFTER_JOURNAL_APPEND, "FINAL_OUTPUT_DELIVERY_UNKNOWN"),
        (FaultPoint.EVENT_BEFORE_CHANNEL_ENQUEUE, "FINAL_OUTPUT_DELIVERY_UNKNOWN"),
    ],
)
async def test_final_step_stays_succeeded_on_delivery_failure(
    fault_point, expected_code
) -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        shape2_planning_json(),
        output_for={"synthesis_agent": "SECRET_FINAL_CANDIDATE"},
    )
    controller = event_controller(
        fault_point,
        event_type=RuntimeEventType.OUTPUT_DELTA,
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        "coordinate one review",
        fault_controller=controller,
    )
    final_step_state: dict[str, object] = {}
    execute_task = asyncio.create_task(scope.execute())
    for _ in range(200):
        store = scope.coordinator.step_result_store
        if store is not None:
            break
        await asyncio.sleep(0.005)
    assert store is not None
    original_seal = store.seal

    def capture_seal():
        step = scope.agent_state.steps.get("synthesis")
        final_step_state["status"] = step.status if step is not None else None
        final_step_state["readable"] = store.has_readable("synthesis")
        original_seal()

    store.seal = capture_seal
    result = await execute_task

    assert result.status is RunStatus.FAILED
    assert result.error_code == expected_code
    assert result.stop_reason.value == "UNHANDLED_ERROR"
    assert result.succeeded_step_ids == ("task-code", "synthesis")
    # Final Step remains SUCCEEDED on delivery failure (never AGENT_STEP_FAILED).
    assert final_step_state["status"] is StepStatus.SUCCEEDED
    assert final_step_state["readable"] is True
    completed = step_completed(services, scope.run_id, "synthesis")
    assert len(completed) == 1
    assert completed[0].safe_payload["status"] == "SUCCEEDED"
    output_events = [
        item
        for item in records(services, scope.run_id)
        if item.event_type is RuntimeEventType.OUTPUT_DELTA
    ]
    assert len(output_events) == (0 if fault_point is FaultPoint.EVENT_BEFORE_JOURNAL_APPEND else 1)
    assert "AGENT_STEP_FAILED" not in repr(records(services, scope.run_id))
    assert "SECRET_FINAL_CANDIDATE" not in repr(records(services, scope.run_id))
    await scope.close()


@pytest.mark.asyncio
async def test_completion_event_failure_keeps_delivery_status_and_fails_run() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(shape2_planning_json())
    rule = FaultRule(
        rule_id="synthesis-completed-fault",
        fault_point=FaultPoint.EVENT_BEFORE_CHANNEL_ENQUEUE,
        action=FaultAction.RAISE_TYPED_ERROR,
        trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.RUN_SCOPE,
        max_hits=1,
        component="event_channel",
        step_id="synthesis",
        event_type=RuntimeEventType.STEP_COMPLETED.value,
        safe_fault_code=InjectedFaultCode.INJECTED_JOURNAL_FAILURE,
        dangerous_window=FaultPoint.EVENT_BEFORE_CHANNEL_ENQUEUE
        in DANGEROUS_FAULT_POINTS,
    )
    controller = FaultInjectionController(
        FaultPlan("synthesis-completed", (rule,), created_at=datetime.now(UTC)),
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        "coordinate one review",
        fault_controller=controller,
    )

    result = await scope.execute()

    assert result.status is RunStatus.FAILED
    assert result.error_code == "STEP_COMPLETION_EVENT_FAILED"
    output_events = [
        item
        for item in records(services, scope.run_id)
        if item.event_type is RuntimeEventType.OUTPUT_DELTA
    ]
    # The OUTPUT is never retried; the safe report keeps the delivery status.
    assert len(output_events) == 1
    await scope.close()


@pytest.mark.asyncio
async def test_shape3_delivery_failure_is_not_agent_failure() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(shape3_planning_json())
    controller = event_controller(
        FaultPoint.EVENT_BEFORE_JOURNAL_APPEND,
        event_type=RuntimeEventType.OUTPUT_DELTA,
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        "coordinate two reviews",
        fault_controller=controller,
    )

    result = await scope.execute()

    assert result.status is RunStatus.FAILED
    assert result.error_code == "FINAL_OUTPUT_DELIVERY_FAILED"
    assert result.failed_step_ids == ()
    assert result.succeeded_step_ids == (
        "task-code",
        "task-knowledge",
        "synthesis",
    )
    assert len(router.calls_for("synthesis_agent")) == 1
    assert len(router.calls_for("code_expert")) == 1
    assert len(router.calls_for("knowledge_expert")) == 1
    completed = step_completed(services, scope.run_id, "synthesis")
    assert completed[0].safe_payload["status"] == "SUCCEEDED"
    await scope.close()
