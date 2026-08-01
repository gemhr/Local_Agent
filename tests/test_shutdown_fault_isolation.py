from __future__ import annotations

from dataclasses import replace

import pytest

from core.runtime import (
    FaultPoint,
    GracefulShutdownCoordinator,
    RuntimeAdmissionState,
)
from tests._runtime_assembly_fixtures import make_services
from tests._shutdown_fault_fixtures import (
    RecordingResource,
    shutdown_controller,
    shutdown_rule,
)


def semantic_report(report):
    return (
        report.state,
        report.lifecycle_state,
        report.active_run_count,
        report.cancel_requested_count,
        report.cancelled_run_count,
        report.cancel_failed_count,
        report.gracefully_drained_count,
        report.forced_run_count,
        report.remaining_run_count,
        report.worker_drain_status,
        report.active_worker_count,
        report.detached_worker_count,
        report.unknown_worker_count,
        report.observability_flush_status,
        report.trace_flush_status,
        tuple(
            (item.component, item.status, item.error_code)
            for item in report.components
        ),
    )


@pytest.mark.asyncio
async def test_disabled_controller_has_shutdown_parity_and_zero_counters():
    calls_a: list[str] = []
    calls_b: list[str] = []
    services_a = replace(
        make_services(snapshot_enabled=False),
        event_journal=RecordingResource("journal", calls_a),
        extra_closeables=(("remaining_store", RecordingResource("remaining", calls_a)),),
    )
    services_b = replace(
        make_services(snapshot_enabled=False),
        event_journal=RecordingResource("journal", calls_b),
        extra_closeables=(("remaining_store", RecordingResource("remaining", calls_b)),),
    )
    disabled = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_COMPONENT_CLOSE,
            shutdown_component="remaining_store",
        ),
        enabled=False,
    )

    normal = await GracefulShutdownCoordinator(
        services_a,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown()
    with_disabled = await GracefulShutdownCoordinator(
        services_b,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(disabled)

    assert semantic_report(normal) == semantic_report(with_disabled)
    assert calls_a == calls_b
    counter = disabled.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (0, 0)


@pytest.mark.asyncio
async def test_faulted_shutdown_operation_does_not_pollute_new_container():
    calls_a: list[str] = []
    calls_b: list[str] = []
    remaining_a = RecordingResource("remaining", calls_a)
    remaining_b = RecordingResource("remaining", calls_b)
    services_a = replace(
        make_services(snapshot_enabled=False),
        extra_closeables=(("remaining_store", remaining_a),),
    )
    services_b = replace(
        make_services(snapshot_enabled=False),
        extra_closeables=(("remaining_store", remaining_b),),
    )
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_COMPONENT_CLOSE,
            shutdown_component="remaining_store",
        )
    )

    first = await GracefulShutdownCoordinator(
        services_a,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)
    controller.close()
    second = await GracefulShutdownCoordinator(
        services_b,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown()

    assert remaining_a.close_calls == 0
    assert remaining_b.close_calls == 1
    assert "RUNTIME_COMPONENT_CLOSE_INJECTED_FAILURE" in first.error_codes
    assert second.error_codes == ()
    assert second.state is RuntimeAdmissionState.CLOSED


@pytest.mark.asyncio
async def test_controller_close_does_not_close_runtime_components():
    calls: list[str] = []
    remaining = RecordingResource("remaining", calls)
    services = replace(
        make_services(snapshot_enabled=False),
        extra_closeables=(("remaining_store", remaining),),
    )
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_COMPONENT_CLOSE,
            shutdown_component="remaining_store",
        )
    )

    controller.close()
    assert remaining.close_calls == 0
    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    assert remaining.close_calls == 1
    assert report.error_codes == ()
