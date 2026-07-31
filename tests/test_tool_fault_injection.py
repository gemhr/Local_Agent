import asyncio

import pytest

from core.runtime import (
    FaultPoint,
    InjectedFaultCode,
    ToolErrorCategory,
    ToolExecutionError,
    ToolExecutionService,
    ToolExecutionStatus,
    RetryPolicy,
    FaultAction,
    FaultInjectionScope,
    FaultPlan,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InMemorySpanRecorder,
    RunCancelledError,
    RunEventEmitter,
    RuntimeEventChannel,
    RuntimeEventType,
)
from core.runtime.retry import RetryExecutor
from tests.tool_fault_test_support import (
    CountingToolAdapter,
    make_context,
    make_controller,
    zero_side_effects,
    NOW,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "point",
    [
        FaultPoint.TOOL_BEFORE_INVOCATION,
        FaultPoint.TOOL_BEFORE_ATTEMPT,
        FaultPoint.TOOL_BEFORE_PROVIDER_CALL,
    ],
)
async def test_all_tool_pre_call_points_have_zero_provider_side_effects(point):
    adapter = CountingToolAdapter()
    context, _ = make_context()
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation(),
        adapter=adapter,
        run_context=context,
        step_id="step",
        fault_controller=make_controller(point),
    )
    assert isinstance(result, ToolExecutionError)
    assert result.provider_started is False
    assert zero_side_effects(adapter) == (0, 0, 0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "category", "status"),
    [
        (InjectedFaultCode.INJECTED_TRANSIENT_FAILURE, ToolErrorCategory.TRANSIENT, ToolExecutionStatus.FAILED),
        (InjectedFaultCode.INJECTED_TIMEOUT, ToolErrorCategory.TIMEOUT, ToolExecutionStatus.TIMED_OUT),
        (InjectedFaultCode.INJECTED_PERMANENT_FAILURE, ToolErrorCategory.INTERNAL, ToolExecutionStatus.FAILED),
    ],
)
async def test_tool_fault_codes_map_to_safe_typed_errors(code, category, status):
    adapter = CountingToolAdapter()
    context, _ = make_context()
    service = ToolExecutionService(retry_executor=RetryExecutor(RetryPolicy(
        max_attempts=1, base_delay_seconds=0, max_delay_seconds=0
    )))
    result = await service.execute(
        invocation=adapter.build_invocation(), adapter=adapter,
        run_context=context, step_id="step",
        fault_controller=make_controller(FaultPoint.TOOL_BEFORE_PROVIDER_CALL, code),
    )
    assert result.category is category
    assert result.status is status
    assert "SECRET" not in str(result.to_safe_dict())
    assert "tool-fault" not in str(result.to_safe_dict())


@pytest.mark.asyncio
async def test_rate_limit_is_not_fabricated_in_tool_taxonomy():
    adapter = CountingToolAdapter()
    context, _ = make_context()
    service = ToolExecutionService(retry_executor=RetryExecutor(RetryPolicy(
        max_attempts=1, base_delay_seconds=0, max_delay_seconds=0
    )))
    result = await service.execute(
        invocation=adapter.build_invocation(), adapter=adapter,
        run_context=context, step_id="step",
        fault_controller=make_controller(
            FaultPoint.TOOL_BEFORE_PROVIDER_CALL,
            InjectedFaultCode.INJECTED_RATE_LIMIT,
        ),
    )
    assert result.category is ToolErrorCategory.INTERNAL
    assert result.safe_error_code == "TOOL_INJECTED_FAULT_UNSUPPORTED"
    assert result.retry_disposition.name == "UNSAFE"
    assert adapter.provider_call_count == 0


@pytest.mark.asyncio
async def test_provider_pre_call_fault_pairs_events_and_closes_all_spans():
    adapter = CountingToolAdapter()
    context, _ = make_context()
    channel = RuntimeEventChannel(
        8, run_id=context.run_id, cancellation_token=context.cancellation_token
    )
    emitter = RunEventEmitter(
        run_id=context.run_id, trace_id=context.trace_id, channel=channel
    ).for_step("step")
    recorder = InMemorySpanRecorder()
    result = await ToolExecutionService(span_recorder=recorder).execute(
        invocation=adapter.build_invocation(), adapter=adapter,
        run_context=context, step_id="step", event_emitter=emitter,
        fault_controller=make_controller(FaultPoint.TOOL_BEFORE_PROVIDER_CALL),
    )
    await channel.close()
    events = [event async for event in channel]
    assert [event.event_type for event in events] == [
        RuntimeEventType.TOOL_STARTED, RuntimeEventType.TOOL_COMPLETED
    ]
    assert events[-1].payload.provider_started is False
    assert result.provider_started is False
    assert recorder.health_snapshot().active_span_count == 0


@pytest.mark.asyncio
async def test_invocation_fault_creates_no_attempt_event_or_span():
    adapter = CountingToolAdapter()
    context, _ = make_context()
    channel = RuntimeEventChannel(
        8, run_id=context.run_id, cancellation_token=context.cancellation_token
    )
    emitter = RunEventEmitter(
        run_id=context.run_id, trace_id=context.trace_id, channel=channel
    ).for_step("step")
    recorder = InMemorySpanRecorder()
    await ToolExecutionService(span_recorder=recorder).execute(
        invocation=adapter.build_invocation(), adapter=adapter,
        run_context=context, step_id="step", event_emitter=emitter,
        fault_controller=make_controller(FaultPoint.TOOL_BEFORE_INVOCATION),
    )
    await channel.close()
    assert [event async for event in channel] == []
    assert [r.component for r in recorder.snapshot()] == ["tool_invocation"]
    assert recorder.health_snapshot().active_span_count == 0


@pytest.mark.asyncio
async def test_blocking_pre_call_fault_honors_run_cancellation_without_provider():
    rule = FaultRule(
        rule_id="tool-block", fault_point=FaultPoint.TOOL_BEFORE_PROVIDER_CALL,
        action=FaultAction.BLOCK_UNTIL_RELEASED, trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.ATTEMPT_SCOPE, max_hits=1, component="tool",
    )
    adapter = CountingToolAdapter(resource_key="RESOURCE_SECRET")
    context, source = make_context()
    service = ToolExecutionService()
    async with FaultInjectionScope(
        FaultPlan("tool-block-plan", (rule,), created_at=NOW)
    ) as scope:
        task = asyncio.create_task(service.execute(
            invocation=adapter.build_invocation(), adapter=adapter,
            run_context=context, step_id="step",
            fault_controller=scope.controller,
        ))
        await asyncio.wait_for(scope.blocker("tool-block").entered.wait(), 1)
        source.cancel()
        with pytest.raises(RunCancelledError):
            await task
    assert adapter.provider_call_count == 0
    assert not service.concurrency_controller.is_resource_held("RESOURCE_SECRET")
    assert context.budget_ledger.snapshot().active_reservation_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "point",
    [
        FaultPoint.TOOL_AFTER_SIDE_EFFECT_COMMIT,
        FaultPoint.TOOL_BEFORE_COMPLETION_EVENT,
    ],
)
async def test_b2b_dangerous_tool_points_are_not_invoked(point):
    rule = FaultRule(
        rule_id="dangerous", fault_point=point,
        action=FaultAction.RAISE_TYPED_ERROR, trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.ATTEMPT_SCOPE, max_hits=1, component="tool",
        safe_fault_code=InjectedFaultCode.INJECTED_PERMANENT_FAILURE,
        dangerous_window=True,
    )
    controller = make_controller(FaultPoint.TOOL_BEFORE_INVOCATION, enabled=False)
    controller = type(controller).for_test(
        FaultPlan("dangerous-plan", (rule,), created_at=NOW)
    )
    adapter = CountingToolAdapter()
    context, _ = make_context()
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation(), adapter=adapter,
        run_context=context, step_id="step", fault_controller=controller,
    )
    assert result.output.content == "ok"
    assert controller.snapshot().counters[0].match_count == 0
