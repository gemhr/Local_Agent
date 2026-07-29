from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from core.runtime import (
    AgentState,
    AgentStateMachine,
    BudgetLedger,
    EventChannelClosedError,
    InMemoryRunEventJournal,
    InMemorySpanRecorder,
    JournalError,
    JournalErrorCode,
    OutputDeltaPayload,
    ParallelExecutionPolicy,
    ParallelExecutor,
    Plan,
    PlanSource,
    PlanStep,
    RunBudget,
    RunCoordinator,
    RunCompletedPayload,
    RunEventEmitter,
    RunHandle,
    RunRegistry,
    RunStatus,
    RunStartedPayload,
    RuntimeEventChannel,
    RuntimeEventDraft,
    RuntimeEventType,
    SerialScheduler,
    StepExecutionMode,
    StepStatus,
    StopReason,
    TaskCapabilityRequirements,
    create_run_context,
)


def draft(index: int = 1) -> RuntimeEventDraft:
    return RuntimeEventDraft(
        run_id="run-a",
        trace_id="trace-a",
        event_type=RuntimeEventType.RUN_STARTED,
        component="test",
        payload=RunStartedPayload(f"RUNNING-{index}"),
    )


class RecordingJournal(InMemoryRunEventJournal):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, int]] = []

    def append(self, event):
        self.calls.append((event.event_id, event.sequence))
        return super().append(event)


class FailingJournal(InMemoryRunEventJournal):
    def append(self, event):
        raise JournalError(
            JournalErrorCode.JOURNAL_APPEND_FAILED,
            "测试 Journal 追加失败",
        )


class StateAssertingJournal(InMemoryRunEventJournal):
    def __init__(self, state: AgentState) -> None:
        super().__init__()
        self.state = state
        self.observations: list[tuple[RuntimeEventType, str]] = []

    def append(self, event):
        if event.event_type is RuntimeEventType.RUN_STARTED:
            assert self.state.status is RunStatus.RUNNING
            observed = self.state.status.value
        elif event.event_type is RuntimeEventType.STEP_STARTED:
            assert self.state.steps["answer"].status is StepStatus.RUNNING
            observed = self.state.steps["answer"].status.value
        elif event.event_type is RuntimeEventType.STEP_COMPLETED:
            assert self.state.steps["answer"].status is StepStatus.SUCCEEDED
            observed = self.state.steps["answer"].status.value
        elif event.event_type is RuntimeEventType.RUN_COMPLETED:
            assert self.state.status is RunStatus.SUCCEEDED
            assert self.state.stop_reason is StopReason.COMPLETED
            observed = self.state.status.value
        else:
            observed = "IGNORED"
        self.observations.append((event.event_type, observed))
        return super().append(event)


@pytest.mark.asyncio
async def test_channel_uses_single_sequence_owner_and_journals_before_enqueue():
    journal = RecordingJournal()
    channel = RuntimeEventChannel(4, run_id="run-a", journal=journal)
    value = await channel.publish(draft())
    assert journal.calls == [(value.event_id, 1)]
    assert journal.last_sequence("run-a") == value.sequence == 1
    iterator = channel.__aiter__()
    assert await anext(iterator) is value
    await channel.abort()


@pytest.mark.asyncio
async def test_journal_failure_does_not_enqueue():
    channel = RuntimeEventChannel(4, run_id="run-a", journal=FailingJournal())
    with pytest.raises(JournalError) as exc:
        await channel.publish(draft())
    assert exc.value.error_code is JournalErrorCode.JOURNAL_APPEND_FAILED
    assert channel.buffered_count == 0


@pytest.mark.asyncio
async def test_channel_abort_after_journal_keeps_record_and_consumes_sequence():
    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(1, run_id="run-a", journal=journal)
    first = await channel.publish(draft(1))
    blocked = asyncio.create_task(channel.publish(draft(2)))
    await asyncio.sleep(0)
    assert journal.last_sequence("run-a") == 2
    await channel.abort()
    with pytest.raises(EventChannelClosedError):
        await blocked
    records = journal.read_after("run-a", 0, 10)
    assert [item.sequence for item in records] == [1, 2]
    assert records[0].event_id == first.event_id


@pytest.mark.asyncio
async def test_channel_failure_does_not_repeat_committed_business_work():
    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(1, run_id="run-a", journal=journal)
    await channel.publish(draft(1))
    applied = 0

    async def commit_then_publish():
        nonlocal applied
        applied += 1
        await channel.publish(draft(2))

    task = asyncio.create_task(commit_then_publish())
    await asyncio.sleep(0)
    await channel.abort()
    with pytest.raises(EventChannelClosedError):
        await task
    assert applied == 1
    assert journal.last_sequence("run-a") == 2


@pytest.mark.asyncio
async def test_all_coordinated_event_families_use_the_same_journal_path():
    from core.runtime import (
        ModelCompletedPayload,
        RetrievalBudgetPayload,
        RetrievalCompletedPayload,
        StepCompletedPayload,
        StepStartedPayload,
        ToolCompletedPayload,
    )

    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(16, run_id="run-a", journal=journal)
    emitter = RunEventEmitter(
        run_id="run-a", trace_id="trace-a", channel=channel
    )
    step = emitter.for_step("answer")
    await emitter.emit(
        RuntimeEventType.RUN_STARTED,
        RunStartedPayload("RUNNING"),
        component="coordinator",
    )
    await step.emit(
        RuntimeEventType.STEP_STARTED,
        StepStartedPayload("RUNNING"),
        component="scheduler",
    )
    await step.emit(
        RuntimeEventType.MODEL_COMPLETED,
        ModelCompletedPayload("profile", 0, 0, True),
        component="model",
    )
    await step.emit(
        RuntimeEventType.TOOL_COMPLETED,
        ToolCompletedPayload("tool", True),
        component="tool",
    )
    await step.emit(
        RuntimeEventType.RETRIEVAL_COMPLETED,
        RetrievalCompletedPayload(
            "retrieval",
            "SUCCEEDED",
            1,
            1,
            1,
            False,
            RetrievalBudgetPayload(retrieval_calls=1),
        ),
        component="retrieval",
    )
    await step.emit(
        RuntimeEventType.OUTPUT_DELTA,
        OutputDeltaPayload("private answer"),
        component="driver",
    )
    await step.emit(
        RuntimeEventType.STEP_COMPLETED,
        StepCompletedPayload("SUCCEEDED"),
        component="executor",
        close=True,
    )
    await emitter.emit(
        RuntimeEventType.RUN_COMPLETED,
        RunCompletedPayload("SUCCEEDED", "COMPLETED"),
        component="coordinator",
    )
    records = journal.read_after("run-a", 0, 20)
    assert [item.sequence for item in records] == list(range(1, 9))
    assert records[-1].event_type is RuntimeEventType.RUN_COMPLETED
    output = next(
        item for item in records if item.event_type is RuntimeEventType.OUTPUT_DELTA
    )
    assert "private answer" not in str(output.safe_payload)


@pytest.mark.asyncio
async def test_real_coordinator_commits_state_before_journal():
    context, source = create_run_context(entry_agent_id="test-agent")
    ledger = BudgetLedger(
        RunBudget(), deadline_remaining=context.remaining_seconds
    )
    context.attach_budget_ledger(ledger)
    state = AgentState.for_run_context(context.run_id)
    machine = AgentStateMachine()
    plan = Plan(
        plan_id="journal-integration",
        version=1,
        task_summary="安全测试",
        steps=(
            PlanStep(
                "answer",
                "answer",
                "安全步骤",
                (),
                "完成",
                "test-agent",
                TaskCapabilityRequirements(),
            ),
        ),
        created_at=datetime.now(UTC),
        source=PlanSource.DETERMINISTIC,
    )
    journal = StateAssertingJournal(state)
    channel = RuntimeEventChannel(
        16, run_id=context.run_id, journal=journal
    )
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    )
    recorder = InMemorySpanRecorder()
    coordinator = RunCoordinator(
        run_context=context,
        plan=plan,
        agent_state=state,
        budget_ledger=ledger,
        run_handle=RunHandle(
            context.run_id, source, state, "run_coordinator"
        ),
        scheduler=SerialScheduler(machine),
        executor=ParallelExecutor(
            machine, max_concurrency=1, event_emitter=emitter
        ),
        run_registry=RunRegistry(),
        policy=ParallelExecutionPolicy(max_concurrency=1),
        state_machine=machine,
        event_emitter=emitter,
        span_recorder=recorder,
    )

    class Driver:
        async def execute(self, claim, run_context):
            return claim.step_id

    result = await coordinator.execute(
        driver=Driver(), execution_mode=StepExecutionMode.ASYNC
    )
    assert result.status is RunStatus.SUCCEEDED
    assert journal.observations == [
        (RuntimeEventType.RUN_STARTED, "RUNNING"),
        (RuntimeEventType.STEP_STARTED, "RUNNING"),
        (RuntimeEventType.STEP_COMPLETED, "SUCCEEDED"),
        (RuntimeEventType.RUN_COMPLETED, "SUCCEEDED"),
    ]
    records = journal.read_after(context.run_id, 0, 10)
    assert records[-1].safe_payload["status"] == "SUCCEEDED"
    assert records[-1].safe_payload["stop_reason"] == "COMPLETED"
    assert isinstance(records[-1].safe_payload["duration_ms"], int)
    assert records[-1].safe_payload["duration_ms"] >= 0
    run_span = next(item for item in recorder.snapshot() if item.component == "runtime")
    step_span = next(item for item in recorder.snapshot() if item.component == "step")
    run_events = [
        item
        for item in records
        if item.event_type in {
            RuntimeEventType.RUN_STARTED,
            RuntimeEventType.RUN_COMPLETED,
        }
    ]
    step_events = [
        item
        for item in records
        if item.event_type in {
            RuntimeEventType.STEP_STARTED,
            RuntimeEventType.STEP_COMPLETED,
        }
    ]
    assert {item.span_id for item in run_events} == {run_span.span_id}
    assert {item.span_id for item in step_events} == {step_span.span_id}
    assert {item.parent_span_id for item in step_events} == {run_span.span_id}
