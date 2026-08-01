from __future__ import annotations

from types import SimpleNamespace
import asyncio

import pytest

import core.runtime.tracing as tracing_module
from core.runtime import (
    CancellationReason,
    CancellationSource,
    FaultAction,
    FaultBlocker,
    FaultPoint,
    InMemorySpanRecorder,
    OperationScopedSpanRecorder,
    TraceOperationError,
)
from core.runtime.tracing import (
    activate_span,
    current_trace_context,
)
from tests._diagnostic_fault_fixtures import diagnostic_controller


def facade_for(point, *, component=None, enabled=True, cancellation_token=None):
    recorder = InMemorySpanRecorder()
    controller = diagnostic_controller(
        point,
        component=component,
        enabled=enabled,
    )
    facade = OperationScopedSpanRecorder(
        recorder,
        fault_controller=controller,
        cancellation_token=cancellation_token,
    )
    return recorder, controller, facade


def test_failed_child_start_preserves_nearest_real_parent():
    recorder, _, facade = facade_for(
        FaultPoint.TRACE_BEFORE_SPAN_START, component="planner"
    )
    root = facade.start_span(
        trace_id="trace",
        run_id="run",
        component="runtime",
        operation="run",
    )
    with activate_span(root):
        child = facade.start_span(
            trace_id="trace",
            run_id="run",
            component="planner",
            operation="plan",
        )
        assert child.context is None
        with activate_span(child):
            assert current_trace_context() == root.context
            grandchild = facade.start_span(
                trace_id="trace",
                run_id="run",
                component="model_invocation",
                operation="invoke",
            )
            grandchild.end_ok()
    assert current_trace_context() is None
    records = recorder.snapshot()
    grandchild_record = next(
        item for item in records if item.component == "model_invocation"
    )
    assert grandchild_record.parent_span_id == root.context.span_id
    assert recorder.health_snapshot().active_span_count == 0


def test_nested_span_end_fault_logically_closes_and_restores_context():
    recorder, _, facade = facade_for(
        FaultPoint.TRACE_BEFORE_SPAN_END, component="planner"
    )
    root = facade.start_span(
        trace_id="trace",
        run_id="run",
        component="runtime",
        operation="run",
    )
    with activate_span(root):
        child = facade.start_span(
            trace_id="trace",
            run_id="run",
            component="planner",
            operation="plan",
        )
        with activate_span(child):
            assert current_trace_context() == child.context
        assert child.ended
        assert current_trace_context() == root.context
    assert current_trace_context() is None
    health = recorder.health_snapshot()
    assert health.active_span_count == 0
    assert health.completed_span_count == 1
    assert health.dropped_span_count == 1
    assert health.end_failures == 1
    assert health.last_safe_error_code == "TRACE_SPAN_END_FAILED"


def test_span_end_cancellation_preserves_first_wins_reason_and_closes_span():
    source = CancellationSource()
    recorder, _, facade = facade_for(
        FaultPoint.TRACE_BEFORE_SPAN_END,
        component="tool_attempt",
        cancellation_token=source.token,
    )
    handle = facade.start_span(
        trace_id="trace",
        run_id="run",
        component="tool_attempt",
        operation="attempt",
    )
    source.cancel(CancellationReason.CLIENT_DISCONNECTED)
    handle.end_cancelled("RUN_CANCELLED")
    source.cancel(CancellationReason.SERVER_SHUTDOWN)

    assert source.token.reason is CancellationReason.CLIENT_DISCONNECTED
    assert handle.ended
    assert recorder.health_snapshot().active_span_count == 0
    assert recorder.health_snapshot().end_failures == 1


def test_trace_flush_fault_preserves_spans_and_later_raw_flush_succeeds():
    recorder, controller, facade = facade_for(
        FaultPoint.TRACE_BEFORE_FLUSH, component="trace_recorder"
    )
    handle = facade.start_span(
        trace_id="trace",
        run_id="run",
        component="runtime",
        operation="run",
    )
    handle.end_ok()
    before = recorder.snapshot()

    with pytest.raises(TraceOperationError) as captured:
        facade.flush(1)

    assert captured.value.error_code == "TRACE_FLUSH_FAILED"
    assert recorder.snapshot() == before
    assert recorder.health_snapshot().flush_failures == 1
    controller.close()
    assert recorder.flush(1) is None
    assert recorder.health_snapshot().active_span_count == 0


def test_disabled_controller_preserves_span_identity_hierarchy_and_health(monkeypatch):
    monkeypatch.setattr(
        tracing_module,
        "uuid4",
        lambda: SimpleNamespace(hex="fixed-span-id"),
    )
    normal_recorder = InMemorySpanRecorder()
    normal = normal_recorder.start_span(
        trace_id="trace",
        run_id="run",
        component="runtime",
        operation="run",
    )
    normal.end_ok()
    disabled_recorder, controller, disabled_facade = facade_for(
        FaultPoint.TRACE_BEFORE_SPAN_START,
        component="runtime",
        enabled=False,
    )
    disabled = disabled_facade.start_span(
        trace_id="trace",
        run_id="run",
        component="runtime",
        operation="run",
    )
    disabled.end_ok()

    first = normal_recorder.snapshot()[0]
    second = disabled_recorder.snapshot()[0]
    assert (first.trace_id, first.span_id, first.parent_span_id) == (
        second.trace_id,
        second.span_id,
        second.parent_span_id,
    )
    assert first.status == second.status
    assert normal_recorder.health_snapshot() == disabled_recorder.health_snapshot()
    counter = controller.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (0, 0)


@pytest.mark.asyncio
async def test_span_start_delay_cancellation_creates_no_active_span():
    source = CancellationSource()
    recorder = InMemorySpanRecorder()
    controller = diagnostic_controller(
        FaultPoint.TRACE_BEFORE_SPAN_START,
        component="runtime",
        action=FaultAction.DELAY,
        delay_seconds=1,
    )
    facade = OperationScopedSpanRecorder(
        recorder,
        fault_controller=controller,
        cancellation_token=source.token,
    )
    task = asyncio.create_task(
        asyncio.to_thread(
            facade.start_span,
            trace_id="trace",
            run_id="run",
            component="runtime",
            operation="run",
        )
    )
    while controller.snapshot().counters[0].hit_count == 0:
        await asyncio.sleep(0)
    source.cancel()
    handle = await asyncio.wait_for(task, 1)
    assert handle.context is None
    assert recorder.health_snapshot().active_span_count == 0
    assert recorder.health_snapshot().start_failures == 1


@pytest.mark.asyncio
async def test_span_end_block_cancellation_still_logically_closes():
    source = CancellationSource()
    recorder = InMemorySpanRecorder()
    blocker = FaultBlocker(timeout_seconds=2)
    controller = diagnostic_controller(
        FaultPoint.TRACE_BEFORE_SPAN_END,
        component="runtime",
        action=FaultAction.BLOCK_UNTIL_RELEASED,
        blocker=blocker,
    )
    facade = OperationScopedSpanRecorder(
        recorder,
        fault_controller=controller,
        cancellation_token=source.token,
    )
    handle = facade.start_span(
        trace_id="trace",
        run_id="run",
        component="runtime",
        operation="run",
    )
    task = asyncio.create_task(asyncio.to_thread(handle.end_ok))
    while not blocker.entered.is_set():
        await asyncio.sleep(0)
    source.cancel()
    await asyncio.wait_for(task, 1)
    blocker.close()
    assert handle.ended
    assert recorder.health_snapshot().active_span_count == 0
    assert recorder.health_snapshot().end_failures == 1


@pytest.mark.asyncio
async def test_trace_flush_delay_cancellation_is_fixed_and_bounded():
    source = CancellationSource()
    recorder = InMemorySpanRecorder()
    controller = diagnostic_controller(
        FaultPoint.TRACE_BEFORE_FLUSH,
        component="trace_recorder",
        action=FaultAction.DELAY,
        delay_seconds=1,
    )
    facade = OperationScopedSpanRecorder(
        recorder,
        fault_controller=controller,
        cancellation_token=source.token,
    )
    task = asyncio.create_task(asyncio.to_thread(facade.flush, 1))
    while controller.snapshot().counters[0].hit_count == 0:
        await asyncio.sleep(0)
    source.cancel()
    with pytest.raises(TraceOperationError) as captured:
        await asyncio.wait_for(task, 1)
    assert captured.value.error_code == "TRACE_FLUSH_CANCELLED"
    assert recorder.health_snapshot().flush_failures == 1


@pytest.mark.asyncio
async def test_trace_flush_block_respects_operation_timeout_and_can_close_later():
    recorder = InMemorySpanRecorder()
    blocker = FaultBlocker(timeout_seconds=2)
    controller = diagnostic_controller(
        FaultPoint.TRACE_BEFORE_FLUSH,
        component="trace_recorder",
        action=FaultAction.BLOCK_UNTIL_RELEASED,
        blocker=blocker,
    )
    facade = OperationScopedSpanRecorder(
        recorder,
        fault_controller=controller,
    )

    with pytest.raises(TraceOperationError) as captured:
        await asyncio.wait_for(asyncio.to_thread(facade.flush, 0.05), 1)

    assert captured.value.error_code == "TRACE_FLUSH_TIMEOUT"
    assert recorder.health_snapshot().flush_failures == 1
    controller.close()
    recorder.close(1)
    assert recorder.health_snapshot().active_span_count == 0
