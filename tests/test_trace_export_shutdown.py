#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP4-B Phase 3.3：ApplicationRuntimeServices 生命周期集成与 component truth。

真实 InMemorySpanRecorder + 真实 TraceExportDispatcher 经实际生命周期 API 的
端到端验证：flush/close target 顺序、producer barrier、RECORDER_CLOSED final
export、disabled-by-absence、FLUSH/CLOSE component truth、close timeout
truth、剩余组件继续、span-recorder 失败与 dispatcher 结果共存。只使用 safe
synthetic IDs 与固定 error codes。
"""

from __future__ import annotations

import threading
import time

import pytest

from core.runtime import ApplicationRuntimeServices
from core.runtime.tracing import InMemorySpanRecorder, SpanStatus
from core.runtime.trace_contract import RUNTIME_RUN_SPAN
from core.runtime.trace_export_dispatcher import (
    TraceExportDispatcher,
    TraceExportDispatcherState,
)
from tests._runtime_assembly_fixtures import make_services


class FakeExporter:
    """记录 send/close 事件顺序；支持 close 失败/异常与确定性阻塞。"""

    def __init__(
        self,
        *,
        close_result: bool = True,
        close_exception: Exception | None = None,
    ) -> None:
        self.events: list[tuple[object, ...]] = []
        self.close_calls = 0
        self.close_result = close_result
        self.close_exception = close_exception
        self.block_event: threading.Event | None = None
        self.block_started = threading.Event()

    def send(self, envelope: object) -> None:
        if self.block_event is not None:
            self.block_started.set()
            self.block_event.wait()
        self.events.append(
            (
                "send",
                getattr(envelope, "span_id", None),
                getattr(envelope, "status", None).value,
                getattr(envelope, "error_code", None),
            )
        )

    def close(self, timeout_seconds: float) -> bool:
        self.close_calls += 1
        self.events.append(("close",))
        if self.close_exception is not None:
            raise self.close_exception
        return self.close_result


class RecordingLifecycle:
    """确定性记录 flush/close 调用顺序的 lifecycle 组件。"""

    def __init__(
        self,
        name: str,
        calls: list[tuple[str, str]],
        *,
        flush_result: bool = True,
        close_result: bool = True,
    ) -> None:
        self.name = name
        self.calls = calls
        self.flush_result = flush_result
        self.close_result = close_result

    def flush(self, timeout_seconds: float | None = None) -> bool:
        self.calls.append(("flush", self.name))
        return self.flush_result

    def close(self, timeout_seconds: float | None = None) -> bool:
        self.calls.append(("close", self.name))
        return self.close_result


class FailingRecorder:
    """close 抛普通异常的 span recorder 替身。"""

    def close(self, timeout_seconds: float | None = None) -> bool:
        raise RuntimeError("raw recorder close secret must not leak")


def ordered_services(calls: list[tuple[str, str]]) -> ApplicationRuntimeServices:
    """全部有序 target 用 RecordingLifecycle 构造（确定性顺序验证）。"""
    return ApplicationRuntimeServices(
        event_journal=RecordingLifecycle("event_journal", calls),
        observability_dispatcher=RecordingLifecycle(
            "observability_dispatcher", calls
        ),
        structured_logger=object(),
        runtime_metrics_recorder=object(),
        span_recorder=RecordingLifecycle("span_recorder", calls),
        snapshot_store=RecordingLifecycle("snapshot_store", calls),
        recovery_validator=None,
        model_invocation_router=object(),
        tool_execution_service=object(),
        retrieval_execution_service=None,
        blocking_executors=(),
        worker_trackers=(),
        run_registry=object(),
        snapshot_enabled=True,
        recovery_enabled=False,
        trace_export_dispatcher=RecordingLifecycle(
            "trace_export_dispatcher", calls
        ),
    )


def real_services(*, exporter: FakeExporter, capacity: int = 8):
    """真实 recorder（observer 绑定）+ 真实 dispatcher 的 ApplicationRuntimeServices。"""
    dispatcher = TraceExportDispatcher(
        exporter=exporter, queue_capacity=capacity
    )
    recorder = InMemorySpanRecorder(
        completion_observer=dispatcher.observe_completed_span
    )
    base = make_services(span_recorder=recorder, snapshot_enabled=True)
    services = ApplicationRuntimeServices(
        event_journal=base.event_journal,
        observability_dispatcher=base.observability_dispatcher,
        structured_logger=base.structured_logger,
        runtime_metrics_recorder=base.runtime_metrics_recorder,
        span_recorder=recorder,
        snapshot_store=base.snapshot_store,
        recovery_validator=base.recovery_validator,
        model_invocation_router=base.model_invocation_router,
        tool_execution_service=base.tool_execution_service,
        retrieval_execution_service=base.retrieval_execution_service,
        blocking_executors=(),
        worker_trackers=(),
        run_registry=base.run_registry,
        snapshot_enabled=True,
        recovery_enabled=True,
        trace_export_dispatcher=dispatcher,
    )
    return services, recorder, dispatcher


def start_active_span(recorder: InMemorySpanRecorder, index: int):
    return recorder.start_span(
        trace_id="t-life",
        run_id="r-life",
        component="integration",
        operation=RUNTIME_RUN_SPAN,
    )


# --- disabled-by-absence ---------------------------------------------------


def test_disabled_by_absence_no_trace_export_component() -> None:
    services = make_services()  # trace_export_dispatcher 默认 None
    assert services.trace_export_dispatcher is None
    targets = services._targets()
    assert not any(
        component == "trace_export_dispatcher"
        for component, _target, _operation in targets
    )


@pytest.mark.asyncio
async def test_disabled_by_absence_flush_close_component_results() -> None:
    services = make_services()
    flush_report = await services.flush(1)
    assert not any(
        component.component == "trace_export_dispatcher"
        for component in flush_report.components
    )
    close_report = await services.close(1)
    assert not any(
        component.component == "trace_export_dispatcher"
        for component in close_report.components
    )
    assert close_report.completed is True


# --- target 顺序 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_target_order_deterministic() -> None:
    calls: list[tuple[str, str]] = []
    services = ordered_services(calls)
    report = await services.flush(1)
    assert report.completed is True
    flush_order = [name for kind, name in calls if kind == "flush"]
    assert flush_order == [
        "observability_dispatcher",
        "span_recorder",
        "trace_export_dispatcher",
        "snapshot_store",
        "event_journal",
    ]


@pytest.mark.asyncio
async def test_close_target_order_deterministic() -> None:
    calls: list[tuple[str, str]] = []
    services = ordered_services(calls)
    report = await services.close(1)
    assert report.completed is True
    close_order = [name for kind, name in calls if kind == "close"]
    assert close_order == [
        "observability_dispatcher",
        "span_recorder",
        "trace_export_dispatcher",
        "snapshot_store",
        "event_journal",
    ]


# --- 真实 recorder -> dispatcher 生命周期 -----------------------------------


@pytest.mark.asyncio
async def test_real_close_final_export_before_adapter_close() -> None:
    exporter = FakeExporter()
    services, recorder, dispatcher = real_services(exporter=exporter)
    active = [start_active_span(recorder, index) for index in range(3)]
    report = await services.close(5.0)
    assert report.completed is True
    # adapter send（RECORDER_CLOSED final envelopes）先于 adapter close
    assert exporter.events[-1] == ("close",)
    sends = [event for event in exporter.events if event[0] == "send"]
    assert len(sends) == 3
    assert all(event[2] == SpanStatus.CANCELLED.value for event in sends)
    assert all(event[3] == "RECORDER_CLOSED" for event in sends)
    assert exporter.close_calls == 1
    # component truth：span_recorder 与 trace_export_dispatcher 独立 COMPLETED
    components = {item.component: item for item in report.components}
    assert components["span_recorder"].status == "COMPLETED"
    assert components["trace_export_dispatcher"].status == "COMPLETED"
    assert components["trace_export_dispatcher"].operation == "CLOSE"


@pytest.mark.asyncio
async def test_producer_barrier_via_lifecycle_close() -> None:
    exporter = FakeExporter()
    services, recorder, dispatcher = real_services(exporter=exporter)
    active = [start_active_span(recorder, index) for index in range(4)]
    await services.close(5.0)
    # close() 返回后所有 close-start active handles 已同步收口并完成 observer
    assert recorder.health_snapshot().active_span_count == 0
    assert recorder.health_snapshot().dropped_span_count == 4
    assert dispatcher.health().sent_total == 4
    assert dispatcher.health().state is TraceExportDispatcherState.CLOSED
    assert len(active) == 4


# --- flush / close component truth -----------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_flush_failure_component_truth() -> None:
    exporter = FakeExporter()
    exporter.block_event = threading.Event()
    services, recorder, dispatcher = real_services(exporter=exporter)
    handle = start_active_span(recorder, 0)
    handle.end_ok()
    assert exporter.block_started.wait(5.0)
    report = await services.flush(1.0)
    components = {item.component: item for item in report.components}
    assert report.completed is False
    exporter_comp = components["trace_export_dispatcher"]
    assert exporter_comp.status == "FAILED"
    assert exporter_comp.operation == "FLUSH"
    assert exporter_comp.error_code == "RUNTIME_TRACE_EXPORT_FLUSH_TIMEOUT"
    # 其他组件继续且成功；dispatcher 保持 RUNNING
    assert components["observability_dispatcher"].status == "COMPLETED"
    assert components["span_recorder"].status == "COMPLETED"
    assert dispatcher.health().state is TraceExportDispatcherState.RUNNING
    exporter.block_event.set()
    assert dispatcher.flush(5.0) is True
    assert dispatcher.close(5.0) is True


@pytest.mark.asyncio
async def test_adapter_close_false_component_truth() -> None:
    exporter = FakeExporter(close_result=False)
    services, recorder, dispatcher = real_services(exporter=exporter)
    report = await services.close(5.0)
    components = {item.component: item for item in report.components}
    assert report.completed is False
    exporter_comp = components["trace_export_dispatcher"]
    assert exporter_comp.status == "FAILED"
    assert exporter_comp.operation == "CLOSE"
    # 物理 lifecycle 已结束（state==CLOSED）但 adapter close 失败 → CLOSE_FAILED，
    # 不得归类为 TIMEOUT。
    assert exporter_comp.error_code == "RUNTIME_TRACE_EXPORT_CLOSE_FAILED"
    # 其余组件仍关闭
    assert components["observability_dispatcher"].status == "COMPLETED"
    assert components["event_journal"].status == "COMPLETED"
    assert exporter.close_calls == 1
    # dispatcher CLOSED 仍可能是 close result False（adapter 物理 close 失败）
    assert dispatcher.health().state is TraceExportDispatcherState.CLOSED


@pytest.mark.asyncio
async def test_adapter_close_exception_component_truth_no_raw_leak() -> None:
    exporter = FakeExporter(
        close_exception=RuntimeError("raw close secret must not leak")
    )
    services, recorder, dispatcher = real_services(exporter=exporter)
    report = await services.close(5.0)
    components = {item.component: item for item in report.components}
    exporter_comp = components["trace_export_dispatcher"]
    assert exporter_comp.status == "FAILED"
    assert exporter_comp.operation == "CLOSE"
    # adapter close 异常被 dispatcher content-free 隔离后物理结束（state==CLOSED）
    # → CLOSE_FAILED，不得归类为 TIMEOUT。
    assert exporter_comp.error_code == "RUNTIME_TRACE_EXPORT_CLOSE_FAILED"
    assert dispatcher.health().state is TraceExportDispatcherState.CLOSED
    assert "raw close secret must not leak" not in repr(report)
    assert "raw close secret must not leak" not in str(report)
    assert "raw close secret must not leak" not in repr(dispatcher.health())
    assert components["observability_dispatcher"].status == "COMPLETED"
    assert components["event_journal"].status == "COMPLETED"
    assert exporter.close_calls == 1


@pytest.mark.asyncio
async def test_dispatcher_close_timeout_truth() -> None:
    exporter = FakeExporter()
    exporter.block_event = threading.Event()
    services, recorder, dispatcher = real_services(exporter=exporter)
    handle = start_active_span(recorder, 0)
    handle.end_ok()
    assert exporter.block_started.wait(5.0)
    report = await services.close(0.4)
    components = {item.component: item for item in report.components}
    exporter_comp = components["trace_export_dispatcher"]
    assert exporter_comp.status == "FAILED"
    assert exporter_comp.operation == "CLOSE"
    assert exporter_comp.error_code == "RUNTIME_TRACE_EXPORT_CLOSE_TIMEOUT"
    # worker 仍存活：dispatcher 不得被伪报为 CLOSED
    assert dispatcher.health().state is TraceExportDispatcherState.CLOSING
    # 释放 worker：dispatcher 自身后续 bounded close 完成
    exporter.block_event.set()
    assert dispatcher.close(5.0) is True
    assert dispatcher.health().state is TraceExportDispatcherState.CLOSED
    # 剩余组件在 lifecycle close 中继续
    assert components["observability_dispatcher"].status == "COMPLETED"
    assert components["event_journal"].status == "COMPLETED"


@pytest.mark.asyncio
async def test_span_recorder_failure_and_dispatcher_result_coexist() -> None:
    exporter = FakeExporter()
    dispatcher = TraceExportDispatcher(exporter=exporter, queue_capacity=8)
    base = make_services(snapshot_enabled=True)
    services = ApplicationRuntimeServices(
        event_journal=base.event_journal,
        observability_dispatcher=base.observability_dispatcher,
        structured_logger=base.structured_logger,
        runtime_metrics_recorder=base.runtime_metrics_recorder,
        span_recorder=FailingRecorder(),
        snapshot_store=base.snapshot_store,
        recovery_validator=base.recovery_validator,
        model_invocation_router=base.model_invocation_router,
        tool_execution_service=base.tool_execution_service,
        retrieval_execution_service=base.retrieval_execution_service,
        blocking_executors=(),
        worker_trackers=(),
        run_registry=base.run_registry,
        snapshot_enabled=True,
        recovery_enabled=True,
        trace_export_dispatcher=dispatcher,
    )
    report = await services.close(5.0)
    components = {item.component: item for item in report.components}
    assert components["span_recorder"].status == "FAILED"
    assert components["span_recorder"].error_code == "RUNTIME_COMPONENT_CLOSE_FAILED"
    # dispatcher 结果独立保留（不被 recorder 失败吞掉）
    assert components["trace_export_dispatcher"].status == "COMPLETED"
    assert components["trace_export_dispatcher"].error_code is None
    assert exporter.close_calls == 1
    assert report.completed is False


@pytest.mark.asyncio
async def test_worker_fatal_close_classified_as_failed_not_timeout() -> None:
    """P1-02：worker fatal（FAILED、worker 已死）必须分类为 CLOSE_FAILED 而非 TIMEOUT。"""

    class FatalExporter(FakeExporter):
        def send(self, envelope: object) -> None:
            raise SystemExit("SYNTHETIC_FATAL_SECRET_MARKER")

    exporter = FatalExporter()
    services, recorder, dispatcher = real_services(exporter=exporter, capacity=8)
    handle = start_active_span(recorder, 0)
    handle.end_ok()
    deadline = time.monotonic() + 5.0
    while (
        dispatcher.health().state is not TraceExportDispatcherState.FAILED
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert dispatcher.health().state is TraceExportDispatcherState.FAILED
    assert dispatcher._worker.is_alive() is False
    started = time.monotonic()
    report = await services.close(5.0)
    elapsed = time.monotonic() - started
    components = {item.component: item for item in report.components}
    exporter_comp = components["trace_export_dispatcher"]
    assert exporter_comp.status == "FAILED"
    assert exporter_comp.operation == "CLOSE"
    assert exporter_comp.error_code == "RUNTIME_TRACE_EXPORT_CLOSE_FAILED"
    assert exporter_comp.error_code != "RUNTIME_TRACE_EXPORT_CLOSE_TIMEOUT"
    # close 不经 deadline 耗尽立即返回（bounded）
    assert elapsed < 1.0
    # adapter 从未被调用（worker 死于 sentinel 之前）
    assert exporter.close_calls == 0
    # raw marker 不出现在任何 component/report/health 输出
    assert "SYNTHETIC_FATAL_SECRET_MARKER" not in repr(report)
    assert "SYNTHETIC_FATAL_SECRET_MARKER" not in str(report)
    assert "SYNTHETIC_FATAL_SECRET_MARKER" not in repr(dispatcher.health())
    assert "SYNTHETIC_FATAL_SECRET_MARKER" not in repr(exporter_comp)
    # 剩余生命周期组件继续
    assert components["observability_dispatcher"].status == "COMPLETED"
    assert components["span_recorder"].status == "COMPLETED"
    assert components["event_journal"].status == "COMPLETED"
