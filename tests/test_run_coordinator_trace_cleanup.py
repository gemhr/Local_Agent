#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP4-A Phase 3.1：RunCoordinator root Span / ContextVar 全退出路径清理回归。

P1-01 RUN_COORDINATOR_TRACE_CONTEXT_CLEANUP_LEAK：
root Span 与 Trace/SpanRecorder ContextVar 成功安装后，execute() 的所有退出
路径（正常返回、RunCoordinatorError 重新抛出、清理阶段未知异常、取消、超时）
都必须结束 root Span 并恢复进入 execute() 前的上下文。

本文件只使用 safe synthetic IDs 与 fixed error codes，不携带业务正文。
"""

from __future__ import annotations

import pytest

from core.runtime import (
    CancellationReason,
    InMemorySpanRecorder,
    RunCoordinatorError,
    RunDeadlineExceededError,
    RunHandle,
    RunStatus,
    SpanStatus,
)
from core.runtime.tracing import (
    current_span_recorder,
    current_trace_context,
    install_span_recorder,
    install_trace_context,
    reset_span_recorder,
    reset_trace_context,
)
from core.runtime.trace_contract import RUNTIME_RUN_SPAN
from tests.test_run_coordinator import AsyncDriver, CoordinatorFixture


def registration_conflict(fixture: CoordinatorFixture) -> None:
    """预注册一个不同的 active handle，使 execute() 触发 COORDINATOR_REGISTRATION_FAILED。"""
    other = RunHandle(
        fixture.context.run_id,
        fixture.source,
        fixture.state,
        "other_owner",
    )
    fixture.registry.register(other)


def root_run_span(recorder: InMemorySpanRecorder):
    return next(
        record
        for record in recorder.snapshot()
        if record.component == "runtime"
        and record.operation == RUNTIME_RUN_SPAN
    )


@pytest.mark.asyncio
async def test_registration_failure_restores_empty_outer_context() -> None:
    """§16/§17：空外层上下文下的注册失败，Span/ContextVar 全部恢复为空。"""
    assert current_trace_context() is None
    assert current_span_recorder() is None

    recorder = InMemorySpanRecorder()
    fixture = CoordinatorFixture(span_recorder=recorder)
    registration_conflict(fixture)

    with pytest.raises(RunCoordinatorError) as exc_info:
        await fixture.coordinator.execute(driver=AsyncDriver())

    # 原异常类型/error_code/safe_message 语义保持不变。
    assert isinstance(exc_info.value, RunCoordinatorError)
    assert exc_info.value.error_code == "COORDINATOR_REGISTRATION_FAILED"
    assert exc_info.value.safe_message == "RunHandle 注册失败"
    # root Span 以既有 safe error code 收口且不残留 active span。
    assert recorder.health_snapshot().active_span_count == 0
    root = root_run_span(recorder)
    assert root.status is SpanStatus.ERROR
    assert root.error_code == "COORDINATOR_REGISTRATION_FAILED"
    assert len([r for r in recorder.snapshot() if r.operation == RUNTIME_RUN_SPAN]) == 1
    # ContextVar 恢复到进入 execute() 前的空/默认值。
    assert current_trace_context() is None
    assert current_span_recorder() is None


@pytest.mark.asyncio
async def test_registration_failure_restores_nested_outer_context_exactly() -> None:
    """§18：预先安装有效外层 TraceContext/recorder，失败后必须精确恢复。"""
    outer_recorder = InMemorySpanRecorder()
    outer_root = outer_recorder.start_span(
        trace_id="outer-trace-1",
        run_id="outer-run-1",
        component="runtime",
        operation="run",
    )
    trace_token = install_trace_context(outer_root.context)
    recorder_token = install_span_recorder(outer_recorder)
    try:
        assert current_trace_context() == outer_root.context
        assert current_span_recorder() is outer_recorder

        inner_recorder = InMemorySpanRecorder()
        fixture = CoordinatorFixture(span_recorder=inner_recorder)
        registration_conflict(fixture)

        with pytest.raises(RunCoordinatorError) as exc_info:
            await fixture.coordinator.execute(driver=AsyncDriver())
        assert exc_info.value.error_code == "COORDINATOR_REGISTRATION_FAILED"

        # 失败的内层 recorder 不留 active span。
        assert inner_recorder.health_snapshot().active_span_count == 0
        # 外层上下文必须精确恢复，而不是仅断言非 None。
        assert current_trace_context() == outer_root.context
        assert current_span_recorder() is outer_recorder
    finally:
        reset_trace_context(trace_token)
        reset_span_recorder(recorder_token)
    assert current_trace_context() is None
    assert current_span_recorder() is None


@pytest.mark.asyncio
async def test_normal_success_closes_root_span_and_restores_context() -> None:
    """§19：普通成功 Run 行为不变：root Span OK、无 active span、ContextVar 恢复。"""
    assert current_trace_context() is None
    recorder = InMemorySpanRecorder()
    fixture = CoordinatorFixture(span_recorder=recorder)

    result = await fixture.coordinator.execute(driver=AsyncDriver())

    assert result.status is RunStatus.SUCCEEDED
    assert recorder.health_snapshot().active_span_count == 0
    root = root_run_span(recorder)
    assert root.status is SpanStatus.OK
    assert len([r for r in recorder.snapshot() if r.operation == RUNTIME_RUN_SPAN]) == 1
    assert current_trace_context() is None
    assert current_span_recorder() is None


@pytest.mark.asyncio
async def test_cleanup_phase_exception_propagates_and_restores_context() -> None:
    """§20：root Span 安装后传播的清理阶段异常，原异常语义不变且上下文恢复。"""
    recorder = InMemorySpanRecorder()
    fixture = CoordinatorFixture(span_recorder=recorder)

    def broken_snapshot():
        raise RuntimeError("budget snapshot failure")

    fixture.ledger.snapshot = broken_snapshot

    with pytest.raises(RunCoordinatorError) as exc_info:
        await fixture.coordinator.execute(driver=AsyncDriver())

    assert exc_info.value.error_code == "BUDGET_SNAPSHOT_FAILED"
    assert recorder.health_snapshot().active_span_count == 0
    root = root_run_span(recorder)
    assert root.status is SpanStatus.ERROR
    assert root.error_code == "BUDGET_SNAPSHOT_FAILED"
    assert current_trace_context() is None
    assert current_span_recorder() is None


@pytest.mark.asyncio
async def test_cancellation_and_timeout_restore_context() -> None:
    """§21：取消与超时路径通过同一收口点恢复上下文。"""
    cancelled_recorder = InMemorySpanRecorder()
    cancelled = CoordinatorFixture(span_recorder=cancelled_recorder)
    cancelled.source.cancel(CancellationReason.USER_CANCELLED)
    cancelled_result = await cancelled.coordinator.execute(driver=AsyncDriver())
    assert cancelled_result.status is RunStatus.CANCELLED
    assert cancelled_recorder.health_snapshot().active_span_count == 0
    assert root_run_span(cancelled_recorder).status is SpanStatus.CANCELLED
    assert current_trace_context() is None
    assert current_span_recorder() is None

    class DeadlineDriver:
        async def execute(self, claim, run_context):
            raise RunDeadlineExceededError("deadline")

    timeout_recorder = InMemorySpanRecorder()
    timed_out = CoordinatorFixture(span_recorder=timeout_recorder)
    timed_out_result = await timed_out.coordinator.execute(driver=DeadlineDriver())
    assert timed_out_result.status is RunStatus.FAILED
    assert timed_out_result.stop_reason.value == "DEADLINE_EXCEEDED"
    assert timeout_recorder.health_snapshot().active_span_count == 0
    assert root_run_span(timeout_recorder).status is SpanStatus.ERROR
    assert current_trace_context() is None
    assert current_span_recorder() is None


@pytest.mark.asyncio
async def test_sequential_runs_do_not_leak_trace_across_runs() -> None:
    """§23：Run A 注册失败后，Run B 不得继承 A 的 trace_id/span_id/recorder。"""
    run_a_recorder = InMemorySpanRecorder()
    fixture_a = CoordinatorFixture(span_recorder=run_a_recorder)
    registration_conflict(fixture_a)
    with pytest.raises(RunCoordinatorError) as exc_info:
        await fixture_a.coordinator.execute(driver=AsyncDriver())
    assert exc_info.value.error_code == "COORDINATOR_REGISTRATION_FAILED"
    run_a_trace_id = fixture_a.context.trace_id
    assert current_trace_context() is None
    assert current_span_recorder() is None

    run_b_recorder = InMemorySpanRecorder()
    fixture_b = CoordinatorFixture(span_recorder=run_b_recorder)
    result_b = await fixture_b.coordinator.execute(driver=AsyncDriver())
    assert result_b.status is RunStatus.SUCCEEDED

    root_b = root_run_span(run_b_recorder)
    assert root_b.trace_id == fixture_b.context.trace_id
    assert root_b.trace_id != run_a_trace_id
    assert root_b.run_id == fixture_b.context.run_id
    # 各自 recorder 不混入另一 Run 的 correlation 事实。
    assert not any(
        r.run_id == fixture_a.context.run_id for r in run_b_recorder.snapshot()
    )
    assert not any(
        r.run_id == fixture_b.context.run_id for r in run_a_recorder.snapshot()
    )
    assert current_trace_context() is None
    assert current_span_recorder() is None


def test_finalize_root_span_first_end_wins_does_not_overwrite() -> None:
    """§22：收口机制不能覆盖已被既有正常路径合法结束的 root Span。"""
    recorder = InMemorySpanRecorder()
    handle = recorder.start_span(
        trace_id="first-end-trace",
        run_id="first-end-run",
        component="runtime",
        operation="run",
    )
    handle.end_ok()

    fixture = CoordinatorFixture()
    fixture.coordinator._finalize_root_span(
        handle,
        result=None,
        terminal_publication_failed=False,
    )

    records = recorder.snapshot()
    assert len(records) == 1
    assert records[0].status is SpanStatus.OK
    assert recorder.health_snapshot().active_span_count == 0
