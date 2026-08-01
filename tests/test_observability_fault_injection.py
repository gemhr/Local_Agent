from __future__ import annotations

import asyncio

import pytest

from core.runtime import (
    CancellationSource,
    ControllableFaultSleeper,
    FaultAction,
    FaultPoint,
    InMemoryRunEventJournal,
    RuntimeEventChannel,
)
from tests._diagnostic_fault_fixtures import diagnostic_controller
from tests.test_event_fault_injection import family_drafts
from tests.test_observability_dispatcher import dispatcher


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "draft",
    family_drafts(),
    ids=lambda item: item.event_type.value,
)
async def test_record_fault_is_best_effort_after_journal_authority(draft):
    value, logger, metrics, *_ = dispatcher()
    journal = InMemoryRunEventJournal()
    controller = diagnostic_controller(
        FaultPoint.OBSERVABILITY_BEFORE_RECORD,
        component="observability_dispatcher",
        event_type=draft.event_type.value,
    )
    channel = RuntimeEventChannel(
        4,
        run_id=draft.run_id,
        journal=journal,
        observability_dispatcher=value,
        fault_controller=controller,
    )

    event = await channel.publish(draft)
    record = journal.read_after(draft.run_id, 0, 10)[0]
    record.verify()

    assert record.event_id == event.event_id
    assert record.sequence == event.sequence == 1
    assert channel.buffered_count == 1
    assert value.buffered_count == 0
    assert logger.records == ()
    metric_snapshot = metrics.snapshot()
    assert metric_snapshot.counters == {}
    assert metric_snapshot.gauges == {}
    assert metric_snapshot.histograms == {}
    health = value.health.snapshot()
    assert health.status == "DEGRADED"
    assert health.record_failures == 1
    assert health.last_safe_error_code == "OBSERVABILITY_RECORD_FAILED"
    counter = controller.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (1, 1)

    await channel.abort()
    assert await value.close()


@pytest.mark.asyncio
async def test_disabled_record_controller_matches_no_controller_output():
    record_draft = family_drafts()[0]
    first, first_logger, first_metrics, *_ = dispatcher()
    second, second_logger, second_metrics, *_ = dispatcher()
    controller = diagnostic_controller(
        FaultPoint.OBSERVABILITY_BEFORE_RECORD,
        component="observability_dispatcher",
        enabled=False,
    )
    from core.runtime import JournalRecord, RuntimeEvent

    event = RuntimeEvent.from_draft(record_draft, 1)
    record = JournalRecord.from_event(event)
    assert await first.submit(record)
    assert await second.submit(record, fault_controller=controller)
    assert await first.flush()
    assert await second.flush()

    assert first_logger.records == second_logger.records
    assert first_metrics.snapshot() == second_metrics.snapshot()
    assert first.health.snapshot() == second.health.snapshot()
    counter = controller.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (0, 0)
    assert await first.close()
    assert await second.close()


@pytest.mark.asyncio
async def test_run_a_record_fault_does_not_affect_run_b_on_shared_dispatcher():
    value, logger, *_ = dispatcher()
    journal = InMemoryRunEventJournal()
    controller_a = diagnostic_controller(
        FaultPoint.OBSERVABILITY_BEFORE_RECORD,
        component="observability_dispatcher",
    )
    channel_a = RuntimeEventChannel(
        4,
        run_id="run-a",
        journal=journal,
        observability_dispatcher=value,
        fault_controller=controller_a,
    )
    channel_b = RuntimeEventChannel(
        4,
        run_id="run-b",
        journal=journal,
        observability_dispatcher=value,
    )
    draft_a = family_drafts()[0]
    draft_b = type(draft_a)(
        "run-b",
        "trace-b",
        draft_a.event_type,
        draft_a.component,
        draft_a.payload,
    )

    await channel_a.publish(draft_a)
    controller_a.close()
    await channel_b.publish(draft_b)
    assert await value.flush()

    assert journal.last_sequence("run-a") == 1
    assert journal.last_sequence("run-b") == 1
    assert [item.run_id for item in logger.records] == ["run-b"]
    assert value.health.snapshot().record_failures == 1
    await channel_a.abort()
    await channel_b.abort()
    assert await value.close()


@pytest.mark.asyncio
async def test_record_delay_responds_to_run_cancellation_without_enqueue():
    value, *_ = dispatcher()
    sleeper = ControllableFaultSleeper()
    source = CancellationSource()
    controller = diagnostic_controller(
        FaultPoint.OBSERVABILITY_BEFORE_RECORD,
        component="observability_dispatcher",
        action=FaultAction.DELAY,
        sleeper=sleeper,
    )
    from core.runtime import JournalRecord, RuntimeEvent

    item = JournalRecord.from_event(RuntimeEvent.from_draft(family_drafts()[0], 1))
    task = asyncio.create_task(
        value.submit(
            item,
            fault_controller=controller,
            cancellation_token=source.token,
        )
    )
    await asyncio.wait_for(sleeper.entered.wait(), 1)
    source.cancel()
    assert await asyncio.wait_for(task, 1) is False
    assert value.buffered_count == 0
    assert value.health.snapshot().last_safe_error_code == (
        "OBSERVABILITY_RECORD_CANCELLED"
    )
    assert await value.close()
