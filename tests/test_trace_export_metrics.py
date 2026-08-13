#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP4-B Phase 3.3：Trace export metrics descriptor / 投影 / 边界测试。

真实 TraceExportDispatcher + metrics recorder 的指标事件验证，以及
reason/stage 词表、高基数 label 禁止、fingerprint label 禁止、metric failure
隔离。只使用 safe synthetic IDs 与固定 error codes。
"""

from __future__ import annotations

import dataclasses
import threading

import pytest

from core.runtime.metrics import (
    DEFAULT_RUNTIME_METRIC_REGISTRY,
    InMemoryMetricsRecorder,
    MetricLabelPolicy,
    MetricType,
)
from core.runtime.trace_contract import RUNTIME_RUN_SPAN
from core.runtime.trace_export_contract import TraceExportEnvelope, project_span
from core.runtime.trace_export_dispatcher import (
    TRACE_EXPORT_DROP_REASONS,
    TRACE_EXPORT_FAILURE_STAGES,
    TRACE_EXPORT_METRIC_ACCEPTED_TOTAL,
    TRACE_EXPORT_METRIC_ATTEMPTED_TOTAL,
    TRACE_EXPORT_METRIC_DROPPED_TOTAL,
    TRACE_EXPORT_METRIC_FAILURES_TOTAL,
    TRACE_EXPORT_METRIC_FLUSH_DURATION_SECONDS,
    TRACE_EXPORT_METRIC_QUEUE_DEPTH,
    TRACE_EXPORT_METRIC_SENT_TOTAL,
    TraceExportDispatcher,
)
from core.runtime.tracing import SpanRecord, SpanStatus

MAKER_MARKER = "MARKER_METRIC_9F31"


class FakeExporter:
    """支持 send_fail_first / 确定性阻塞 / close 失败注入。"""

    def __init__(
        self,
        *,
        send_fail_first: int = 0,
        close_result: bool = True,
        close_exception: Exception | None = None,
    ) -> None:
        self.received: list[TraceExportEnvelope] = []
        self.send_fail_first = send_fail_first
        self.close_result = close_result
        self.close_exception = close_exception
        self.close_calls = 0
        self.block_event: threading.Event | None = None
        self.block_started = threading.Event()

    def send(self, envelope: TraceExportEnvelope) -> None:
        assert isinstance(envelope, TraceExportEnvelope)
        if self.send_fail_first > 0:
            self.send_fail_first -= 1
            raise RuntimeError("transport down (synthetic)")
        if self.block_event is not None:
            self.block_started.set()
            self.block_event.wait()
        self.received.append(envelope)

    def close(self, timeout_seconds: float) -> bool:
        self.close_calls += 1
        if self.close_exception is not None:
            raise self.close_exception
        return self.close_result


def make_record(operation: str = RUNTIME_RUN_SPAN) -> SpanRecord:
    from datetime import UTC, datetime, timedelta

    started_at = datetime.now(UTC)
    return SpanRecord(
        trace_id="trace-m",
        span_id="span-m",
        parent_span_id=None,
        run_id="run-m",
        step_id=None,
        component="metrics",
        operation=operation,
        started_at=started_at,
        completed_at=started_at + timedelta(milliseconds=5),
        duration_ms=5.0,
        status=SpanStatus.OK,
        error_code=None,
        attributes={"plan_id": "plan-m"},
    )


def make_dispatcher(exporter: FakeExporter | None = None, capacity: int = 16):
    metrics = InMemoryMetricsRecorder()
    value = TraceExportDispatcher(
        exporter=exporter if exporter is not None else FakeExporter(),
        queue_capacity=capacity,
        metrics_recorder=metrics,
    )
    return value, metrics


# --- descriptors -----------------------------------------------------------


def test_exporter_metric_descriptors_exist_with_correct_types() -> None:
    registry = DEFAULT_RUNTIME_METRIC_REGISTRY
    expected = {
        TRACE_EXPORT_METRIC_ACCEPTED_TOTAL: MetricType.COUNTER,
        TRACE_EXPORT_METRIC_ATTEMPTED_TOTAL: MetricType.COUNTER,
        TRACE_EXPORT_METRIC_SENT_TOTAL: MetricType.COUNTER,
        TRACE_EXPORT_METRIC_DROPPED_TOTAL: MetricType.COUNTER,
        TRACE_EXPORT_METRIC_FAILURES_TOTAL: MetricType.COUNTER,
        TRACE_EXPORT_METRIC_QUEUE_DEPTH: MetricType.GAUGE,
        TRACE_EXPORT_METRIC_FLUSH_DURATION_SECONDS: MetricType.HISTOGRAM,
    }
    for name, metric_type in expected.items():
        assert name in registry, f"missing descriptor {name}"
        assert registry[name].type is metric_type
    assert registry[TRACE_EXPORT_METRIC_FLUSH_DURATION_SECONDS].unit == "seconds"
    assert registry[TRACE_EXPORT_METRIC_QUEUE_DEPTH].unit == "envelopes"


def test_exporter_metric_descriptor_label_bounds() -> None:
    dropped = DEFAULT_RUNTIME_METRIC_REGISTRY[TRACE_EXPORT_METRIC_DROPPED_TOTAL]
    failures = DEFAULT_RUNTIME_METRIC_REGISTRY[TRACE_EXPORT_METRIC_FAILURES_TOTAL]
    assert dropped.allowed_labels == frozenset({"reason"})
    assert dropped.required_labels == frozenset({"reason"})
    assert dropped.bounded_values["reason"] == TRACE_EXPORT_DROP_REASONS
    assert failures.allowed_labels == frozenset({"stage"})
    assert failures.required_labels == frozenset({"stage"})
    assert failures.bounded_values["stage"] == TRACE_EXPORT_FAILURE_STAGES


# --- 成功路径 -------------------------------------------------------------


def test_successful_path_metrics() -> None:
    dispatcher, metrics = make_dispatcher()
    assert dispatcher.observe_completed_span(make_record()) is True
    assert dispatcher.observe_completed_span(make_record()) is True
    assert dispatcher.flush(5.0) is True
    snapshot = metrics.snapshot()
    assert snapshot.counter(TRACE_EXPORT_METRIC_ACCEPTED_TOTAL) == 2
    assert snapshot.counter(TRACE_EXPORT_METRIC_ATTEMPTED_TOTAL) == 2
    assert snapshot.counter(TRACE_EXPORT_METRIC_SENT_TOTAL) == 2
    assert snapshot.gauge(TRACE_EXPORT_METRIC_QUEUE_DEPTH) == 0
    assert len(snapshot.histogram(TRACE_EXPORT_METRIC_FLUSH_DURATION_SECONDS)) == 1
    assert dispatcher.close(5.0) is True


# --- 各类 drop / failure 事件 ---------------------------------------------


def test_queue_full_drop_metric() -> None:
    exporter = FakeExporter()
    dispatcher, metrics = make_dispatcher(exporter=exporter, capacity=2)
    exporter.block_event = threading.Event()
    assert dispatcher.observe_completed_span(make_record()) is True
    assert exporter.block_started.wait(5.0)
    assert dispatcher.observe_completed_span(make_record()) is True
    assert dispatcher.observe_completed_span(make_record()) is True
    assert dispatcher.observe_completed_span(make_record()) is False
    snapshot = metrics.snapshot()
    assert snapshot.counter(TRACE_EXPORT_METRIC_DROPPED_TOTAL, {"reason": "queue_full"}) == 1
    # queue_full 不是执行失败
    assert snapshot.counter(TRACE_EXPORT_METRIC_FAILURES_TOTAL, {"stage": "transport"}) == 0
    exporter.block_event.set()
    assert dispatcher.close(5.0) is True


def test_projection_failure_drop_metric() -> None:
    dispatcher, metrics = make_dispatcher()
    assert dispatcher.observe_completed_span(make_record(operation="unknown.op")) is False
    snapshot = metrics.snapshot()
    assert snapshot.counter(TRACE_EXPORT_METRIC_DROPPED_TOTAL, {"reason": "projection_failed"}) == 1
    assert snapshot.counter(TRACE_EXPORT_METRIC_ACCEPTED_TOTAL) == 0
    assert dispatcher.close(5.0) is True


def test_compatibility_rejection_drop_metric(monkeypatch) -> None:
    from core.runtime import trace_export_dispatcher as dispatcher_module

    dispatcher, metrics = make_dispatcher()
    good = project_span(make_record())
    forged = dataclasses.replace(good, contract_fingerprint="0" * 64)
    monkeypatch.setattr(dispatcher_module, "project_span", lambda _record: forged)
    assert dispatcher.observe_completed_span(make_record()) is False
    snapshot = metrics.snapshot()
    assert snapshot.counter(TRACE_EXPORT_METRIC_DROPPED_TOTAL, {"reason": "incompatible"}) == 1
    assert snapshot.counter(TRACE_EXPORT_METRIC_ACCEPTED_TOTAL) == 0
    assert dispatcher.close(5.0) is True


def test_transport_failure_metrics() -> None:
    exporter = FakeExporter(send_fail_first=1)
    dispatcher, metrics = make_dispatcher(exporter=exporter)
    assert dispatcher.observe_completed_span(make_record()) is True
    assert dispatcher.observe_completed_span(make_record()) is True
    assert dispatcher.flush(5.0) is True
    snapshot = metrics.snapshot()
    assert snapshot.counter(TRACE_EXPORT_METRIC_ATTEMPTED_TOTAL) == 2
    assert snapshot.counter(TRACE_EXPORT_METRIC_SENT_TOTAL) == 1
    assert snapshot.counter(TRACE_EXPORT_METRIC_DROPPED_TOTAL, {"reason": "transport_failed"}) == 1
    assert snapshot.counter(TRACE_EXPORT_METRIC_FAILURES_TOTAL, {"stage": "transport"}) == 1
    assert dispatcher.close(5.0) is True


def test_flush_timeout_metrics() -> None:
    exporter = FakeExporter()
    dispatcher, metrics = make_dispatcher(exporter=exporter)
    exporter.block_event = threading.Event()
    assert dispatcher.observe_completed_span(make_record()) is True
    assert exporter.block_started.wait(5.0)
    assert dispatcher.flush(0.1) is False
    snapshot = metrics.snapshot()
    assert snapshot.counter(TRACE_EXPORT_METRIC_FAILURES_TOTAL, {"stage": "flush"}) == 1
    assert len(snapshot.histogram(TRACE_EXPORT_METRIC_FLUSH_DURATION_SECONDS)) == 1
    exporter.block_event.set()
    assert dispatcher.flush(5.0) is True
    assert dispatcher.close(5.0) is True


def test_close_failure_metrics() -> None:
    exporter = FakeExporter(close_result=False)
    dispatcher, metrics = make_dispatcher(exporter=exporter)
    assert dispatcher.close(5.0) is False
    snapshot = metrics.snapshot()
    assert snapshot.counter(TRACE_EXPORT_METRIC_FAILURES_TOTAL, {"stage": "close"}) == 1
    assert dispatcher.close(5.0) is False
    assert snapshot.counter(TRACE_EXPORT_METRIC_FAILURES_TOTAL, {"stage": "close"}) == 1


def test_worker_fatal_metrics_worker_unavailable_drop_parity() -> None:
    class FatalExporter(FakeExporter):
        def send(self, envelope: TraceExportEnvelope) -> None:
            raise SystemExit("synthetic worker invariant failure")

    exporter = FatalExporter()
    dispatcher, metrics = make_dispatcher(exporter=exporter, capacity=8)
    assert dispatcher.observe_completed_span(make_record()) is True
    assert dispatcher.observe_completed_span(make_record()) is True
    deadline = __import__("time").monotonic() + 5.0
    while (
        dispatcher.health().state.value != "FAILED"
        and __import__("time").monotonic() < deadline
    ):
        __import__("time").sleep(0.01)
    assert dispatcher.close(1.0) is False
    health = dispatcher.health()
    snapshot = metrics.snapshot()
    # P1-04：worker fatal abandonment 使用 worker_unavailable（不是 shutdown_timeout）
    assert health.dropped_total == 1
    assert snapshot.counter(
        TRACE_EXPORT_METRIC_DROPPED_TOTAL, {"reason": "worker_unavailable"}
    ) == 1
    assert snapshot.counter(
        TRACE_EXPORT_METRIC_DROPPED_TOTAL, {"reason": "shutdown_timeout"}
    ) == 0
    # worker fatal → stage=worker；close 失败 → stage=close（不伪造 transport）
    assert snapshot.counter(TRACE_EXPORT_METRIC_FAILURES_TOTAL, {"stage": "worker"}) == 1
    assert snapshot.counter(TRACE_EXPORT_METRIC_FAILURES_TOTAL, {"stage": "close"}) == 1
    assert snapshot.counter(TRACE_EXPORT_METRIC_FAILURES_TOTAL, {"stage": "transport"}) == 0
    # 幂等：重复 close/health/snapshot 不重复计数
    assert dispatcher.close(1.0) is False
    assert dispatcher.health().dropped_total == 1
    assert metrics.snapshot().counter(
        TRACE_EXPORT_METRIC_DROPPED_TOTAL, {"reason": "worker_unavailable"}
    ) == 1
    assert metrics.snapshot().counter(
        TRACE_EXPORT_METRIC_FAILURES_TOTAL, {"stage": "worker"}
    ) == 1


def test_queue_depth_gauge_reflects_depth() -> None:
    exporter = FakeExporter()
    dispatcher, metrics = make_dispatcher(exporter=exporter, capacity=2)
    exporter.block_event = threading.Event()
    assert dispatcher.observe_completed_span(make_record()) is True
    assert exporter.block_started.wait(5.0)
    assert dispatcher.observe_completed_span(make_record()) is True
    assert dispatcher.observe_completed_span(make_record()) is True
    assert metrics.snapshot().gauge(TRACE_EXPORT_METRIC_QUEUE_DEPTH) == 2
    exporter.block_event.set()
    assert dispatcher.flush(5.0) is True
    assert dispatcher.close(5.0) is True


# --- metric failure isolation ---------------------------------------------


def test_metrics_failure_isolation() -> None:
    class BrokenMetricsRecorder:
        label_policy = MetricLabelPolicy()

        def increment_counter(self, *args, **kwargs) -> None:
            raise RuntimeError("metrics down")

        def set_gauge(self, *args, **kwargs) -> None:
            raise RuntimeError("metrics down")

        def observe_histogram(self, *args, **kwargs) -> None:
            raise RuntimeError("metrics down")

    dispatcher = TraceExportDispatcher(
        exporter=FakeExporter(), queue_capacity=8, metrics_recorder=BrokenMetricsRecorder()
    )
    assert dispatcher.observe_completed_span(make_record()) is True
    assert dispatcher.flush(5.0) is True
    assert dispatcher.close(5.0) is True
    assert dispatcher.health().sent_total == 1


def test_metrics_absent_dispatcher_identical() -> None:
    dispatcher = TraceExportDispatcher(exporter=FakeExporter(), queue_capacity=8)
    assert dispatcher.observe_completed_span(make_record()) is True
    assert dispatcher.flush(5.0) is True
    assert dispatcher.close(5.0) is True
    assert dispatcher.health().sent_total == 1


# --- 词表 / 高基数边界 ----------------------------------------------------


def test_fabricated_reason_rejected() -> None:
    metrics = InMemoryMetricsRecorder()
    with pytest.raises(ValueError):
        metrics.increment_counter(
            TRACE_EXPORT_METRIC_DROPPED_TOTAL,
            labels={"reason": "RAW_USER_VALUE"},
        )


def test_fabricated_stage_rejected() -> None:
    metrics = InMemoryMetricsRecorder()
    with pytest.raises(ValueError):
        metrics.increment_counter(
            TRACE_EXPORT_METRIC_FAILURES_TOTAL,
            labels={"stage": "SOME_RANDOM_STAGE"},
        )


def test_approved_reason_and_stage_vocabulary_accepted() -> None:
    metrics = InMemoryMetricsRecorder()
    for reason in TRACE_EXPORT_DROP_REASONS:
        metrics.increment_counter(
            TRACE_EXPORT_METRIC_DROPPED_TOTAL, labels={"reason": reason}
        )
    for stage in TRACE_EXPORT_FAILURE_STAGES:
        metrics.increment_counter(
            TRACE_EXPORT_METRIC_FAILURES_TOTAL, labels={"stage": stage}
        )
    snapshot = metrics.snapshot()
    for reason in TRACE_EXPORT_DROP_REASONS:
        assert snapshot.counter(TRACE_EXPORT_METRIC_DROPPED_TOTAL, {"reason": reason}) == 1
    for stage in TRACE_EXPORT_FAILURE_STAGES:
        assert snapshot.counter(TRACE_EXPORT_METRIC_FAILURES_TOTAL, {"stage": stage}) == 1


@pytest.mark.parametrize(
    "forbidden_label",
    ["run_id", "trace_id", "span_id", "step_id", "url"],
)
def test_high_cardinality_denied_labels_rejected(forbidden_label: str) -> None:
    metrics = InMemoryMetricsRecorder()
    with pytest.raises(ValueError):
        metrics.increment_counter(
            TRACE_EXPORT_METRIC_ACCEPTED_TOTAL,
            labels={forbidden_label: "some-id-value"},
        )


@pytest.mark.parametrize(
    "forbidden_label",
    ["fingerprint", "contract_fingerprint", "endpoint", "raw_status", "raw_exception"],
)
def test_exporter_forbidden_labels_rejected_by_descriptor(forbidden_label: str) -> None:
    metrics = InMemoryMetricsRecorder()
    with pytest.raises(ValueError):
        metrics.increment_counter(
            TRACE_EXPORT_METRIC_DROPPED_TOTAL,
            labels={"reason": "queue_full", forbidden_label: "value"},
        )


def test_span_id_globally_denied_in_policy() -> None:
    dropped = DEFAULT_RUNTIME_METRIC_REGISTRY[TRACE_EXPORT_METRIC_DROPPED_TOTAL]
    with pytest.raises(ValueError):
        MetricLabelPolicy().normalize({"span_id": "span-1"}, dropped)


def test_fingerprint_never_a_metric_label() -> None:
    metrics = InMemoryMetricsRecorder()
    with pytest.raises(ValueError):
        metrics.increment_counter(
            TRACE_EXPORT_METRIC_ACCEPTED_TOTAL,
            labels={"fingerprint": "a" * 64},
        )
