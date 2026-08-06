from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from core.runtime import CoordinatedRuntimeFactory, RunStatus, StopReason
from core.runtime.budget import BudgetExceededError
from core.runtime.cancellation import CancellationReason
from core.runtime.checkpoint_contract import CheckpointKind
from core.runtime.events import RuntimeEventType
from core.runtime.metrics import InMemoryMetricsRecorder
from core.runtime.parallel_execution import StepExecutionMode
from core.runtime.run_coordinator import DynamicPlanState, RunCoordinatorError
from core.runtime.recovery_contract import RecoveryReason, RecoveryStatus
from tests._runtime_assembly_fixtures import FakeRouter, make_services


def direct_json(agent_id: str = "core_router") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "decision": "DIRECT_ANSWER",
            "agent_id": agent_id,
            "reason_code": "MODEL_DIRECT",
        }
    )


def delegated_json(*, multi_step: bool) -> str:
    tasks = [
        {
            "task_id": "code",
            "agent_id": "code_expert",
            "instruction": "Inspect the implementation contract.",
            "capabilities": ["code_reasoning"],
        }
    ]
    if multi_step:
        tasks.append(
            {
                "task_id": "data",
                "agent_id": "data_analyst",
                "instruction": "Inspect the data contract.",
                "capabilities": ["data_analysis"],
            }
        )
    return json.dumps(
        {
            "schema_version": 1,
            "decision": "DELEGATE",
            "tasks": tasks,
            "synthesis_required": multi_step,
        }
    )


class RecordingRouter(FakeRouter):
    def __init__(self, planning_output: str | None = None) -> None:
        super().__init__()
        self.planning_output = planning_output or direct_json()
        self.planning_calls = 0
        self.agent_calls: list[tuple[str, str]] = []

    def complete_planning_decision(self, user_request: str, **kwargs) -> str:
        self.planning_calls += 1
        return self.planning_output

    def complete_single_agent(self, agent_id: str, query: str, **kwargs) -> str:
        self.agent_calls.append((agent_id, query))
        return "dynamic-output"


def event_records(services, run_id: str):
    return services.event_journal.read_after(run_id, 0, 1000)


def event_types(services, run_id: str) -> list[RuntimeEventType]:
    return [record.event_type for record in event_records(services, run_id)]


def last_index(types: list, event_type) -> int:
    return len(types) - 1 - list(reversed(types)).index(event_type)


@pytest.mark.asyncio
async def test_dynamic_scope_delays_plan_bound_components_and_freezes_once() -> None:
    services = make_services()
    router = RecordingRouter()
    scope = await CoordinatedRuntimeFactory(router, services).create_run_scope(
        "core_router", "original user request"
    )

    assert scope.plan is None
    assert scope.scheduler is None
    assert scope.executor is None
    assert scope.checkpoint_coordinator is None
    assert scope.coordinator.dynamic_plan_state is DynamicPlanState.UNRESOLVED

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert scope.coordinator.plan_frozen is True
    assert scope.plan is scope.coordinator.plan
    assert scope.scheduler is scope.coordinator.scheduler
    assert scope.executor is scope.coordinator.executor
    assert scope.checkpoint_coordinator is not None
    assert router.planning_calls == 1
    assert router.agent_calls == [("core_router", "original user request")]
    assert scope.coordinator.invocation_bindings is None

    records = event_records(services, scope.run_id)
    types = [record.event_type for record in records]
    assert types.index(RuntimeEventType.RUN_STARTED) < types.index(
        RuntimeEventType.PLANNING_STARTED
    ) < types.index(RuntimeEventType.PLAN_CREATED) < types.index(
        RuntimeEventType.STEP_STARTED
    )
    assert types.count(RuntimeEventType.PLAN_CREATED) == 1
    snapshots = services.snapshot_store.list_for_run(scope.run_id, 10)
    assert len(snapshots) == 1
    assert snapshots[0].checkpoint_kind == CheckpointKind.POST_PLAN_PRE_EXECUTION.value
    plan_created = next(
        record for record in records if record.event_type is RuntimeEventType.PLAN_CREATED
    )
    assert snapshots[0].last_journal_sequence == plan_created.sequence
    recovery = services.recovery_validator.assess_snapshot(
        snapshot=snapshots[0], current_plan=scope.plan
    )
    assert recovery.status is RecoveryStatus.UNSUPPORTED
    assert RecoveryReason.UNSUPPORTED_CHECKPOINT_KIND in recovery.reasons

    with pytest.raises(RunCoordinatorError, match="DYNAMIC_PLAN_STATE_INVALID"):
        await scope.coordinator._prepare_dynamic_execution()
    await scope.close()


@pytest.mark.asyncio
async def test_execution_before_dynamic_plan_freeze_fails_explicitly() -> None:
    services = make_services(snapshot_enabled=False)
    scope = await CoordinatedRuntimeFactory(
        RecordingRouter(), services
    ).create_run_scope("core_router", "request")

    with pytest.raises(RunCoordinatorError, match="EXECUTION_BEFORE_PLAN_FROZEN"):
        await scope.coordinator._execute_batches(
            driver=scope.driver,
            execution_mode=StepExecutionMode.SYNC_BLOCKING,
            concurrency_specs=None,
        )
    await scope.close()


@pytest.mark.asyncio
async def test_static_scope_has_no_planning_events() -> None:
    services = make_services(snapshot_enabled=False)
    router = RecordingRouter()
    scope = await CoordinatedRuntimeFactory(router, services).create_static_run_scope(
        "core_router", "static request"
    )
    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert router.planning_calls == 0
    types = event_types(services, scope.run_id)
    assert RuntimeEventType.PLANNING_STARTED not in types
    assert RuntimeEventType.PLAN_CREATED not in types
    await scope.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_id",
    ["knowledge_expert", "code_expert", "data_analyst"],
)
async def test_explicit_specialist_is_deterministic_and_uses_original_binding(
    agent_id: str,
) -> None:
    services = make_services(snapshot_enabled=False)
    router = RecordingRouter()
    request = f"explicit request for {agent_id}"
    scope = await CoordinatedRuntimeFactory(router, services).create_run_scope(
        agent_id, request
    )

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert router.planning_calls == 0
    assert router.agent_calls == [(agent_id, request)]
    assert RuntimeEventType.PLANNING_STARTED in event_types(services, scope.run_id)
    assert RuntimeEventType.PLAN_CREATED in event_types(services, scope.run_id)
    await scope.close()


@pytest.mark.asyncio
async def test_deterministic_planning_records_safe_source_and_duration_metrics() -> None:
    metrics = InMemoryMetricsRecorder()
    services = make_services(
        snapshot_enabled=False,
        runtime_metrics_recorder=metrics,
    )
    scope = await CoordinatedRuntimeFactory(
        RecordingRouter(), services
    ).create_run_scope("knowledge_expert", "metric request")

    result = await scope.execute()
    snapshot = metrics.snapshot()
    labels = {
        "planning_source": "EXPLICIT_ENTRY",
        "status": "SUCCEEDED",
    }

    assert result.status is RunStatus.SUCCEEDED
    assert snapshot.counter("runtime_planning_total", labels) == 1
    assert len(snapshot.histogram("runtime_planning_duration_seconds", labels)) == 1
    await scope.close()


@pytest.mark.asyncio
async def test_delegated_knowledge_direct_uses_binding_instruction() -> None:
    services = make_services(snapshot_enabled=False)
    router = RecordingRouter()
    request = "调用知识专家，总结 cdt_field_mapping.md"
    scope = await CoordinatedRuntimeFactory(router, services).create_run_scope(
        "core_router", request
    )

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert router.planning_calls == 0
    assert router.agent_calls == [("knowledge_expert", request)]
    await scope.close()


@pytest.mark.asyncio
async def test_multi_step_plan_runs_and_delivers_unique_final() -> None:
    services = make_services()
    router = RecordingRouter(delegated_json(multi_step=True))
    scope = await CoordinatedRuntimeFactory(router, services).create_run_scope(
        "core_router", "coordinate two professional reviews"
    )

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert result.stop_reason is StopReason.COMPLETED
    assert result.error_code is None
    assert scope.coordinator.plan_frozen is True
    assert sorted(agent for agent, _ in router.agent_calls) == [
        "code_expert",
        "data_analyst",
        "synthesis_agent",
    ]
    assert scope.coordinator.invocation_bindings is None
    types = event_types(services, scope.run_id)
    assert RuntimeEventType.PLAN_CREATED in types
    assert types.count(RuntimeEventType.STEP_STARTED) == 3
    assert types.count(RuntimeEventType.STEP_COMPLETED) == 3
    assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
    assert types.count(RuntimeEventType.RUN_COMPLETED) == 1
    assert types.index(RuntimeEventType.OUTPUT_DELTA) < last_index(
        types,
        RuntimeEventType.STEP_COMPLETED
    )
    # The unique final text is exactly the synthesis candidate (journal keeps
    # only the safe text digest, never the raw body).
    output_records = [
        record
        for record in event_records(services, scope.run_id)
        if record.event_type is RuntimeEventType.OUTPUT_DELTA
    ]
    assert len(output_records) == 1
    assert output_records[0].safe_payload["text_digest"] == hashlib.sha256(
        "dynamic-output".encode("utf-8")
    ).hexdigest()
    assert len(services.snapshot_store.list_for_run(scope.run_id, 10)) == 1
    store = scope.coordinator.step_result_store
    assert store is not None
    assert store.is_cleared is True
    assert store.entry_count() == 0
    await scope.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selected_agent", "planning_output", "expected_code"),
    [
        ("missing_agent", direct_json(), "UNKNOWN_AGENT"),
        ("core_router", "not-json /private/path.md", "PLANNER_SCHEMA_INVALID"),
        ("core_router", direct_json("knowledge_expert"), "MODEL_DIRECT_AGENT_NOT_ALLOWED"),
    ],
)
async def test_planning_failures_have_no_plan_checkpoint_step_or_raw_output(
    selected_agent: str,
    planning_output: str,
    expected_code: str,
) -> None:
    services = make_services()
    router = RecordingRouter(planning_output)
    scope = await CoordinatedRuntimeFactory(router, services).create_run_scope(
        selected_agent, "sensitive request /private/path.md"
    )

    result = await scope.execute()

    assert result.status is RunStatus.FAILED
    assert result.stop_reason is StopReason.PLANNING_FAILED
    assert result.error_code == expected_code
    types = event_types(services, scope.run_id)
    assert RuntimeEventType.PLANNING_STARTED in types
    assert RuntimeEventType.PLAN_CREATED not in types
    assert RuntimeEventType.STEP_STARTED not in types
    assert types.count(RuntimeEventType.RUN_COMPLETED) == 1
    assert services.snapshot_store.list_for_run(scope.run_id, 10) == ()
    assert scope.coordinator.dynamic_plan_state is DynamicPlanState.FAILED
    rendered = repr([(record.safe_payload, record.event_type) for record in event_records(services, scope.run_id)])
    assert "not-json" not in rendered
    assert "/private/path.md" not in rendered
    await scope.close()


@pytest.mark.asyncio
async def test_independent_planner_timeout_maps_to_planning_failed() -> None:
    class SlowRouter(RecordingRouter):
        def complete_planning_decision(self, user_request: str, **kwargs) -> str:
            import time

            time.sleep(0.1)
            return super().complete_planning_decision(user_request, **kwargs)

    services = make_services(snapshot_enabled=False)
    scope = await CoordinatedRuntimeFactory(
        SlowRouter(), services, planning_timeout_seconds=0.01
    ).create_run_scope("core_router", "slow planning")

    result = await scope.execute()

    assert result.status is RunStatus.FAILED
    assert result.stop_reason is StopReason.PLANNING_FAILED
    assert result.error_code == "PLANNER_TIMEOUT"
    types = event_types(services, scope.run_id)
    assert RuntimeEventType.PLAN_CREATED not in types
    assert RuntimeEventType.STEP_STARTED not in types
    await scope.close()


@pytest.mark.asyncio
async def test_total_deadline_is_not_reclassified_as_planning_failure() -> None:
    class SlowRouter(RecordingRouter):
        def complete_planning_decision(self, user_request: str, **kwargs) -> str:
            import time

            time.sleep(0.1)
            return super().complete_planning_decision(user_request, **kwargs)

    services = make_services(snapshot_enabled=False)
    scope = await CoordinatedRuntimeFactory(
        SlowRouter(), services, planning_timeout_seconds=1
    ).create_run_scope("core_router", "deadline planning", timeout_seconds=0.01)

    result = await scope.execute()

    assert result.status is RunStatus.FAILED
    assert result.stop_reason is StopReason.DEADLINE_EXCEEDED
    assert RuntimeEventType.PLAN_CREATED not in event_types(services, scope.run_id)
    await scope.close()


@pytest.mark.asyncio
async def test_planning_budget_exhaustion_keeps_existing_budget_mapping() -> None:
    class BudgetRouter(RecordingRouter):
        def complete_planning_decision(self, user_request: str, **kwargs) -> str:
            ledger = kwargs["run_context"].budget_ledger
            raise BudgetExceededError(
                "model_calls", 1, 0, ledger.snapshot()
            )

    services = make_services(snapshot_enabled=False)
    scope = await CoordinatedRuntimeFactory(
        BudgetRouter(), services
    ).create_run_scope("core_router", "budget planning")

    result = await scope.execute()

    assert result.status is RunStatus.FAILED
    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert result.error_code == "BUDGET_EXHAUSTED"
    assert RuntimeEventType.PLAN_CREATED not in event_types(services, scope.run_id)
    await scope.close()


@pytest.mark.asyncio
async def test_user_cancellation_during_planning_is_not_planning_failed() -> None:
    import threading
    import time

    started = threading.Event()

    class CancellableRouter(RecordingRouter):
        def complete_planning_decision(self, user_request: str, **kwargs) -> str:
            started.set()
            context = kwargs["run_context"]
            while True:
                context.raise_if_inactive()
                time.sleep(0.001)

    services = make_services(snapshot_enabled=False)
    scope = await CoordinatedRuntimeFactory(
        CancellableRouter(), services
    ).create_run_scope("core_router", "cancel planning")
    execution = asyncio.create_task(scope.execute())
    assert await asyncio.to_thread(started.wait, 1)
    assert scope.request_cancel(CancellationReason.REQUEST_CANCELLED) is True

    result = await execution

    assert result.status is RunStatus.CANCELLED
    assert result.stop_reason is StopReason.USER_CANCELLED
    assert RuntimeEventType.PLAN_CREATED not in event_types(services, scope.run_id)
    await scope.close()


@pytest.mark.asyncio
async def test_dynamic_bindings_close_and_clear_is_called_on_terminal(monkeypatch) -> None:
    from core.runtime.invocation_bindings import StepInvocationBindings

    closed = []
    original = StepInvocationBindings.close_and_clear

    def record_close(self) -> None:
        closed.append(self)
        original(self)

    monkeypatch.setattr(StepInvocationBindings, "close_and_clear", record_close)
    services = make_services(snapshot_enabled=False)
    scope = await CoordinatedRuntimeFactory(
        RecordingRouter(), services
    ).create_run_scope("core_router", "binding cleanup")

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert len(closed) == 1
    assert closed[0].closed is True
    assert scope.coordinator.invocation_bindings is None
    await scope.close()
