from __future__ import annotations

import pytest

from core.runtime import (
    CoordinatedRuntimeFactory,
    EventPublicationError,
    FaultPoint,
    InMemoryRunEventJournal,
    RunCoordinatorError,
    RunStatus,
    RuntimeEventType,
    RuntimeEventChannel,
)
from tests._event_fault_fixtures import event_controller, run_started_draft
from tests._runtime_assembly_fixtures import FakeRouter, make_services


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "point",
    [
        FaultPoint.EVENT_AFTER_JOURNAL_APPEND,
        FaultPoint.EVENT_BEFORE_CHANNEL_ENQUEUE,
    ],
)
async def test_partial_publication_retains_record_consumes_sequence_and_never_replays(point):
    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(
        4,
        run_id="run-a",
        journal=journal,
        fault_controller=event_controller(point),
    )
    with pytest.raises(EventPublicationError) as captured:
        await channel.publish(run_started_draft())

    failed_event = captured.value.evidence
    assert captured.value.partially_persisted is True
    assert journal.last_sequence("run-a") == failed_event.sequence == 1
    assert channel.buffered_count == 0

    next_event = await channel.publish(run_started_draft())
    assert next_event.sequence == 2
    assert journal.last_sequence("run-a") == 2
    assert channel.buffered_count == 1
    iterator = channel.__aiter__()
    assert await anext(iterator) is next_event
    await iterator.aclose()
    await channel.abort()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "point",
    [
        FaultPoint.JOURNAL_BEFORE_TERMINAL_APPEND,
        FaultPoint.EVENT_AFTER_JOURNAL_APPEND,
        FaultPoint.EVENT_BEFORE_CHANNEL_ENQUEUE,
    ],
)
async def test_terminal_publication_fault_keeps_authoritative_state_and_cleanup(point):
    journal = InMemoryRunEventJournal()
    services = make_services(journal=journal, snapshot_enabled=False)
    factory = CoordinatedRuntimeFactory(FakeRouter(), services)
    scope = await factory.create_run_scope(
        "core_router",
        "query",
        fault_controller=event_controller(
            point, event_type=RuntimeEventType.RUN_COMPLETED
        ),
    )

    with pytest.raises(RunCoordinatorError) as captured:
        await scope.execute()

    assert captured.value.error_code == "RUNTIME_TERMINAL_PUBLICATION_FAILED"
    assert scope.agent_state.status is RunStatus.SUCCEEDED
    assert services.run_registry.get(scope.run_id) is None
    terminal_records = [
        item
        for item in journal.read_after(scope.run_id, 0, 100)
        if item.event_type is RuntimeEventType.RUN_COMPLETED
    ]
    expected_journal_count = (
        0 if point is FaultPoint.JOURNAL_BEFORE_TERMINAL_APPEND else 1
    )
    assert len(terminal_records) == expected_journal_count

    await scope.close()
    channel_events = [item async for item in scope.event_channel]
    assert sum(
        item.event_type is RuntimeEventType.RUN_COMPLETED
        for item in channel_events
    ) == 0
    assert scope.event_channel.publications_in_flight == 0


@pytest.mark.asyncio
async def test_run_scoped_controller_isolation_does_not_close_normal_run_channel():
    journal = InMemoryRunEventJournal()
    services = make_services(journal=journal, snapshot_enabled=False)
    factory = CoordinatedRuntimeFactory(FakeRouter(), services)
    failed_scope = await factory.create_run_scope(
        "core_router",
        "query-a",
        fault_controller=event_controller(
            FaultPoint.EVENT_BEFORE_JOURNAL_APPEND,
            event_type=RuntimeEventType.RUN_STARTED,
        ),
    )
    normal_scope = await factory.create_run_scope("core_router", "query-b")

    failed_result = await failed_scope.execute()
    normal_result = await normal_scope.execute()
    assert failed_result.status is RunStatus.FAILED
    assert normal_result.status is RunStatus.SUCCEEDED
    assert failed_scope.event_channel is not normal_scope.event_channel
    assert failed_scope.fault_controller is not normal_scope.fault_controller
    assert normal_scope.event_channel.is_closed is False

    await failed_scope.close()
    await normal_scope.close()
    normal_events = [item async for item in normal_scope.event_channel]
    assert [item.sequence for item in normal_events] == list(
        range(1, len(normal_events) + 1)
    )
    assert sum(
        item.event_type is RuntimeEventType.RUN_COMPLETED
        for item in normal_events
    ) == 1
