from __future__ import annotations

import pytest

from core.runtime import (
    EventPublicationError,
    InMemoryRunEventJournal,
    ModelCompletedPayload,
    OutputDeltaPayload,
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
@pytest.mark.parametrize(
    "draft", family_drafts()[:-1], ids=lambda item: item.event_type.value
)
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
    assert error.evidence.sequence == 1
    assert error.evidence.event_type == draft.event_type.value
    assert not hasattr(error, "event")
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


@pytest.mark.asyncio
async def test_terminal_uses_only_terminal_specific_pre_append_seam():
    from datetime import UTC, datetime

    from core.runtime import (
        FaultAction,
        FaultInjectionController,
        FaultPlan,
        FaultRule,
        FaultScope,
        FaultTrigger,
        InjectedFaultCode,
    )

    rules = tuple(
        FaultRule(
            rule_id=point.value,
            fault_point=point,
            action=FaultAction.RAISE_TYPED_ERROR,
            trigger=FaultTrigger.ALWAYS,
            scope=FaultScope.RUN_SCOPE,
            max_hits=1,
            component="event_channel",
            safe_fault_code=InjectedFaultCode.INJECTED_JOURNAL_FAILURE,
        )
        for point in (
            FaultPoint.EVENT_BEFORE_JOURNAL_APPEND,
            FaultPoint.JOURNAL_BEFORE_TERMINAL_APPEND,
        )
    )
    controller = FaultInjectionController(
        FaultPlan("terminal-seam", rules, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(
        2, run_id="run-a", journal=journal, fault_controller=controller
    )
    terminal = family_drafts()[-1]
    with pytest.raises(EventPublicationError):
        await channel.publish(terminal)
    counters = {item.rule_id: item for item in controller.snapshot().counters}
    assert counters[FaultPoint.EVENT_BEFORE_JOURNAL_APPEND.value].match_count == 0
    assert counters[FaultPoint.JOURNAL_BEFORE_TERMINAL_APPEND.value].hit_count == 1


@pytest.mark.asyncio
async def test_disabled_dual_pre_append_rules_execute_neither_physical_seam():
    from datetime import UTC, datetime

    from core.runtime import (
        FaultAction,
        FaultInjectionController,
        FaultPlan,
        FaultRule,
        FaultScope,
        FaultTrigger,
        InjectedFaultCode,
    )

    rules = tuple(
        FaultRule(
            rule_id=point.value,
            fault_point=point,
            action=FaultAction.RAISE_TYPED_ERROR,
            trigger=FaultTrigger.ALWAYS,
            scope=FaultScope.RUN_SCOPE,
            max_hits=1,
            component="event_channel",
            safe_fault_code=InjectedFaultCode.INJECTED_JOURNAL_FAILURE,
        )
        for point in (
            FaultPoint.EVENT_BEFORE_JOURNAL_APPEND,
            FaultPoint.JOURNAL_BEFORE_TERMINAL_APPEND,
        )
    )
    controller = FaultInjectionController(
        FaultPlan("disabled-terminal-seam", rules, created_at=datetime(2026, 1, 1, tzinfo=UTC)),
        enabled=False,
    )
    channel = RuntimeEventChannel(
        2,
        run_id="run-a",
        journal=InMemoryRunEventJournal(),
        fault_controller=controller,
    )
    await channel.publish(family_drafts()[-1])
    counters = controller.snapshot().counters
    assert all((item.match_count, item.hit_count) == (0, 0) for item in counters)


@pytest.mark.asyncio
async def test_publication_error_exposes_no_runtime_event_or_payload_secret():
    secret = "MODEL_OUTPUT_SECRET"
    draft = RuntimeEventDraft(
        "run-a",
        "trace-a",
        RuntimeEventType.OUTPUT_DELTA,
        "model",
        OutputDeltaPayload(secret),
    )
    channel = RuntimeEventChannel(
        2,
        run_id="run-a",
        journal=InMemoryRunEventJournal(),
        fault_controller=event_controller(FaultPoint.EVENT_BEFORE_JOURNAL_APPEND),
    )
    with pytest.raises(EventPublicationError) as captured:
        await channel.publish(draft)
    error = captured.value
    assert not hasattr(error, "event")
    assert not hasattr(error.evidence, "payload")
    assert secret not in repr(error)
    assert secret not in repr(error.evidence)
