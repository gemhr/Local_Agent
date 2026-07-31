from __future__ import annotations

import asyncio

import pytest

from core.runtime import (
    FaultAction,
    ControllableFaultSleeper,
    FaultInjectionScope,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    OperationIdempotency,
    RunEventEmitter,
    RunCancelledError,
    RuntimeEventChannel,
    RuntimeEventType,
    ToolExecutionService,
    ToolExecutionError,
    ToolSideEffectState,
)
from tests.tool_fault_test_support import (
    NOW,
    PhaseAwareToolAdapter,
    make_context,
)


def blocking_rule(point: FaultPoint) -> FaultRule:
    return FaultRule(
        rule_id="boundary-block",
        fault_point=point,
        action=FaultAction.BLOCK_UNTIL_RELEASED,
        trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.ATTEMPT_SCOPE,
        max_hits=1,
        component="tool",
        dangerous_window=True,
    )


def delay_rule(point: FaultPoint, seconds: float = 0.5) -> FaultRule:
    return FaultRule(
        rule_id="boundary-delay",
        fault_point=point,
        action=FaultAction.DELAY,
        trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.ATTEMPT_SCOPE,
        max_hits=1,
        component="tool",
        delay_seconds=seconds,
        dangerous_window=True,
    )


def event_emitter(context):
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
    return channel, emitter


@pytest.mark.asyncio
async def test_cancellation_at_before_commit_keeps_not_started_and_cleans_resources():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    context, source = make_context()
    service = ToolExecutionService()
    channel, emitter = event_emitter(context)
    async with FaultInjectionScope(
        FaultPlan(
            "before-commit-cancel",
            (blocking_rule(FaultPoint.TOOL_BEFORE_SIDE_EFFECT_COMMIT),),
            created_at=NOW,
        )
    ) as scope:
        task = asyncio.create_task(
            service.execute(
                invocation=adapter.build_invocation(),
                adapter=adapter,
                run_context=context,
                step_id="step",
                event_emitter=emitter,
                fault_controller=scope.controller,
            )
        )
        await asyncio.wait_for(
            scope.blocker("boundary-block").entered.wait(), 1
        )
        source.cancel()
        with pytest.raises(RunCancelledError):
            await task
    await channel.close()
    events = [event async for event in channel]

    assert adapter.provider_entered_count == 1
    assert adapter.before_side_effect_called_count == 1
    assert adapter.side_effect_marker_committed_count == 0
    assert adapter.external_effect_applied_count == 0
    assert adapter.compensation_called_count == 0
    assert context.budget_ledger.snapshot().committed_usage.tool_calls == 1
    assert context.budget_ledger.snapshot().active_reservation_count == 0
    assert service.concurrency_controller.worker_snapshot()["active_worker_count"] == 0
    assert [event.event_type for event in events] == [
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
    ]
    assert events[-1].payload.provider_started is True
    assert events[-1].payload.side_effect_state == "NOT_STARTED"


@pytest.mark.asyncio
async def test_cancellation_after_provider_return_preserves_committed_fact():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    context, source = make_context()
    service = ToolExecutionService()
    channel, emitter = event_emitter(context)
    async with FaultInjectionScope(
        FaultPlan(
            "after-return-cancel",
            (blocking_rule(FaultPoint.TOOL_AFTER_PROVIDER_RETURN),),
            created_at=NOW,
        )
    ) as scope:
        task = asyncio.create_task(
            service.execute(
                invocation=adapter.build_invocation(),
                adapter=adapter,
                run_context=context,
                step_id="step",
                event_emitter=emitter,
                fault_controller=scope.controller,
            )
        )
        await asyncio.wait_for(
            scope.blocker("boundary-block").entered.wait(), 1
        )
        source.cancel()
        with pytest.raises(RunCancelledError):
            await task
    await channel.close()
    events = [event async for event in channel]

    assert adapter.provider_entered_count == 1
    assert adapter.provider_returned_count == 1
    assert adapter.external_effect_applied_count == 1
    assert adapter.compensation_called_count == 0
    assert context.budget_ledger.snapshot().committed_usage.tool_calls == 1
    assert context.budget_ledger.snapshot().active_reservation_count == 0
    assert service.concurrency_controller.worker_snapshot()["active_worker_count"] == 0
    assert [event.event_type for event in events] == [
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
    ]
    assert events[-1].payload.provider_started is True
    assert events[-1].payload.side_effect_state == "COMMITTED"


@pytest.mark.asyncio
async def test_controller_close_cannot_change_authoritative_committed_state():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT,
        response_state=ToolSideEffectState.COMMITTED,
    )
    context, _ = make_context()
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation(),
        adapter=adapter,
        run_context=context,
        step_id="step",
    )
    assert result.side_effect_state is ToolSideEffectState.COMMITTED
    assert adapter.external_effect_applied_count == 1
    assert adapter.compensation_called_count == 0


@pytest.mark.asyncio
async def test_before_commit_delay_is_cancellable_without_applying_effect():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    context, source = make_context()
    async with FaultInjectionScope(
        FaultPlan(
            "before-delay-cancel",
            (delay_rule(FaultPoint.TOOL_BEFORE_SIDE_EFFECT_COMMIT),),
            created_at=NOW,
        )
    ) as scope:
        task = asyncio.create_task(
            ToolExecutionService().execute(
                invocation=adapter.build_invocation(),
                adapter=adapter,
                run_context=context,
                step_id="step",
                fault_controller=scope.controller,
            )
        )
        for _ in range(100):
            if scope.controller.snapshot().counters[0].hit_count == 1:
                break
            await asyncio.sleep(0.005)
        assert scope.controller.snapshot().counters[0].hit_count == 1
        source.cancel()
        with pytest.raises(RunCancelledError):
            await task
    assert adapter.provider_entered_count == 1
    assert adapter.external_effect_applied_count == 0
    assert adapter.compensation_called_count == 0


@pytest.mark.asyncio
async def test_after_return_delay_is_cancellable_and_preserves_committed_effect():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    context, source = make_context()
    sleeper = ControllableFaultSleeper()
    async with FaultInjectionScope(
        FaultPlan(
            "after-delay-cancel",
            (delay_rule(FaultPoint.TOOL_AFTER_PROVIDER_RETURN),),
            created_at=NOW,
        ),
        sleeper=sleeper,
    ) as scope:
        task = asyncio.create_task(
            ToolExecutionService().execute(
                invocation=adapter.build_invocation(),
                adapter=adapter,
                run_context=context,
                step_id="step",
                fault_controller=scope.controller,
            )
        )
        await asyncio.wait_for(sleeper.entered.wait(), 1)
        source.cancel()
        with pytest.raises(RunCancelledError):
            await task
    assert adapter.provider_returned_count == 1
    assert adapter.external_effect_applied_count == 1
    assert adapter.compensation_called_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("point", "expected_state", "expected_effects"),
    [
        (
            FaultPoint.TOOL_BEFORE_SIDE_EFFECT_COMMIT,
            ToolSideEffectState.NOT_STARTED,
            0,
        ),
        (
            FaultPoint.TOOL_AFTER_PROVIDER_RETURN,
            ToolSideEffectState.COMMITTED,
            1,
        ),
    ],
)
async def test_boundary_delay_honors_attempt_deadline(
    point, expected_state, expected_effects
):
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    context, _ = make_context()
    async with FaultInjectionScope(
        FaultPlan(
            "boundary-delay-deadline",
            (delay_rule(point, seconds=0.2),),
            created_at=NOW,
        )
    ) as scope:
        result = await ToolExecutionService().execute(
            invocation=adapter.build_invocation(
                requested_timeout_seconds=0.02
            ),
            adapter=adapter,
            run_context=context,
            step_id="step",
            fault_controller=scope.controller,
        )
    assert isinstance(result, ToolExecutionError)
    assert result.provider_started is True
    assert result.side_effect_state is expected_state
    assert adapter.external_effect_applied_count == expected_effects
    assert adapter.compensation_called_count == 0
