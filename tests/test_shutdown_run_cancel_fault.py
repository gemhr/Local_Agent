from __future__ import annotations

import pytest

from core.runtime import (
    ActiveRunControlHandle,
    CancellationReason,
    CancellationSource,
    FaultAction,
    FaultBlocker,
    FaultPoint,
    GracefulShutdownCoordinator,
)
from tests._runtime_assembly_fixtures import make_services
from tests._shutdown_fault_fixtures import (
    shutdown_controller,
    shutdown_rule,
)


def register_run(services, run_id: str, *, source=None, callback=None):
    active_source = source or CancellationSource()

    async def default_abort(reason):
        services.run_registry.unregister(run_id)

    handle = ActiveRunControlHandle(
        run_id=run_id,
        runtime_mode="COORDINATED",
        cancellation_source=active_source,
        owner="test",
        force_abort_callback=callback or default_abort,
    )
    services.run_registry.register(handle)
    return handle, active_source


@pytest.mark.asyncio
async def test_one_run_cancel_fault_does_not_stop_other_runs_or_force_abort():
    services = make_services(snapshot_enabled=False)
    _, source_a = register_run(services, "run-a")
    _, source_b = register_run(services, "run-b")
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_BEFORE_RUN_CANCEL,
            run_id="run-a",
        )
    )
    coordinator = GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    )

    report = await coordinator.shutdown(controller)

    assert report.active_run_count == 2
    assert report.cancel_requested_count == 1
    assert report.cancelled_run_count == 1
    assert report.cancel_failed_count == 1
    assert report.forced_run_count == 2
    assert report.remaining_run_count == 0
    assert source_a.token.reason is CancellationReason.SERVER_SHUTDOWN
    assert source_b.token.reason is CancellationReason.SERVER_SHUTDOWN
    assert "RUNTIME_RUN_CANCEL_INJECTED_FAILURE" in report.error_codes


@pytest.mark.asyncio
async def test_cancel_callback_failure_isolated_and_client_reason_is_first_wins():
    services = make_services(snapshot_enabled=False)
    disconnected = CancellationSource()
    disconnected.cancel(CancellationReason.CLIENT_DISCONNECTED)

    class BrokenCancelHandle(ActiveRunControlHandle):
        def request_cancel(self, reason):
            raise RuntimeError("provider-secret-error")

    handle = BrokenCancelHandle(
        run_id="run-a",
        runtime_mode="COORDINATED",
        cancellation_source=disconnected,
        owner="test",
    )
    services.run_registry.register(handle)
    _, source_b = register_run(services, "run-b")
    coordinator = GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    )

    report = await coordinator.shutdown()

    assert report.cancel_failed_count == 1
    assert report.cancel_requested_count == 2
    assert source_b.token.reason is CancellationReason.SERVER_SHUTDOWN
    assert disconnected.token.reason is CancellationReason.CLIENT_DISCONNECTED
    assert report.remaining_run_count == 0
    assert "RUNTIME_RUN_CANCEL_FAILED" in report.error_codes


@pytest.mark.asyncio
async def test_repeated_shutdown_does_not_reevaluate_run_cancel_fault():
    services = make_services(snapshot_enabled=False)
    register_run(services, "run-a")
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_BEFORE_RUN_CANCEL,
            run_id="run-a",
        )
    )
    coordinator = GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    )

    first = await coordinator.shutdown(controller)
    before = controller.snapshot()
    second = await coordinator.shutdown(controller)

    assert second is first
    assert controller.snapshot() == before
    assert before.counters[0].hit_count == 1


@pytest.mark.asyncio
async def test_completed_handle_does_not_consume_cancel_fault_rule():
    services = make_services(snapshot_enabled=False)
    handle, _ = register_run(services, "run-a")
    handle.mark_completed()
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_BEFORE_RUN_CANCEL,
            run_id="run-a",
        )
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    counter = controller.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (0, 0)
    assert report.cancel_requested_count == 0


@pytest.mark.asyncio
async def test_run_cancel_block_is_bounded_and_force_abort_still_runs():
    services = make_services(snapshot_enabled=False)
    register_run(services, "run-a")
    blocker = FaultBlocker(timeout_seconds=2)
    rule = shutdown_rule(
        FaultPoint.SHUTDOWN_BEFORE_RUN_CANCEL,
        rule_id="run-block",
        run_id="run-a",
        action=FaultAction.BLOCK_UNTIL_RELEASED,
    )
    controller = shutdown_controller(
        rule,
        blockers={"run-block": blocker},
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.02,
    ).shutdown(controller)

    assert report.cancel_failed_count == 1
    assert report.forced_run_count == 1
    assert report.remaining_run_count == 0
    assert "RUNTIME_RUN_CANCEL_INJECTED_TIMEOUT" in report.error_codes
    controller.close()
