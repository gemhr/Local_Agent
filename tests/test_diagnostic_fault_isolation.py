from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.runtime import (
    CheckpointKind,
    CheckpointMode,
    CheckpointStatus,
    CoordinatedRuntimeFactory,
    FaultAction,
    FaultInjectionController,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InMemoryMetricsRecorder,
    InMemorySpanRecorder,
    InjectedFaultCode,
    RecoveryStatus,
    RunStatus,
)
from tests._runtime_assembly_fixtures import FakeRouter, make_services
from tests.test_observability_dispatcher import dispatcher


def combined_controller():
    rules = (
        FaultRule(
            rule_id="observability-record-fault",
            fault_point=FaultPoint.OBSERVABILITY_BEFORE_RECORD,
            action=FaultAction.RAISE_TYPED_ERROR,
            trigger=FaultTrigger.ALWAYS,
            scope=FaultScope.COMPONENT_SCOPE,
            max_hits=100,
            component="observability_dispatcher",
            safe_fault_code=InjectedFaultCode.INJECTED_PERMANENT_FAILURE,
        ),
        FaultRule(
            rule_id="trace-end-fault",
            fault_point=FaultPoint.TRACE_BEFORE_SPAN_END,
            action=FaultAction.RAISE_TYPED_ERROR,
            trigger=FaultTrigger.ALWAYS,
            scope=FaultScope.COMPONENT_SCOPE,
            max_hits=1,
            component="runtime",
            safe_fault_code=InjectedFaultCode.INJECTED_PERMANENT_FAILURE,
            dangerous_window=True,
        ),
    )
    return FaultInjectionController(
        FaultPlan(
            "diagnostic-isolation",
            rules,
            created_at=datetime(2026, 1, 24, tzinfo=UTC),
        )
    )


@pytest.mark.asyncio
async def test_diagnostic_failure_cannot_change_snapshot_or_recovery_authority():
    class CountingRouter(FakeRouter):
        def __init__(self):
            super().__init__()
            self.call_count = 0

        def complete_single_agent(self, agent_id, query, **kwargs):
            self.call_count += 1
            return super().complete_single_agent(agent_id, query, **kwargs)

    observability, logger, metrics, *_ = dispatcher()
    spans = InMemorySpanRecorder(metrics_recorder=metrics)
    services = make_services(
        dispatcher=observability,
        span_recorder=spans,
        snapshot_enabled=True,
    )
    router = CountingRouter()
    controller = combined_controller()
    scope = await CoordinatedRuntimeFactory(router, services).create_run_scope(
        "core_router", "query", fault_controller=controller
    )

    runtime_result = await scope.execute()
    checkpoint = await scope.coordinator.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.TERMINAL,
        timeout=1,
    )
    assessment = services.recovery_validator.validate(
        snapshot_id=checkpoint.snapshot_id,
        current_plan=scope.plan,
    )
    records = services.event_journal.read_after(scope.run_id, 0, 100)

    assert runtime_result.status is RunStatus.SUCCEEDED
    assert router.call_count == 1
    assert checkpoint.status is CheckpointStatus.SAVED
    assert checkpoint.persisted and not checkpoint.partially_persisted
    assert assessment.status is RecoveryStatus.TERMINAL
    assert [item.sequence for item in records] == list(
        range(1, len(records) + 1)
    )
    for record in records:
        record.verify()
    assert logger.records == ()
    assert observability.health.snapshot().record_failures == len(records)
    assert spans.health_snapshot().active_span_count == 0
    assert spans.health_snapshot().end_failures == 1
    assert "OBSERVABILITY" not in scope.driver.output
    assert "TRACE" not in scope.driver.output

    metric_labels = tuple(
        labels
        for _name, labels in (
            *metrics.snapshot().counters.keys(),
            *metrics.snapshot().gauges.keys(),
        )
    )
    assert "observability-record-fault" not in repr(metric_labels)
    assert "trace-end-fault" not in repr(metric_labels)
    await scope.close()
    assert await observability.close()


def test_run_a_trace_fault_does_not_close_or_reparent_shared_recorder():
    from core.runtime import OperationScopedSpanRecorder
    from tests._diagnostic_fault_fixtures import diagnostic_controller

    shared = InMemorySpanRecorder()
    controller_a = diagnostic_controller(
        FaultPoint.TRACE_BEFORE_SPAN_START,
        component="runtime",
    )
    run_a = OperationScopedSpanRecorder(
        shared, fault_controller=controller_a
    )
    failed = run_a.start_span(
        trace_id="trace-a",
        run_id="run-a",
        component="runtime",
        operation="run",
    )
    assert failed.context is None
    controller_a.close()

    healthy = shared.start_span(
        trace_id="trace-b",
        run_id="run-b",
        component="runtime",
        operation="run",
    )
    healthy.end_ok()
    records = shared.snapshot()

    assert len(records) == 1
    assert records[0].trace_id == "trace-b"
    assert records[0].run_id == "run-b"
    assert records[0].parent_span_id is None
    assert shared.health_snapshot().active_span_count == 0


@pytest.mark.asyncio
async def test_diagnostic_health_and_errors_contain_no_sensitive_runtime_data():
    from core.runtime import (
        ObservabilityOperationError,
        TraceOperationError,
    )

    observability, *_ = dispatcher()
    trace = InMemorySpanRecorder().health_snapshot()
    combined = " ".join(
        (
            repr(observability.health.snapshot()),
            repr(trace),
            repr(ObservabilityOperationError("OBSERVABILITY_FLUSH_FAILED")),
            repr(TraceOperationError("TRACE_FLUSH_FAILED")),
        )
    )
    for secret in (
        "SECRET_PROMPT_TEXT",
        "MODEL_OUTPUT_SECRET",
        "TOOL_ARGUMENT_SECRET",
        "TOOL_OUTPUT_SECRET",
        "RAG_CHUNK_SECRET",
        "MEMORY_SECRET",
        "C:\\Users\\private-user",
        "provider-secret-error",
        "raw-idempotency-key",
        "raw-resource-key",
        "raw-snapshot-payload",
    ):
        assert secret not in combined
    assert await observability.close()
