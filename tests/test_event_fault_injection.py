from __future__ import annotations

import pytest

from core.runtime import (
    EventPublicationError,
    InMemoryRunEventJournal,
    ModelCompletedPayload,
    RetrievalBudgetPayload,
    RetrievalCompletedPayload,
    RunCompletedPayload,
    RunStartedPayload,
    RuntimeEventChannel,
    RuntimeEventDraft,
    RuntimeEventType,
    StepStartedPayload,
    ToolCompletedPayload,
    FaultPoint,
)
from tests._event_fault_fixtures import event_controller


def family_drafts():
    return (
        RuntimeEventDraft(
            "run-a", "trace-a", RuntimeEventType.RUN_STARTED, "run", RunStartedPayload("RUNNING")
        ),
        RuntimeEventDraft(
            "run-a", "trace-a", RuntimeEventType.STEP_STARTED, "step", StepStartedPayload("RUNNING"), step_id="step", step_sequence=1
        ),
        RuntimeEventDraft(
            "run-a", "trace-a", RuntimeEventType.MODEL_COMPLETED, "model", ModelCompletedPayload("profile", 0, 0, True)
        ),
        RuntimeEventDraft(
            "run-a", "trace-a", RuntimeEventType.TOOL_COMPLETED, "tool", ToolCompletedPayload("tool", True)
        ),
        RuntimeEventDraft(
            "run-a", "trace-a", RuntimeEventType.RETRIEVAL_COMPLETED, "retrieval",
            RetrievalCompletedPayload("retrieval", "SUCCEEDED", 1, 1, 1, False, RetrievalBudgetPayload(retrieval_calls=1)),
        ),
        RuntimeEventDraft(
            "run-a", "trace-a", RuntimeEventType.RUN_COMPLETED, "run", RunCompletedPayload("SUCCEEDED", "COMPLETED")
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", family_drafts(), ids=lambda item: item.event_type.value)
async def test_before_append_fault_keeps_real_event_out_of_both_media(draft):
    journal = InMemoryRunEventJournal()
    controller = event_controller(
        FaultPoint.EVENT_BEFORE_JOURNAL_APPEND,
        event_type=draft.event_type,
    )
    channel = RuntimeEventChannel(
        4, run_id="run-a", journal=journal, fault_controller=controller
    )

    with pytest.raises(EventPublicationError) as captured:
        await channel.publish(draft)

    error = captured.value
    assert error.partially_persisted is False
    assert error.event.sequence == 1
    assert error.event.event_type is draft.event_type
    assert journal.read_after("run-a", 0, 10) == ()
    assert channel.buffered_count == 0
    assert controller.snapshot().counters[0].hit_count == 1


@pytest.mark.asyncio
async def test_disabled_controller_preserves_journal_channel_identity_and_order():
    journal = InMemoryRunEventJournal()
    controller = event_controller(
        FaultPoint.EVENT_BEFORE_JOURNAL_APPEND, enabled=False
    )
    channel = RuntimeEventChannel(
        4, run_id="run-a", journal=journal, fault_controller=controller
    )
    event = await channel.publish(family_drafts()[0])
    await channel.close()
    output = [item async for item in channel]
    record = journal.read_after("run-a", 0, 10)[0]

    assert output == [event]
    assert (record.event_id, record.sequence, record.event_type) == (
        event.event_id,
        event.sequence,
        event.event_type,
    )
    counter = controller.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (0, 0)
