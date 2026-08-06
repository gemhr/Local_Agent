"""WP6 delivery failure matrix: at-most-once output, completion/terminal events."""

from __future__ import annotations

import pytest

from core.runtime import (
    CoordinatedRuntimeFactory,
    FaultPoint,
    OutputGateState,
    RunCoordinatorError,
    RunStatus,
    RuntimeEventType,
)
from tests._stage2_5_wp6_fixtures import wp6_controller
from tests._wp3_fixtures import (
    Wp3RecordingRouter,
    make_wp3_services,
    shape2_planning_json,
)


def _records(services, run_id: str):
    return services.event_journal.read_after(run_id, 0, 1000)


def _types(services, run_id: str) -> list[RuntimeEventType]:
    return [item.event_type for item in _records(services, run_id)]


@pytest.mark.asyncio
async def test_output_before_publish_failure_is_failed_not_unknown() -> None:
    """FP-OUT-01: journal append 前失败 -> FAILED，不产生任何正文事实。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        shape2_planning_json(),
        output_for={"synthesis_agent": "SECRET_FINAL_CANDIDATE"},
    )
    controller = wp6_controller(
        FaultPoint.OUTPUT_BEFORE_PUBLISH,
        component="output_gate",
        operation_kind="OUTPUT_DELTA",
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        "delivery before publish fault",
        fault_controller=controller,
    )

    result = await scope.execute()

    assert result.status is RunStatus.FAILED
    assert result.error_code == "FINAL_OUTPUT_DELIVERY_FAILED"
    # Final Step 仍 SUCCEEDED（交付失败不改写执行成功）。
    assert "synthesis" in result.succeeded_step_ids
    gate = scope.coordinator.output_gate
    assert gate is not None and gate.state is OutputGateState.FAILED
    types = _types(services, scope.run_id)
    assert RuntimeEventType.OUTPUT_DELTA not in types
    # failed 不写 Memory。
    assert router.memory_manager.count_messages("core_router") == 0
    journal_text = repr(_records(services, scope.run_id))
    assert "SECRET_FINAL_CANDIDATE" not in journal_text
    await scope.close()


@pytest.mark.asyncio
async def test_step_completed_after_delivery_failure_keeps_delivery() -> None:
    """FP-OUT-13: delivery 成功后 STEP_COMPLETED 发布失败 ->
    Run FAILED(STEP_COMPLETION_EVENT_FAILED)，正文 attempt 仍为 1 次。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        shape2_planning_json(),
        output_for={"synthesis_agent": "SECRET_FINAL_CANDIDATE"},
    )
    controller = wp6_controller(
        FaultPoint.EVENT_BEFORE_JOURNAL_APPEND,
        component="event_channel",
        operation_kind="JOURNAL_APPEND",
        event_type=RuntimeEventType.STEP_COMPLETED,
        step_id="synthesis",
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        "completion event after delivery fault",
        fault_controller=controller,
    )

    result = await scope.execute()

    assert result.status is RunStatus.FAILED
    assert result.error_code == "STEP_COMPLETION_EVENT_FAILED"
    # 正文只尝试一次（已 journaled），不重发。
    types = _types(services, scope.run_id)
    assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
    assert "SECRET_FINAL_CANDIDATE" not in repr(
        _records(services, scope.run_id)
    )
    await scope.close()


@pytest.mark.asyncio
async def test_terminal_publication_failure_after_delivery() -> None:
    """FP-OUT-14/FP-MEM-12: delivered + Memory 成功后 terminal 事件失败 ->
    RunCoordinator 以 RUNTIME_TERMINAL_PUBLICATION_FAILED 暴露；
    Memory 保留完整 exchange，不重发正文、不重写。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        shape2_planning_json(),
        output_for={"synthesis_agent": "SECRET_FINAL_CANDIDATE"},
    )
    controller = wp6_controller(
        FaultPoint.JOURNAL_BEFORE_TERMINAL_APPEND,
        component="event_channel",
        operation_kind="JOURNAL_APPEND",
        event_type=RuntimeEventType.RUN_COMPLETED,
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        "terminal event fault",
        fault_controller=controller,
    )

    with pytest.raises(RunCoordinatorError) as exc:
        await scope.execute()

    assert exc.value.error_code == "RUNTIME_TERMINAL_PUBLICATION_FAILED"
    types = _types(services, scope.run_id)
    assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
    assert RuntimeEventType.RUN_COMPLETED not in types
    # Memory exchange 已完整提交（delivered）。
    history = router.memory_manager.get_chat_history(
        "core_router", ascending=True
    )
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert "SECRET_FINAL_CANDIDATE" not in repr(
        _records(services, scope.run_id)
    )
    await scope.close()


@pytest.mark.asyncio
async def test_success_delivery_is_exactly_once() -> None:
    """at-most-once 基线：成功主链正文只出现一次且仅一个 Gate 终态。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        shape2_planning_json(),
        output_for={"synthesis_agent": "ONCE_FINAL"},
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("core_router", "delivery exactly once")

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    types = _types(services, scope.run_id)
    assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
    gate = scope.coordinator.output_gate
    assert gate is not None and gate.state is OutputGateState.PUBLISHED
    assert gate.attempted is True and gate.terminal is True
    await scope.close()
