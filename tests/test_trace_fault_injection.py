from __future__ import annotations

import pytest

from core.runtime import (
    CoordinatedRuntimeFactory,
    FaultPoint,
    InMemorySpanRecorder,
    OperationScopedSpanRecorder,
    RunStatus,
    RuntimeEventType,
)
from core.runtime.tracing import current_trace_context
from tests._diagnostic_fault_fixtures import diagnostic_controller
from tests._runtime_assembly_fixtures import FakeRouter, make_services


@pytest.mark.parametrize(
    "component",
    (
        "runtime",
        "planner",
        "step",
        "model_invocation",
        "tool_attempt",
        "retrieval_stage",
    ),
)
def test_span_start_fault_creates_no_identity_context_or_active_gauge(component):
    recorder = InMemorySpanRecorder()
    controller = diagnostic_controller(
        FaultPoint.TRACE_BEFORE_SPAN_START,
        component=component,
    )
    facade = OperationScopedSpanRecorder(
        recorder, fault_controller=controller
    )

    handle = facade.start_span(
        trace_id="trace",
        run_id="run",
        component=component,
        operation="operation",
    )

    assert handle.context is None
    assert current_trace_context() is None
    assert handle.end_ok() is None
    health = recorder.health_snapshot()
    assert health.active_span_count == 0
    assert health.completed_span_count == 0
    assert health.dropped_span_count == 1
    assert health.start_failures == 1
    assert health.status == "DEGRADED"
    assert health.last_safe_error_code == "TRACE_SPAN_START_FAILED"
    assert recorder.snapshot() == ()


@pytest.mark.asyncio
async def test_run_span_start_fault_is_best_effort_and_business_runs_once():
    class CountingRouter(FakeRouter):
        def __init__(self):
            super().__init__()
            self.call_count = 0

        def complete_single_agent(self, agent_id, query, **kwargs):
            self.call_count += 1
            return super().complete_single_agent(agent_id, query, **kwargs)

    recorder = InMemorySpanRecorder()
    services = make_services(span_recorder=recorder, snapshot_enabled=False)
    router = CountingRouter()
    controller = diagnostic_controller(
        FaultPoint.TRACE_BEFORE_SPAN_START,
        component="runtime",
    )
    scope = await CoordinatedRuntimeFactory(router, services).create_run_scope(
        "agent", "query", fault_controller=controller
    )

    result = await scope.execute()
    records = services.event_journal.read_after(scope.run_id, 0, 100)

    assert result.status is RunStatus.SUCCEEDED
    assert router.call_count == 1
    assert sum(
        item.event_type is RuntimeEventType.RUN_COMPLETED for item in records
    ) == 1
    assert all(item.verify() is None for item in records)
    assert recorder.health_snapshot().active_span_count == 0
    assert recorder.health_snapshot().start_failures == 1
    assert current_trace_context() is None
    await scope.close()


@pytest.mark.asyncio
async def test_run_span_end_fault_does_not_change_runtime_result_or_journal():
    recorder = InMemorySpanRecorder()
    services = make_services(span_recorder=recorder, snapshot_enabled=False)
    controller = diagnostic_controller(
        FaultPoint.TRACE_BEFORE_SPAN_END,
        component="runtime",
    )
    scope = await CoordinatedRuntimeFactory(
        FakeRouter(), services
    ).create_run_scope("agent", "query", fault_controller=controller)

    result = await scope.execute()
    records = services.event_journal.read_after(scope.run_id, 0, 100)

    assert result.status is RunStatus.SUCCEEDED
    assert records[-1].event_type is RuntimeEventType.RUN_COMPLETED
    assert recorder.health_snapshot().active_span_count == 0
    assert recorder.health_snapshot().end_failures == 1
    assert current_trace_context() is None
    await scope.close()
