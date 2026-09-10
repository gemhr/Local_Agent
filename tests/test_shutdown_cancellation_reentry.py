from __future__ import annotations

import asyncio

import pytest

from core.runtime import (
    FaultAction,
    FaultBlocker,
    FaultPoint,
    GracefulShutdownCoordinator,
    RuntimeAdmissionState,
    RuntimeLifecycleState,
)
from tests._shutdown_fault_fixtures import (
    RecordingResource,
    shutdown_controller,
    shutdown_rule,
)
from tests.test_shutdown_component_fault import services_for_components


@pytest.mark.asyncio
async def test_cancelled_shutdown_reentry_does_not_reclose_completed_components():
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
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_BEFORE_JOURNAL_CLOSE,
            rule_id="journal-block",
            shutdown_component="event_journal",
            action=FaultAction.BLOCK_UNTIL_RELEASED,
        ),
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
    assert report.fully_closed is True
    assert snapshot.close_calls == journal.close_calls == remaining.close_calls == 1
