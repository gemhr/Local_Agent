from __future__ import annotations

import asyncio

import pytest

from core.runtime import (
    ActiveRunControlHandle,
    CancellationSource,
    FaultAction,
    FaultBlocker,
    FaultPoint,
    GracefulShutdownCoordinator,
    RuntimeAdmissionState,
    RuntimeLifecycleState,
)
from dataclasses import replace
from tests._runtime_assembly_fixtures import make_services
from tests._shutdown_fault_fixtures import (
    RecordingResource,
    RecordingWorker,
    shutdown_controller,
    shutdown_rule,
)
from tests.test_shutdown_component_fault import services_for_components
from tests.test_observability_dispatcher import dispatcher


@pytest.mark.asyncio
async def test_cancelled_shutdown_reentry_continues_without_reclosing_successes():
    calls: list[str] = []
    snapshot = RecordingResource("snapshot", calls)
    journal = RecordingResource("journal", calls)
    remaining = RecordingResource("remaining", calls)
    services = services_for_components(
        journal=journal,
        snapshot=snapshot,
        extra=(("remaining_store", remaining),),
    )
    blocker = FaultBlocker(timeout_seconds=2)
    rule = shutdown_rule(
        FaultPoint.SHUTDOWN_BEFORE_JOURNAL_CLOSE,
        rule_id="journal-block",
        shutdown_component="event_journal",
        action=FaultAction.BLOCK_UNTIL_RELEASED,
    )
    controller = shutdown_controller(
        rule,
        blockers={"journal-block": blocker},
    )
    coordinator = GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=1,
    )

    first = asyncio.create_task(coordinator.shutdown(controller))
    await asyncio.wait_for(blocker.entered.wait(), 1)
    assert snapshot.close_calls == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert services.lifecycle_state is RuntimeLifecycleState.SHUTTING_DOWN
    assert services.admission_gate.state is RuntimeAdmissionState.DRAINING
    controller.close()

    report = await asyncio.wait_for(coordinator.shutdown(), 1)
    repeated = await coordinator.shutdown()

    assert report is repeated
    assert report.orchestration_completed is True
    assert report.fully_closed is True
    assert snapshot.close_calls == 1
    assert journal.close_calls == 1
    assert remaining.close_calls == 1
    assert services.admission_gate.state is RuntimeAdmissionState.CLOSED
    assert services.lifecycle_state is RuntimeLifecycleState.CLOSED


@pytest.mark.asyncio
async def test_cancelled_run_cancel_seam_is_retried_without_reopening_admission():
    services = make_services(snapshot_enabled=False)
    source = CancellationSource()

    async def abort(_reason):
        services.run_registry.unregister("run-a")

    services.run_registry.register(
        ActiveRunControlHandle(
            run_id="run-a",
            runtime_mode="COORDINATED",
            cancellation_source=source,
            owner="test",
            force_abort_callback=abort,
        )
    )
    blocker = FaultBlocker(timeout_seconds=2)
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_BEFORE_RUN_CANCEL,
            rule_id="run-cancel-block",
            run_id="run-a",
            action=FaultAction.BLOCK_UNTIL_RELEASED,
        ),
        blockers={"run-cancel-block": blocker},
    )
    coordinator = GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=1,
    )
    first = asyncio.create_task(coordinator.shutdown(controller))
    await asyncio.wait_for(blocker.entered.wait(), 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert source.token.is_cancelled() is False
    assert services.admission_gate.state is RuntimeAdmissionState.DRAINING
    controller.close()
    report = await asyncio.wait_for(coordinator.shutdown(), 1)
    assert source.token.is_cancelled() is True
    assert report.orchestration_completed is report.fully_closed is True
    assert report.active_run_count == report.cancel_requested_count == 1


@pytest.mark.asyncio
async def test_cancelled_worker_drain_seam_preserves_worker_truth_for_reentry():
    calls: list[str] = []
    worker = RecordingWorker(calls, active=1)
    model = RecordingResource("model", calls)
    services = replace(
        make_services(snapshot_enabled=False),
        blocking_executors=(worker,),
        extra_closeables=(("model_engine_0", model),),
    )
    blocker = FaultBlocker(timeout_seconds=2)
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_BEFORE_WORKER_DRAIN,
            rule_id="worker-drain-block",
            action=FaultAction.BLOCK_UNTIL_RELEASED,
        ),
        blockers={"worker-drain-block": blocker},
    )
    coordinator = GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=1,
    )
    first = asyncio.create_task(coordinator.shutdown(controller))
    await asyncio.wait_for(blocker.entered.wait(), 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert worker.wait_calls == 0
    assert worker.active_worker_count == 1
    controller.close()
    report = await asyncio.wait_for(coordinator.shutdown(), 1)
    assert worker.wait_calls == 1
    assert model.close_calls == 1
    assert report.worker_drain_status == "IDLE"
    assert report.fully_closed is True


@pytest.mark.asyncio
async def test_cancelled_flush_seam_allows_second_shutdown_to_finish():
    observability, *_ = dispatcher()
    services = replace(
        make_services(snapshot_enabled=False),
        observability_dispatcher=observability,
    )
    blocker = FaultBlocker(timeout_seconds=2)
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.OBSERVABILITY_BEFORE_FLUSH,
            rule_id="flush-block",
            action=FaultAction.BLOCK_UNTIL_RELEASED,
        ),
        blockers={"flush-block": blocker},
    )
    coordinator = GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=1,
    )
    first = asyncio.create_task(coordinator.shutdown(controller))
    await asyncio.wait_for(blocker.entered.wait(), 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert services.admission_gate.state is RuntimeAdmissionState.DRAINING
    assert services.lifecycle_state is RuntimeLifecycleState.SHUTTING_DOWN
    controller.close()
    report = await asyncio.wait_for(coordinator.shutdown(), 1)
    assert report.observability_flush_status == "COMPLETED"
    assert report.orchestration_completed is report.fully_closed is True
