from __future__ import annotations

from dataclasses import replace

import pytest

from core.runtime import FaultAction, FaultPoint, GracefulShutdownCoordinator
from tests._runtime_assembly_fixtures import make_services
from tests._shutdown_fault_fixtures import (
    RecordingResource,
    RecordingWorker,
    shutdown_controller,
    shutdown_rule,
)


def services_with_worker(worker, model, remaining):
    return replace(
        make_services(snapshot_enabled=False),
        blocking_executors=(worker,),
        legacy_step_executor=worker,
        extra_closeables=(
            ("model_engine_0", model),
            ("remaining_store", remaining),
        ),
    )


@pytest.mark.asyncio
async def test_worker_drain_fault_preserves_worker_truth_and_defers_model():
    calls: list[str] = []
    worker = RecordingWorker(calls, active=1, detached=1)
    model = RecordingResource("model", calls)
    remaining = RecordingResource("remaining", calls)
    services = services_with_worker(worker, model, remaining)
    controller = shutdown_controller(
        shutdown_rule(FaultPoint.SHUTDOWN_BEFORE_WORKER_DRAIN)
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    assert worker.wait_calls == 0
    assert report.worker_drain_status == "FAILED"
    assert report.active_worker_count == 1
    assert report.detached_worker_count == 1
    assert model.close_calls == 0
    assert remaining.close_calls == 1
    assert "RUNTIME_WORKER_DRAIN_INJECTED_FAILURE" in report.error_codes
    assert "RUNTIME_MODEL_CLOSE_DEFERRED_ACTIVE_WORKER" in report.error_codes


@pytest.mark.asyncio
async def test_worker_drain_delay_is_bounded_and_not_reported_idle():
    calls: list[str] = []
    worker = RecordingWorker(calls, active=1)
    model = RecordingResource("model", calls)
    services = services_with_worker(
        worker, model, RecordingResource("remaining", calls)
    )
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_BEFORE_WORKER_DRAIN,
            action=FaultAction.DELAY,
            delay_seconds=1,
        )
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.02,
    ).shutdown(controller)

    assert worker.wait_calls == 0
    assert report.worker_drain_status == "FAILED"
    assert "RUNTIME_WORKER_DRAIN_INJECTED_TIMEOUT" in report.error_codes
    assert model.close_calls == 0


@pytest.mark.asyncio
async def test_worker_naturally_idle_after_fault_still_does_not_qualify_model_close():
    calls: list[str] = []
    worker = RecordingWorker(calls, active=0, detached=0)
    model = RecordingResource("model", calls)
    services = services_with_worker(
        worker, model, RecordingResource("remaining", calls)
    )
    controller = shutdown_controller(
        shutdown_rule(FaultPoint.SHUTDOWN_BEFORE_WORKER_DRAIN)
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    assert (report.active_worker_count, report.detached_worker_count) == (0, 0)
    assert report.worker_drain_status == "FAILED"
    assert model.close_calls == 0


@pytest.mark.asyncio
async def test_no_worker_drain_operation_does_not_consume_fault_rule():
    services = make_services(snapshot_enabled=False)
    controller = shutdown_controller(
        shutdown_rule(FaultPoint.SHUTDOWN_BEFORE_WORKER_DRAIN)
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    counter = controller.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (0, 0)
    assert report.worker_drain_status == "IDLE"
