from __future__ import annotations

from dataclasses import replace

import pytest

from core.chat_service import ChatService
from core.runtime import CoordinatedRuntimeFactory, RuntimeEventType
from tests._runtime_assembly_fixtures import FakeRouter, make_services


@pytest.mark.asyncio
async def test_channel_is_single_sequence_owner_and_coordinator_emits_one_terminal() -> None:
    services = make_services(snapshot_enabled=False)
    router = FakeRouter()
    service = ChatService(
        router,
        coordinated_runtime_factory=CoordinatedRuntimeFactory(router, services),
        run_registry=services.run_registry,
    )

    events = [
        event
        async for event in service.stream_coordinated_agent_events(
            "agent-a", "question", persist=False
        )
    ]

    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert sum(
        event.event_type is RuntimeEventType.RUN_COMPLETED for event in events
    ) == 1
    assert events[-1].event_type is RuntimeEventType.RUN_COMPLETED


@pytest.mark.asyncio
async def test_application_close_deduplicates_shared_resource_by_identity() -> None:
    class SharedResource:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    shared = SharedResource()
    services = replace(
        make_services(snapshot_enabled=False),
        extra_closeables=(("shared_primary", shared), ("shared_alias", shared)),
    )

    await services.close(1)
    await services.close(1)

    assert shared.close_count == 1
