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
async def test_specific_model_fault_reserves_shared_identity_from_alias():
    calls: list[str] = []
    shared = RecordingResource("shared", calls)
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
        services_for_components(
            journal=RecordingResource("journal", calls),
            extra=(("model_engine_0", shared), ("remaining_store", shared)),
        ),
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    counters = {item.rule_id: item for item in controller.snapshot().counters}
    assert counters["model-specific"].hit_count == 1
    assert counters["remaining-generic"].match_count == 0
    assert shared.close_calls == 0
    assert report.has_deferred_resources is False
    assert report.fully_closed is False
