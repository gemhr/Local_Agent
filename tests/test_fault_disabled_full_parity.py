from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime

import pytest

from core.runtime import (
    CoordinatedRuntimeFactory,
    FaultAction,
    FaultInjectionController,
    FaultInjectionRecorder,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    GracefulShutdownCoordinator,
    InjectedFaultCode,
    RunRegistry,
)
from tests._runtime_assembly_fixtures import FakeRouter, make_services


def _disabled(recorder: FaultInjectionRecorder) -> FaultInjectionController:
    rule = FaultRule(
        rule_id="disabled-full-chain",
        fault_point=FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
        action=FaultAction.RAISE_TYPED_ERROR,
        trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.RUN_SCOPE,
        max_hits=1,
        component="model",
        safe_fault_code=InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
    )
    return FaultInjectionController(
        FaultPlan(
            "disabled-full-chain",
            (rule,),
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        enabled=False,
        recorder=recorder,
    )


def _event_shape(events):
    def payload_shape(payload):
        value = asdict(payload) if is_dataclass(payload) else payload
        if isinstance(value, dict):
            value.pop("duration_ms", None)
        return value

    return tuple(
        (
            event.sequence,
            event.event_type,
            event.component,
            event.step_id,
            event.step_sequence,
            payload_shape(event.payload),
        )
        for event in events
    )


def _shutdown_shape(report):
    return (
        report.state,
        report.lifecycle_state,
        report.orchestration_completed,
        report.fully_closed,
        report.has_failures,
        report.has_deferred_resources,
        report.worker_drain_status,
        report.remaining_run_count,
        tuple(
            (item.component, item.operation, item.status, item.error_code)
            for item in report.components
        ),
    )


async def _run(controller):
    registry = RunRegistry()
    services = make_services(run_registry=registry, snapshot_enabled=False)
    router = FakeRouter()
    scope = await CoordinatedRuntimeFactory(router, services).create_run_scope(
        "agent-a",
        "question",
        run_id="fixed-run",
        trace_id="fixed-trace",
        persist=False,
        fault_controller=controller,
    )
    result = await scope.execute()
    await scope.event_channel.close()
    events = [event async for event in scope.event_channel]
    await scope.close()
    def safe_payload_shape(payload):
        value = dict(payload)
        value.pop("duration_ms", None)
        return value

    journal_shape = tuple(
        (
            row.sequence,
            row.event_type,
            row.component,
            row.step_id,
            row.step_sequence,
            safe_payload_shape(row.safe_payload),
        )
        for row in services.event_journal.read_after("fixed-run", 0, 100)
    )
    metrics = services.observability_dispatcher.records
    worker_snapshot = tuple(
        getattr(tracker, "snapshot")()
        for tracker in services.worker_trackers
        if callable(getattr(tracker, "snapshot", None))
    )
    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.2,
    ).shutdown(controller)
    result_shape = (
        result.run_id,
        result.plan_id,
        result.status,
        result.stop_reason,
        result.succeeded_step_ids,
        result.failed_step_ids,
        result.cancelled_step_ids,
        result.blocked_step_ids,
        result.budget_snapshot.committed_usage,
    )
    return (
        result_shape,
        _event_shape(events),
        journal_shape,
        tuple((item.sequence, item.event_type) for item in metrics),
        worker_snapshot,
        _shutdown_shape(report),
        registry.observability_snapshot()["active_runs"],
    )


@pytest.mark.asyncio
async def test_no_controller_and_disabled_controller_have_full_chain_parity() -> None:
    recorder = FaultInjectionRecorder()
    controller = _disabled(recorder)
    without = await _run(None)
    with_disabled = await _run(controller)

    assert without == with_disabled
    assert recorder.snapshot().records == ()
    counter = controller.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (0, 0)
