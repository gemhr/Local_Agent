from __future__ import annotations

import pytest

from core.chat_service import ChatService
from core.runtime import CoordinatedRuntimeFactory, RuntimeEventType, RunRegistry
from tests._runtime_assembly_fixtures import FakeRouter, make_services
from tests._runtime_invariants import (
    RuntimeInvariantReport,
    build_runtime_invariant_report,
)


@pytest.mark.asyncio
async def test_default_coordinated_request_satisfies_central_invariants():
    registry = RunRegistry()
    services = make_services(
        run_registry=registry,
        snapshot_enabled=False,
    )
    router = FakeRouter()
    factory = CoordinatedRuntimeFactory(router, services)
    service = ChatService(
        router,
        coordinated_runtime_factory=factory,
        run_registry=registry,
    )

    events = [
        event
        async for event in service.stream_coordinated_agent_events(
            "agent-a", "question", persist=False
        )
    ]
    report = build_runtime_invariant_report(
        events,
        active_registry_count=registry.observability_snapshot()[
            "active_runs"
        ],
        active_channel_count=len(
            services.observability_dispatcher.gauge_provider.channels
        ),
    )

    assert isinstance(report, RuntimeInvariantReport)
    report.assert_valid()
    assert sum(
        event.event_type is RuntimeEventType.OUTPUT_DELTA
        for event in events
    ) == 1


def test_invariant_report_is_derived_and_detects_second_owner():
    report = build_runtime_invariant_report(
        (),
        sequence_owner_count=2,
    )

    assert report.valid is False
    assert "sequence_owner_count" in report.violations
    assert not hasattr(report, "run_context")
    assert not hasattr(report, "event_channel")
