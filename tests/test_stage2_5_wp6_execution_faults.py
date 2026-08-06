"""WP6 specialist / store / synthesis failure matrix through real seams."""

from __future__ import annotations

import pytest

from core.runtime import (
    CoordinatedRuntimeFactory,
    FaultPoint,
    RunStatus,
    RuntimeEventType,
)
from tests._stage2_5_wp6_fixtures import wp6_controller
from tests._wp3_fixtures import (
    Wp3RecordingRouter,
    make_wp3_services,
    shape3_planning_json,
)


def _records(services, run_id: str):
    return services.event_journal.read_after(run_id, 0, 1000)


def _types(services, run_id: str) -> list[RuntimeEventType]:
    return [item.event_type for item in _records(services, run_id)]


async def _run_with_controller(
    services,
    router,
    controller,
    *,
    agent_id: str = "core_router",
    query: str = "coordinate two reviews",
):
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        agent_id,
        query,
        fault_controller=controller,
    )
    result = await scope.execute()
    return scope, result


@pytest.mark.asyncio
async def test_store_write_prepared_failure_fails_closed() -> None:
    """FP-STORE-01: write_prepared 失败 -> STEP_RESULT_PREPARE_FAILED。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter(shape3_planning_json())
    controller = wp6_controller(
        FaultPoint.STORE_BEFORE_WRITE_PREPARED,
        component="step_result_store",
        operation_kind="WRITE_PREPARED",
        step_id="task-code",
    )
    scope, result = await _run_with_controller(
        services, router, controller
    )

    assert result.status is RunStatus.FAILED
    assert result.error_code == "STEP_RESULT_PREPARE_FAILED"
    assert result.failed_step_ids == ("task-code",)
    assert "synthesis" not in result.succeeded_step_ids
    assert len(router.calls_for("synthesis_agent")) == 0
    types = _types(services, scope.run_id)
    assert RuntimeEventType.OUTPUT_DELTA not in types
    assert router.memory_manager.count_messages("core_router") == 0
    await scope.close()


@pytest.mark.asyncio
async def test_mark_readable_failure_keeps_final_succeeded() -> None:
    """FP-STORE-03/11: mark_readable 失败 -> Step 保持 SUCCEEDED，
    Run 以 STEP_RESULT_COMMIT_FAILED 失败，不重发正文。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        shape3_planning_json(),
        output_for={"synthesis_agent": "SECRET_FINAL_CANDIDATE"},
    )
    controller = wp6_controller(
        FaultPoint.STORE_BEFORE_MARK_READABLE,
        component="step_result_store",
        operation_kind="MARK_READABLE",
        step_id="synthesis",
    )
    scope, result = await _run_with_controller(
        services, router, controller
    )

    assert result.status is RunStatus.FAILED
    assert result.error_code == "STEP_RESULT_COMMIT_FAILED"
    # synthesis Step 状态已 SUCCEEDED（提交完成），仅 Store READABLE 失败。
    assert "synthesis" in result.succeeded_step_ids
    types = _types(services, scope.run_id)
    assert RuntimeEventType.OUTPUT_DELTA not in types
    assert router.memory_manager.count_messages("core_router") == 0
    assert "SECRET_FINAL_CANDIDATE" not in repr(
        _records(services, scope.run_id)
    )
    await scope.close()


@pytest.mark.asyncio
async def test_dependency_read_failure_blocks_synthesis() -> None:
    """FP-STORE-10: synthesis 读取依赖失败 -> SYNTHESIS_FAILED，不 fallback。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter(shape3_planning_json())
    controller = wp6_controller(
        FaultPoint.STORE_BEFORE_DEPENDENCY_READ,
        component="step_result_store",
        operation_kind="DEPENDENCY_READ",
        step_id="synthesis",
    )
    scope, result = await _run_with_controller(
        services, router, controller
    )

    assert result.status is RunStatus.FAILED
    assert result.error_code == "SYNTHESIS_FAILED"
    assert len(router.calls_for("synthesis_agent")) == 0
    types = _types(services, scope.run_id)
    assert RuntimeEventType.OUTPUT_DELTA not in types
    assert router.memory_manager.count_messages("core_router") == 0
    await scope.close()


@pytest.mark.asyncio
async def test_step_before_driver_execute_fails_specialist() -> None:
    """FP-SPEC-01/02: 单 specialist 模型失败 -> synthesis BLOCKED，
    Run 以 REQUIRED_DEPENDENCY_FAILED fail closed；
    无 partial final、无 Memory、无 Core fallback。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter(shape3_planning_json())
    controller = wp6_controller(
        FaultPoint.STEP_BEFORE_DRIVER_EXECUTE,
        component="multi_agent_driver",
        operation_kind="DRIVER_EXECUTE",
        step_id="task-code",
    )
    scope, result = await _run_with_controller(
        services, router, controller
    )

    assert result.status is RunStatus.FAILED
    assert result.error_code == "REQUIRED_DEPENDENCY_FAILED"
    assert len(router.calls_for("code_expert")) == 0
    assert len(router.calls_for("synthesis_agent")) == 0
    types = _types(services, scope.run_id)
    assert RuntimeEventType.OUTPUT_DELTA not in types
    assert router.memory_manager.count_messages("core_router") == 0
    await scope.close()


@pytest.mark.asyncio
async def test_synthesis_driver_fault_is_synthesis_failed() -> None:
    """FP-SYNTH-05: synthesis 模型失败 -> SYNTHESIS_FAILED。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        shape3_planning_json(),
        output_for={"synthesis_agent": "SHOULD_NOT_PUBLISH"},
    )
    controller = wp6_controller(
        FaultPoint.STEP_BEFORE_DRIVER_EXECUTE,
        component="multi_agent_driver",
        operation_kind="DRIVER_EXECUTE",
        step_id="synthesis",
    )
    scope, result = await _run_with_controller(
        services, router, controller
    )

    assert result.status is RunStatus.FAILED
    assert result.error_code == "SYNTHESIS_FAILED"
    assert len(router.calls_for("synthesis_agent")) == 0
    types = _types(services, scope.run_id)
    assert RuntimeEventType.OUTPUT_DELTA not in types
    assert router.memory_manager.count_messages("core_router") == 0
    assert "SHOULD_NOT_PUBLISH" not in repr(_records(services, scope.run_id))
    await scope.close()


@pytest.mark.asyncio
async def test_executor_submit_failure_fails_closed() -> None:
    """FP-SPEC-13: bounded executor 提交失败 -> synthesis BLOCKED，
    Run 以 REQUIRED_DEPENDENCY_FAILED fail closed。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter(shape3_planning_json())
    controller = wp6_controller(
        FaultPoint.EXECUTOR_BEFORE_SUBMIT,
        component="parallel_executor",
        operation_kind="EXECUTOR_SUBMIT",
        step_id="task-code",
    )
    scope, result = await _run_with_controller(
        services, router, controller
    )

    assert result.status is RunStatus.FAILED
    assert result.error_code == "REQUIRED_DEPENDENCY_FAILED"
    assert len(router.calls_for("code_expert")) == 0
    assert len(router.calls_for("synthesis_agent")) == 0
    types = _types(services, scope.run_id)
    assert RuntimeEventType.OUTPUT_DELTA not in types
    assert router.memory_manager.count_messages("core_router") == 0
    await scope.close()


@pytest.mark.asyncio
async def test_specialist_failure_never_falls_back_to_core() -> None:
    """required dependency fail-closed：specialist 失败后 synthesis BLOCKED，
    Core 不回答，无 partial final。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        shape3_planning_json(),
        fail_agents=("knowledge_expert",),
    )
    scope, result = await _run_with_controller(
        services, router, None
    )

    assert result.status is RunStatus.FAILED
    assert result.error_code == "REQUIRED_DEPENDENCY_FAILED"
    assert "task-knowledge" in result.failed_step_ids
    assert "synthesis" not in result.succeeded_step_ids
    assert "task-knowledge" in result.blocked_step_ids or "synthesis" in result.blocked_step_ids
    assert len(router.calls_for("synthesis_agent")) == 0
    assert len(router.calls_for("core_router")) == 0
    types = _types(services, scope.run_id)
    assert RuntimeEventType.OUTPUT_DELTA not in types
    assert router.memory_manager.count_messages("core_router") == 0
    await scope.close()


def test_verify_triple_identity_rejects_mismatch() -> None:
    """FP-SPEC-07/08: adapter/binding identity mismatch fail closed。"""
    from core.runtime import (
        DEFAULT_AGENT_REGISTRY,
        ExecutionKind,
        MultiAgentDriver,
        MultiAgentDriverError,
        MultiAgentDriverErrorCode,
        OutputPolicy,
        Plan,
        PlanSource,
        PlanStep,
        StepClaim,
        TaskCapabilityRequirements,
    )
    from datetime import UTC, datetime

    capabilities = TaskCapabilityRequirements()
    step = PlanStep(
        "task-code",
        "task-code",
        "desc",
        (),
        "done",
        "knowledge_expert",
        capabilities,
        ExecutionKind.AGENT,
        OutputPolicy.INTERNAL,
    )
    plan = Plan(
        "plan-1",
        1,
        "summary",
        (step,),
        datetime.now(UTC),
        PlanSource.MODEL_GENERATED,
    )
    claim = StepClaim(
        plan.plan_id,
        plan.version,
        step.step_id,
        datetime.now(UTC),
        step.capability_requirements,
        "code_expert",
    )
    registration = DEFAULT_AGENT_REGISTRY.resolve("code_expert")
    with pytest.raises(MultiAgentDriverError) as exc:
        MultiAgentDriver._verify_triple_identity(
            claim=claim,
            plan_step=step,
            binding_agent_id="data_analyst",
            registration=registration,
        )
    assert exc.value.error_code is MultiAgentDriverErrorCode.REGISTRY_MISMATCH
