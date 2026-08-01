from __future__ import annotations

import pytest

from core.runtime import FaultPoint, GracefulShutdownCoordinator
from tests._shutdown_fault_fixtures import (
    RecordingResource,
    shutdown_controller,
    shutdown_rule,
)
from tests.test_shutdown_component_fault import services_for_components


@pytest.mark.asyncio
async def test_model_specific_failure_cannot_fall_through_remaining_alias():
    calls: list[str] = []
    shared = RecordingResource("shared-model", calls)
    services = services_for_components(
        journal=RecordingResource("journal", calls),
        extra=(("model_engine_0", shared), ("remaining_store", shared)),
    )
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_BEFORE_MODEL_CLOSE,
            rule_id="model-specific",
            shutdown_component="model_engine_0",
        ),
        shutdown_rule(
            FaultPoint.SHUTDOWN_COMPONENT_CLOSE,
            rule_id="remaining-generic",
            shutdown_component="remaining_store",
        ),
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    counters = {item.rule_id: item for item in controller.snapshot().counters}
    assert counters["model-specific"].hit_count == 1
    assert counters["remaining-generic"].match_count == 0
    assert shared.close_calls == 0
    assert report.has_deferred_resources is False
    assert report.fully_closed is False


@pytest.mark.asyncio
async def test_journal_specific_failure_reserves_identity_from_generic_alias():
    calls: list[str] = []
    shared = RecordingResource("shared-journal", calls)
    services = services_for_components(
        journal=shared,
        extra=(("remaining_store", shared),),
    )
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_BEFORE_JOURNAL_CLOSE,
            rule_id="journal-specific",
            shutdown_component="event_journal",
        ),
        shutdown_rule(
            FaultPoint.SHUTDOWN_COMPONENT_CLOSE,
            rule_id="remaining-generic",
            shutdown_component="remaining_store",
        ),
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    counters = {item.rule_id: item for item in controller.snapshot().counters}
    assert counters["journal-specific"].hit_count == 1
    assert counters["remaining-generic"].match_count == 0
    assert shared.close_calls == 0
    assert report.fully_closed is False
