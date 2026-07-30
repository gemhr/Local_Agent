from __future__ import annotations

import pytest

from core.runtime import (
    ActiveRunControlHandle,
    CancellationReason,
    CancellationSource,
    GracefulShutdownCoordinator,
    RuntimeAdmissionState,
)
from tests._runtime_assembly_fixtures import make_services


@pytest.mark.asyncio
async def test_shutdown_cancels_active_run_forces_timeout_and_is_idempotent():
    services = make_services(snapshot_enabled=False)
    registry = services.run_registry
    source = CancellationSource()

    async def force_abort(reason):
        registry.unregister("run-a")

    handle = ActiveRunControlHandle(
        run_id="run-a",
        runtime_mode="COORDINATED",
        cancellation_source=source,
        owner="test",
        force_abort_callback=force_abort,
    )
    registry.register(handle)
    coordinator = GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.2,
    )

    report = await coordinator.shutdown()
    repeated = await coordinator.shutdown()

    assert report is repeated
    assert report.state is RuntimeAdmissionState.CLOSED
    assert report.active_run_count == 1
    assert report.cancelled_run_count == 1
    assert report.forced_run_count == 1
    assert report.remaining_run_count == 0
    assert source.token.reason is CancellationReason.SERVER_SHUTDOWN
    assert services.run_registry.observability_snapshot()["active_runs"] == 0


@pytest.mark.asyncio
async def test_shutdown_does_not_override_client_disconnect_reason():
    services = make_services(snapshot_enabled=False)
    source = CancellationSource()
    source.cancel(CancellationReason.CLIENT_DISCONNECTED)

    async def force_abort(reason):
        services.run_registry.unregister("run-a")

    services.run_registry.register(
        ActiveRunControlHandle(
            run_id="run-a",
            runtime_mode="COORDINATED",
            cancellation_source=source,
            owner="test",
            force_abort_callback=force_abort,
        )
    )
    coordinator = GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.2,
    )
    await coordinator.shutdown()

    assert source.token.reason is CancellationReason.CLIENT_DISCONNECTED
