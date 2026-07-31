from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from core.runtime import (
    FaultAction,
    FaultInjectionScope,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    OperationIdempotency,
    RunCancelledError,
    RunEventEmitter,
    RuntimeEventChannel,
    RuntimeEventType,
    ToolExecutionError,
    ToolExecutionService,
    ToolSideEffectState,
)
from tests.tool_fault_test_support import (
    PhaseAwareToolAdapter,
    make_context,
    make_controller,
)


async def _execute(adapter, controller=None):
    context, _ = make_context()
    channel = RuntimeEventChannel(
        8, run_id=context.run_id, cancellation_token=context.cancellation_token
    )
    emitter = RunEventEmitter(
        run_id=context.run_id, trace_id=context.trace_id, channel=channel
    ).for_step("step")
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation(),
        adapter=adapter,
        run_context=context,
        step_id="step",
        event_emitter=emitter,
        fault_controller=controller,
    )
    await channel.close()
    return result, [event async for event in channel], context


@pytest.mark.asyncio
async def test_exact_after_commit_point_remains_contract_only_without_owner_hook():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    controller = make_controller(FaultPoint.TOOL_AFTER_SIDE_EFFECT_COMMIT)

    result, events, _ = await _execute(adapter, controller)

    assert result.side_effect_state is ToolSideEffectState.COMMITTED
    assert adapter.provider_entered_count == 1
    assert adapter.external_effect_applied_count == 1
    assert adapter.provider_returned_count == 1
    assert [event.event_type for event in events] == [
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
    ]
    counter = controller.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (0, 0)


@pytest.mark.asyncio
async def test_authoritative_resolution_fault_is_after_return_and_preserves_commit():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    controller = make_controller(
        FaultPoint.TOOL_AFTER_AUTHORITATIVE_SIDE_EFFECT_RESOLUTION
    )

    result, events, context = await _execute(adapter, controller)

    assert isinstance(result, ToolExecutionError)
    assert result.safe_error_code == "TOOL_POST_PROVIDER_FAILURE"
    assert result.side_effect_state is ToolSideEffectState.COMMITTED
    assert adapter.provider_entered_count == 1
    assert adapter.external_effect_applied_count == 1
    assert adapter.provider_returned_count == 1
    assert adapter.compensation_called_count == 0
    assert [event.event_type for event in events] == [
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
    ]
    assert events[-1].payload.side_effect_state == "COMMITTED"
    usage = context.budget_ledger.snapshot().committed_usage
    assert (usage.tool_calls, usage.retries) == (1, 0)


@pytest.mark.asyncio
async def test_authoritative_resolution_delay_does_not_repeat_business_effect():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    controller = make_controller(
        FaultPoint.TOOL_AFTER_AUTHORITATIVE_SIDE_EFFECT_RESOLUTION,
        action=FaultAction.DELAY,
    )

    result, _, _ = await _execute(adapter, controller)

    assert result.output.content == "ok"
    assert result.side_effect_state is ToolSideEffectState.COMMITTED
    assert adapter.provider_entered_count == 1
    assert adapter.external_effect_applied_count == 1
    assert adapter.provider_returned_count == 1
    counter = controller.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (1, 1)


def _blocking_plan() -> FaultPlan:
    return FaultPlan(
        "post-commit-block-plan",
        (
            FaultRule(
                rule_id="post-commit-block",
                fault_point=(
                    FaultPoint.TOOL_AFTER_AUTHORITATIVE_SIDE_EFFECT_RESOLUTION
                ),
                action=FaultAction.BLOCK_UNTIL_RELEASED,
                trigger=FaultTrigger.ALWAYS,
                scope=FaultScope.ATTEMPT_SCOPE,
                max_hits=1,
                component="tool",
                dangerous_window=True,
            ),
        ),
        created_at=datetime(2026, 7, 31, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_run_cancellation_while_post_commit_blocked_preserves_commit():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    context, source = make_context()
    channel = RuntimeEventChannel(
        8, run_id=context.run_id, cancellation_token=context.cancellation_token
    )
    emitter = RunEventEmitter(
        run_id=context.run_id, trace_id=context.trace_id, channel=channel
    ).for_step("step")
    service = ToolExecutionService()
    async with FaultInjectionScope(_blocking_plan()) as scope:
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
        await asyncio.wait_for(scope.blocker("post-commit-block").entered.wait(), 1)
        source.cancel()
        with pytest.raises(RunCancelledError):
            await task
    await channel.close()
    events = [event async for event in channel]

    assert adapter.external_effect_applied_count == 1
    assert adapter.provider_entered_count == 1
    assert adapter.provider_returned_count == 1
    assert events[-1].payload.side_effect_state == "COMMITTED"
    assert context.budget_ledger.snapshot().committed_usage.tool_calls == 1
    assert service.concurrency_controller.active_worker_count == 0


@pytest.mark.asyncio
async def test_scope_close_releases_post_commit_block_without_state_rollback():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    context, _ = make_context()
    service = ToolExecutionService()
    scope = FaultInjectionScope(_blocking_plan())
    task = asyncio.create_task(
        service.execute(
            invocation=adapter.build_invocation(),
            adapter=adapter,
            run_context=context,
            step_id="step",
            fault_controller=scope.controller,
        )
    )
    await asyncio.wait_for(scope.blocker("post-commit-block").entered.wait(), 1)
    await scope.aclose()
    result = await task

    assert result.side_effect_state is ToolSideEffectState.COMMITTED
    assert adapter.external_effect_applied_count == 1
    assert adapter.provider_entered_count == 1
    assert service.concurrency_controller.active_worker_count == 0


@pytest.mark.asyncio
async def test_deadline_while_post_commit_blocked_preserves_commit():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    context, _ = make_context()
    invocation = adapter.build_invocation(requested_timeout_seconds=0.03)
    service = ToolExecutionService()
    async with FaultInjectionScope(_blocking_plan()) as scope:
        result = await service.execute(
            invocation=invocation,
            adapter=adapter,
            run_context=context,
            step_id="step",
            fault_controller=scope.controller,
        )

    assert isinstance(result, ToolExecutionError)
    assert result.side_effect_state is ToolSideEffectState.COMMITTED
    assert result.safe_error_code == "TOOL_DEADLINE_EXCEEDED"
    assert adapter.external_effect_applied_count == 1
    assert adapter.provider_entered_count == 1
    assert adapter.provider_returned_count == 1
    assert service.concurrency_controller.active_worker_count == 0
