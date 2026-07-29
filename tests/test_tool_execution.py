import asyncio
from dataclasses import replace
import threading
import time

import pytest

from core.runtime import (
    BudgetLedger,
    InMemorySpanRecorder,
    OperationIdempotency,
    RetryPolicy,
    RetryDisposition,
    RunCancelledError,
    RunBudget,
    ToolAdapter,
    ToolAdapterInvocationError,
    ToolAdapterResponse,
    ToolConcurrencyController,
    ToolErrorCategory,
    ToolExecutionError,
    ToolExecutionService,
    ToolExecutionSpec,
    ToolExecutionStatus,
    ToolInvocation,
    ToolSideEffectKind,
    ToolSideEffectState,
    create_run_context,
    RunEventEmitter,
    RuntimeEventChannel,
    RuntimeEventType,
    SpanStatus,
)
from core.runtime.retry import RetryExecutor


def make_context(*, budget=None, timeout=2.0):
    context, _ = create_run_context(
        entry_agent_id="test", timeout_seconds=timeout
    )
    context.attach_budget_ledger(BudgetLedger(budget or RunBudget()))
    return context


class ScriptedAdapter(ToolAdapter):
    def __init__(self, script, *, idempotency=OperationIdempotency.READ_ONLY):
        self.script = list(script)
        self.calls = []
        self.spec = ToolExecutionSpec(
            tool_name="scripted",
            side_effect_kind=(
                ToolSideEffectKind.NONE
                if idempotency == OperationIdempotency.READ_ONLY
                else ToolSideEffectKind.LOCAL_STATE_MUTATION
            ),
            idempotency=idempotency,
            default_timeout_seconds=1,
            max_output_bytes=8,
            max_concurrency=2,
        )

    def build_invocation(self, argument_text):
        return ToolInvocation.create(
            tool_name="scripted", arguments={"argument_text": argument_text}
        )

    def invoke_once(self, invocation, context):
        self.calls.append((invocation.invocation_id, context.attempt_id))
        action = self.script.pop(0)
        if isinstance(action, BaseException):
            raise action
        return ToolAdapterResponse(
            content=action,
            content_type="text/plain",
            safe_summary="done",
        )


@pytest.mark.asyncio
async def test_success_reserves_one_tool_call_and_limits_output():
    context = make_context(budget=RunBudget(max_tool_calls=1))
    adapter = ScriptedAdapter(["1234567890"])
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation("x"),
        adapter=adapter,
        run_context=context,
        step_id="step",
    )
    assert result.status == ToolExecutionStatus.SUCCEEDED
    assert result.output.content == "12345678"
    assert result.output.truncated
    assert context.budget_ledger.snapshot().committed_usage.tool_calls == 1


@pytest.mark.asyncio
async def test_read_only_transient_retry_keeps_invocation_and_changes_attempt():
    failure = ToolAdapterInvocationError(
        category=ToolErrorCategory.TRANSIENT,
        safe_error_code="TRANSIENT",
        safe_message="temporary",
    )
    adapter = ScriptedAdapter([failure, "ok"])
    context = make_context(
        budget=RunBudget(max_tool_calls=2, max_retries=1)
    )
    service = ToolExecutionService(
        retry_executor=RetryExecutor(
            RetryPolicy(
                max_attempts=2, base_delay_seconds=0, max_delay_seconds=0
            )
        )
    )
    invocation = adapter.build_invocation("x")
    result = await service.execute(
        invocation=invocation,
        adapter=adapter,
        run_context=context,
        step_id="step",
    )
    assert result.output.content == "ok"
    assert [item[0] for item in adapter.calls] == [
        invocation.invocation_id,
        invocation.invocation_id,
    ]
    assert adapter.calls[0][1] != adapter.calls[1][1]
    usage = context.budget_ledger.snapshot().committed_usage
    assert (usage.tool_calls, usage.retries) == (2, 1)


@pytest.mark.asyncio
async def test_trace_invocation_owns_retry_attempt_siblings():
    failure = ToolAdapterInvocationError(
        category=ToolErrorCategory.TRANSIENT,
        safe_error_code="TRANSIENT",
        safe_message="temporary",
    )
    adapter = ScriptedAdapter([failure, "ok"])
    context = make_context(budget=RunBudget(max_tool_calls=2, max_retries=1))
    recorder = InMemorySpanRecorder()
    service = ToolExecutionService(
        retry_executor=RetryExecutor(
            RetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0)
        ),
        span_recorder=recorder,
    )
    result = await service.execute(
        invocation=adapter.build_invocation("x"),
        adapter=adapter,
        run_context=context,
        step_id="step",
    )
    assert result.status is ToolExecutionStatus.SUCCEEDED
    records = recorder.snapshot()
    invocation = next(r for r in records if r.component == "tool_invocation")
    attempts = [r for r in records if r.component == "tool_attempt"]
    assert len(attempts) == 2
    assert {r.parent_span_id for r in attempts} == {invocation.span_id}
    assert len({r.span_id for r in attempts}) == 2
    assert [r.status for r in attempts] == [SpanStatus.ERROR, SpanStatus.OK]
    assert recorder.health_snapshot().active_span_count == 0


@pytest.mark.asyncio
async def test_non_idempotent_transient_failure_is_not_retried():
    failure = ToolAdapterInvocationError(
        category=ToolErrorCategory.TRANSIENT,
        safe_error_code="TRANSIENT",
        safe_message="temporary",
    )
    adapter = ScriptedAdapter(
        [failure, "must-not-run"],
        idempotency=OperationIdempotency.NON_IDEMPOTENT,
    )
    context = make_context()
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation("x"),
        adapter=adapter,
        run_context=context,
        step_id="step",
    )
    assert isinstance(result, ToolExecutionError)
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_budget_failure_does_not_enter_adapter():
    adapter = ScriptedAdapter(["must-not-run"])
    context = make_context(budget=RunBudget(max_tool_calls=0))
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation("x"),
        adapter=adapter,
        run_context=context,
        step_id="step",
    )
    assert isinstance(result, ToolExecutionError)
    assert result.category == ToolErrorCategory.BUDGET_EXHAUSTED
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_events_pair_only_after_resource_and_budget_and_hide_output():
    context = make_context(budget=RunBudget(max_tool_calls=1))
    channel = RuntimeEventChannel(
        8,
        run_id=context.run_id,
        cancellation_token=context.cancellation_token,
    )
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    ).for_step("step")
    adapter = ScriptedAdapter(["secret-output"])
    recorder = InMemorySpanRecorder()
    result = await ToolExecutionService(span_recorder=recorder).execute(
        invocation=adapter.build_invocation("secret-argument"),
        adapter=adapter,
        run_context=context,
        step_id="step",
        event_emitter=emitter,
    )
    assert result.status == ToolExecutionStatus.SUCCEEDED
    await channel.close()
    events = [event async for event in channel]
    assert [event.event_type for event in events] == [
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
    ]
    assert events[-1].payload.duration_ms == result.duration_ms
    attempt = next(
        record for record in recorder.snapshot() if record.component == "tool_attempt"
    )
    assert {event.span_id for event in events} == {attempt.span_id}
    assert {event.parent_span_id for event in events} == {attempt.parent_span_id}
    safe = str([event.to_safe_dict() for event in events])
    assert "secret-output" not in safe
    assert "secret-argument" not in safe


@pytest.mark.asyncio
async def test_budget_failure_publishes_no_tool_started_event():
    context = make_context(budget=RunBudget(max_tool_calls=0))
    channel = RuntimeEventChannel(
        8,
        run_id=context.run_id,
        cancellation_token=context.cancellation_token,
    )
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    ).for_step("step")
    adapter = ScriptedAdapter(["must-not-run"])
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation("x"),
        adapter=adapter,
        run_context=context,
        step_id="step",
        event_emitter=emitter,
    )
    assert isinstance(result, ToolExecutionError)
    await channel.close()
    assert [event async for event in channel] == []


class BlockingAdapter(ScriptedAdapter):
    def __init__(self, finished):
        super().__init__(["unused"])
        self.spec = replace(self.spec, default_timeout_seconds=0.02)
        self.finished = finished

    def invoke_once(self, invocation, context):
        self.calls.append((invocation.invocation_id, context.attempt_id))
        context.before_side_effect()
        time.sleep(0.12)
        self.finished.set()
        return ToolAdapterResponse("late", "text/plain", "late")


@pytest.mark.asyncio
async def test_sync_timeout_keeps_resource_until_worker_finishes():
    finished = threading.Event()
    adapter = BlockingAdapter(finished)
    adapter.spec = replace(
        adapter.spec,
        requires_resource_key=True,
        idempotency=OperationIdempotency.IDEMPOTENT,
    )
    invocation = ToolInvocation.create(
        tool_name="scripted", arguments={}, resource_key="shared"
    )
    context = make_context()
    recorder = InMemorySpanRecorder()
    service = ToolExecutionService(span_recorder=recorder)
    channel = RuntimeEventChannel(
        8,
        run_id=context.run_id,
        cancellation_token=context.cancellation_token,
    )
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    ).for_step("step")
    result = await service.execute(
        invocation=invocation,
        adapter=adapter,
        run_context=context,
        step_id="step",
        event_emitter=emitter,
    )
    assert isinstance(result, ToolExecutionError)
    assert result.category == ToolErrorCategory.TIMEOUT
    assert result.status == ToolExecutionStatus.TIMED_OUT
    assert result.side_effect_state == ToolSideEffectState.UNKNOWN
    assert result.retry_disposition == RetryDisposition.OUTCOME_UNKNOWN
    assert not result.worker_terminated
    assert result.execution_detached
    assert result.resource_release_pending
    assert len(adapter.calls) == 1
    assert service.concurrency_controller.is_resource_held("shared")
    snapshot = service.concurrency_controller.worker_snapshot()
    assert snapshot["active_worker_count"] == 1
    assert snapshot["detached_worker_count"] == 1
    assert recorder.health_snapshot().active_span_count == 0
    attempt_span = next(
        record for record in recorder.snapshot() if record.component == "tool_attempt"
    )
    assert attempt_span.status is SpanStatus.TIMED_OUT
    assert "late" not in str(snapshot)
    await asyncio.to_thread(finished.wait, 1)
    assert await asyncio.to_thread(
        service.concurrency_controller.wait_until_idle, 1
    )
    assert not service.concurrency_controller.is_resource_held("shared")
    await channel.close()
    events = [event async for event in channel]
    assert [event.event_type for event in events] == [
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
    ]
    completed = events[-1].payload
    assert not completed.worker_terminated
    assert completed.execution_detached
    assert completed.resource_release_pending
    safe = str(events[-1].to_safe_dict())
    assert "late" not in safe


class PermitGateAdapter(ToolAdapter):
    def __init__(
        self,
        *,
        tool_name,
        max_concurrency,
        timeout,
        blocking,
        release=None,
        started=None,
    ):
        self.spec = ToolExecutionSpec(
            tool_name=tool_name,
            side_effect_kind=ToolSideEffectKind.NONE,
            idempotency=OperationIdempotency.READ_ONLY,
            requires_resource_key=True,
            default_timeout_seconds=timeout,
            max_output_bytes=16,
            max_concurrency=max_concurrency,
        )
        self.blocking = blocking
        self.release = release
        self.started = started or threading.Event()

    def build_invocation(self, argument_text):
        return ToolInvocation.create(
            tool_name=self.spec.tool_name,
            arguments={"argument_text": argument_text},
            resource_key=argument_text,
        )

    def invoke_once(self, invocation, context):
        self.started.set()
        if self.blocking:
            self.release.wait(1)
        return ToolAdapterResponse("done", "text/plain", "done")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "global_max",
        "first_tool",
        "second_tool",
        "tool_max",
        "first_resource",
        "second_resource",
    ),
    [
        (1, "global-a", "global-b", 2, "resource-a", "resource-b"),
        (2, "same-tool", "same-tool", 1, "resource-a", "resource-b"),
        (2, "same-tool", "same-tool", 2, "shared-resource", "shared-resource"),
    ],
    ids=["global-permit", "per-tool-permit", "resource-permit"],
)
async def test_detached_worker_holds_every_permit_until_real_exit(
    global_max,
    first_tool,
    second_tool,
    tool_max,
    first_resource,
    second_resource,
):
    release = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()
    controller = ToolConcurrencyController(max_concurrency=global_max)
    service = ToolExecutionService(concurrency_controller=controller)
    first_adapter = PermitGateAdapter(
        tool_name=first_tool,
        max_concurrency=tool_max,
        timeout=0.02,
        blocking=True,
        release=release,
        started=first_started,
    )
    second_adapter = PermitGateAdapter(
        tool_name=second_tool,
        max_concurrency=tool_max,
        timeout=0.5,
        blocking=False,
        started=second_started,
    )
    first_invocation = ToolInvocation.create(
        tool_name=first_tool,
        arguments={"attempt": "first"},
        resource_key=first_resource,
    )
    second_invocation = ToolInvocation.create(
        tool_name=second_tool,
        arguments={"attempt": "second"},
        resource_key=second_resource,
    )

    first_result = await service.execute(
        invocation=first_invocation,
        adapter=first_adapter,
        run_context=make_context(timeout=1),
        step_id="first",
    )
    assert first_started.is_set()
    assert isinstance(first_result, ToolExecutionError)
    assert first_result.execution_detached

    second_task = asyncio.create_task(
        service.execute(
            invocation=second_invocation,
            adapter=second_adapter,
            run_context=make_context(timeout=1),
            step_id="second",
        )
    )
    await asyncio.sleep(0.03)
    assert not second_started.is_set()

    release.set()
    second_result = await asyncio.wait_for(second_task, 1)
    assert second_started.is_set()
    assert second_result.status == ToolExecutionStatus.SUCCEEDED
    assert await asyncio.to_thread(controller.wait_until_idle, 1)


class CheckpointFailureAdapter(ScriptedAdapter):
    def __init__(self, error):
        super().__init__(["unused"], idempotency=OperationIdempotency.NON_IDEMPOTENT)
        self.error = error

    def invoke_once(self, invocation, context):
        self.calls.append((invocation.invocation_id, context.attempt_id))
        context.before_side_effect()
        raise self.error


@pytest.mark.asyncio
async def test_started_then_unqualified_adapter_error_becomes_unknown():
    adapter = CheckpointFailureAdapter(RuntimeError("raw must not escape"))
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation("x"),
        adapter=adapter,
        run_context=make_context(),
        step_id="step",
    )
    assert isinstance(result, ToolExecutionError)
    assert result.side_effect_state == ToolSideEffectState.UNKNOWN
    assert result.retry_disposition == RetryDisposition.OUTCOME_UNKNOWN
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authoritative_state",
    [ToolSideEffectState.COMMITTED, ToolSideEffectState.COMPENSATED],
)
async def test_started_then_authoritative_adapter_error_keeps_explicit_state(
    authoritative_state,
):
    error = ToolAdapterInvocationError(
        category=ToolErrorCategory.VALIDATION,
        safe_error_code="AUTHORITATIVE_FAILURE",
        safe_message="safe",
        side_effect_state=authoritative_state,
        side_effect_state_authoritative=True,
        compensation_attempted=authoritative_state
        == ToolSideEffectState.COMPENSATED,
        compensation_succeeded=authoritative_state
        == ToolSideEffectState.COMPENSATED,
    )
    adapter = CheckpointFailureAdapter(error)
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation("x"),
        adapter=adapter,
        run_context=make_context(),
        step_id="step",
    )
    assert isinstance(result, ToolExecutionError)
    assert result.side_effect_state == authoritative_state


class AsyncCancellationAdapter(ScriptedAdapter):
    is_async = True

    def __init__(self):
        super().__init__(["unused"], idempotency=OperationIdempotency.NON_IDEMPOTENT)
        self.entered = asyncio.Event()

    async def invoke_once(self, invocation, context):
        context.before_side_effect()
        self.entered.set()
        await asyncio.sleep(10)
        return ToolAdapterResponse("never", "text/plain", "never")


@pytest.mark.asyncio
async def test_started_then_cancellation_emits_unknown_completed_state():
    context, source = create_run_context(
        entry_agent_id="test", timeout_seconds=2
    )
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    channel = RuntimeEventChannel(
        8,
        run_id=context.run_id,
        cancellation_token=context.cancellation_token,
    )
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    ).for_step("step")
    adapter = AsyncCancellationAdapter()
    task = asyncio.create_task(
        ToolExecutionService().execute(
            invocation=adapter.build_invocation("secret"),
            adapter=adapter,
            run_context=context,
            step_id="step",
            event_emitter=emitter,
        )
    )
    await adapter.entered.wait()
    source.cancel()
    with pytest.raises(RunCancelledError):
        await task
    await channel.close()
    events = [event async for event in channel]
    completed = events[-1].payload
    assert completed.side_effect_state == ToolSideEffectState.UNKNOWN.value
    assert completed.retry_disposition == RetryDisposition.OUTCOME_UNKNOWN.value
    assert "secret" not in str(events[-1].to_safe_dict())


class JsonAdapter(ScriptedAdapter):
    def __init__(self):
        super().__init__(['{"large":"value"}'])
        self.spec = replace(self.spec, max_output_bytes=4)

    def invoke_once(self, invocation, context):
        return ToolAdapterResponse(
            '{"large":"value"}',
            "application/json",
            "json",
        )


@pytest.mark.asyncio
async def test_oversized_json_returns_typed_error_instead_of_invalid_json():
    context = make_context()
    channel = RuntimeEventChannel(
        8,
        run_id=context.run_id,
        cancellation_token=context.cancellation_token,
    )
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    ).for_step("step")
    adapter = JsonAdapter()
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation("x"),
        adapter=adapter,
        run_context=context,
        step_id="step",
        event_emitter=emitter,
    )
    assert isinstance(result, ToolExecutionError)
    assert result.category == ToolErrorCategory.OUTPUT_TOO_LARGE
    assert result.safe_error_code == "TOOL_OUTPUT_TOO_LARGE"
    assert "content" not in result.partial_result
    await channel.close()
    events = [event async for event in channel]
    assert [event.event_type for event in events] == [
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
    ]
    assert '{"large":"value"}' not in str(events[-1].to_safe_dict())
