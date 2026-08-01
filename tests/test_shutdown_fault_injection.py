from __future__ import annotations

from dataclasses import replace

import pytest

from core.runtime import FaultPoint, GracefulShutdownCoordinator, InMemorySpanRecorder
from tests._runtime_assembly_fixtures import make_services
from tests._shutdown_fault_fixtures import (
    RecordingResource,
    shutdown_controller,
    shutdown_rule,
)
from tests.test_observability_dispatcher import dispatcher


class CountingSpanRecorder(InMemorySpanRecorder):
    def __init__(self):
        super().__init__()
        self.flush_calls = 0
        self.close_calls = 0

    def flush(self, timeout_seconds=None):
        self.flush_calls += 1
        return super().flush(timeout_seconds)

    def close(self, timeout_seconds=None):
        self.close_calls += 1
        return super().close(timeout_seconds)


def real_diagnostic_services(calls):
    observability, *_ = dispatcher()
    spans = CountingSpanRecorder()
    journal = RecordingResource("journal", calls)
    services = replace(
        make_services(
            dispatcher=observability,
            span_recorder=spans,
            snapshot_enabled=False,
        ),
        event_journal=journal,
        extra_closeables=(("remaining_store", RecordingResource("remaining", calls)),),
    )
    return services, observability, spans, journal


@pytest.mark.asyncio
async def test_observability_flush_fault_still_executes_trace_flush_and_close():
    calls: list[str] = []
    services, observability, spans, journal = real_diagnostic_services(calls)
    controller = shutdown_controller(
        shutdown_rule(FaultPoint.OBSERVABILITY_BEFORE_FLUSH)
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.2,
    ).shutdown(controller)

    assert report.observability_flush_status == "FAILED"
    assert report.trace_flush_status == "COMPLETED"
    assert spans.flush_calls == 1
    assert spans.close_calls == 1
    assert journal.close_calls == 1
    assert "RUNTIME_OBSERVABILITY_FLUSH_FAILED" in report.error_codes
    assert observability.health.snapshot().flush_failures == 1


@pytest.mark.asyncio
async def test_trace_flush_fault_still_closes_journal_and_remaining_components():
    calls: list[str] = []
    services, _observability, spans, journal = real_diagnostic_services(calls)
    controller = shutdown_controller(
        shutdown_rule(FaultPoint.TRACE_BEFORE_FLUSH)
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.2,
    ).shutdown(controller)

    assert report.observability_flush_status == "COMPLETED"
    assert report.trace_flush_status == "FAILED"
    assert spans.flush_calls == 0
    assert spans.close_calls == 1
    assert journal.close_calls == 1
    assert "remaining.close" in calls
    assert "RUNTIME_TRACE_FLUSH_FAILED" in report.error_codes


@pytest.mark.asyncio
async def test_both_flush_faults_are_reported_without_changing_close_order():
    calls: list[str] = []
    services, _observability, spans, journal = real_diagnostic_services(calls)
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.OBSERVABILITY_BEFORE_FLUSH,
            rule_id="observability-flush",
        ),
        shutdown_rule(
            FaultPoint.TRACE_BEFORE_FLUSH,
            rule_id="trace-flush",
        ),
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.2,
    ).shutdown(controller)

    assert report.observability_flush_status == "FAILED"
    assert report.trace_flush_status == "FAILED"
    assert spans.flush_calls == 0
    assert journal.close_calls == 1
    assert "remaining.close" in calls
    assert {
        "RUNTIME_OBSERVABILITY_FLUSH_FAILED",
        "RUNTIME_TRACE_FLUSH_FAILED",
    } <= set(report.error_codes)
