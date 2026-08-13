#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP4-B Phase 3.1：TraceExportDispatcher / TraceExporter 协议强制单测。

只使用 safe synthetic IDs、fixed error codes 与合成 marker；不携带真实密钥或
业务正文。覆盖：构造、envelope 边界、projection/compatibility、queue
saturation、跨线程 submission、单 worker、transport 失败隔离、flush barrier、
close（幂等/并发/超时）、FAILED 状态、health 与 forbidden-feature 审计。
"""

from __future__ import annotations

import dataclasses
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.runtime.tracing import SpanRecord, SpanStatus
from core.runtime.trace_contract import RUNTIME_RUN_SPAN, RUNTIME_STEP_SPAN
from core.runtime.trace_export_contract import TraceExportEnvelope, project_span
from core.runtime.trace_export_dispatcher import (
    TRACE_EXPORT_CLOSE_FAILED,
    TRACE_EXPORT_CLOSED,
    TRACE_EXPORT_DROP_REASONS,
    TRACE_EXPORT_FLUSH_TIMEOUT,
    TRACE_EXPORT_INCOMPATIBLE,
    TRACE_EXPORT_PROJECTION_FAILED,
    TRACE_EXPORT_QUEUE_FULL,
    TRACE_EXPORT_TRANSPORT_FAILED,
    TRACE_EXPORT_WORKER_FAILED,
    TraceExportDispatcher,
    TraceExportDispatcherState,
    TraceExportHealthSnapshot,
)

MAKER_MARKER = "MARKER_SESSION_9F31"


class RecordingExporter:
    """Record 每个收到的 envelope（typed check）、send 线程与 close 次数。

    支持确定性阻塞（``block_event`` + ``block_started``）、前 N 次 send 抛错
    （``send_fail_first``）与 close 结果/异常注入，全部用于 Events 驱动的
    确定性测试，不做 fragile sleep oracle。
    """

    def __init__(self) -> None:
        self.received: list[TraceExportEnvelope] = []
        self.send_thread_ids: list[int] = []
        self.max_in_flight = 0
        self.close_calls = 0
        self.close_result = True
        self.close_exception: Exception | None = None
        self.send_fail_first = 0
        self.block_event: threading.Event | None = None
        self.block_started = threading.Event()
        self._lock = threading.Lock()
        self._in_flight = 0

    def send(self, envelope: TraceExportEnvelope) -> None:
        assert isinstance(envelope, TraceExportEnvelope)
        with self._lock:
            self.send_thread_ids.append(threading.get_ident())
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            if self.send_fail_first > 0:
                self.send_fail_first -= 1
                raise RuntimeError("transport unavailable (synthetic)")
            if self.block_event is not None:
                self.block_started.set()
                self.block_event.wait()
            self.received.append(envelope)
        finally:
            with self._lock:
                self._in_flight -= 1

    def close(self, timeout_seconds: float) -> bool:
        self.close_calls += 1
        if self.close_exception is not None:
            raise self.close_exception
        return self.close_result


def make_record(
    *,
    operation: str = RUNTIME_RUN_SPAN,
    component: str = "runtime",
    step_id: str | None = None,
    status: SpanStatus = SpanStatus.OK,
    error_code: str | None = None,
    attributes: dict[str, object] | None = None,
) -> SpanRecord:
    started_at = datetime.now(UTC)
    return SpanRecord(
        trace_id="trace-d-1",
        span_id="span-d-1",
        parent_span_id=None,
        run_id="run-d-1",
        step_id=step_id,
        component=component,
        operation=operation,
        started_at=started_at,
        completed_at=started_at + timedelta(milliseconds=5),
        duration_ms=5.0,
        status=status,
        error_code=error_code,
        attributes=dict(attributes or {}),
    )


@pytest.fixture
def dispatcher_factory():
    instances: list[TraceExportDispatcher] = []

    def factory(exporter: object | None = None, capacity: int = 16) -> TraceExportDispatcher:
        value = TraceExportDispatcher(
            exporter=exporter if exporter is not None else RecordingExporter(),
            queue_capacity=capacity,
        )
        instances.append(value)
        return value

    yield factory

    for value in instances:
        exporter = getattr(value, "_exporter", None)
        release = getattr(exporter, "block_event", None)
        if release is not None:
            release.set()
        value.close(5.0)


# --- 构造 ----------------------------------------------------------------


def test_construction_valid_exporter_positive_capacity_running(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=8)
    assert d.health().state is TraceExportDispatcherState.RUNNING
    assert d.health().queue_capacity == 8
    assert d.health().queue_depth == 0


def test_construction_worker_starts_once_daemon(dispatcher_factory) -> None:
    d = dispatcher_factory(capacity=4)
    assert d._worker.is_alive()
    assert d._worker.daemon is True
    assert d._worker.name == "trace-export-worker"
    assert d.health().state is TraceExportDispatcherState.RUNNING


def test_construction_rejects_zero_or_negative_capacity() -> None:
    with pytest.raises(ValueError):
        TraceExportDispatcher(exporter=RecordingExporter(), queue_capacity=0)
    with pytest.raises(ValueError):
        TraceExportDispatcher(exporter=RecordingExporter(), queue_capacity=-5)


def test_construction_rejects_bool_capacity() -> None:
    with pytest.raises(TypeError):
        TraceExportDispatcher(exporter=RecordingExporter(), queue_capacity=True)


def test_construction_rejects_invalid_exporter_shape() -> None:
    class NoSend:
        def close(self, timeout_seconds: float) -> bool:
            return True

    class NoClose:
        def send(self, envelope: TraceExportEnvelope) -> None:
            return None

    with pytest.raises(TypeError):
        TraceExportDispatcher(exporter=NoSend(), queue_capacity=4)
    with pytest.raises(TypeError):
        TraceExportDispatcher(exporter=NoClose(), queue_capacity=4)


# --- envelope 边界 --------------------------------------------------------


def test_valid_span_reaches_exporter_as_envelope_only(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=8)
    record = make_record(attributes={"plan_id": "plan-1"})
    assert d.observe_completed_span(record) is True
    assert d.flush(5.0) is True
    assert len(exporter.received) == 1
    envelope = exporter.received[0]
    assert isinstance(envelope, TraceExportEnvelope)
    assert not isinstance(envelope, SpanRecord)
    assert not isinstance(envelope, dict)
    assert envelope is not record
    assert envelope.run_id == "run-d-1"
    assert envelope.attributes["plan_id"] == "plan-1"


def test_internal_only_and_raw_content_markers_absent_from_envelope(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=8)
    record = make_record(
        attributes={
            "plan_id": "plan-x",
            "session_id": MAKER_MARKER,
            "runtime_version": "runtime_v9",
            "prompt": "MARKER_PROMPT_9F31",
        }
    )
    assert d.observe_completed_span(record) is True
    assert d.flush(5.0) is True
    envelope = exporter.received[0]
    assert isinstance(envelope, TraceExportEnvelope)
    assert envelope.attributes["plan_id"] == "plan-x"
    for forbidden in ("session_id", "runtime_version", "prompt"):
        assert forbidden not in envelope.attributes


def test_step_bound_span_projected_and_sent(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=8)
    record = make_record(
        operation=RUNTIME_STEP_SPAN,
        component="step",
        step_id="step-1",
        attributes={"execution_kind": "AGENT", "output_policy": "INTERNAL"},
    )
    assert d.observe_completed_span(record) is True
    assert d.flush(5.0) is True
    envelope = exporter.received[0]
    assert envelope.step_id == "step-1"
    assert envelope.attributes["execution_kind"] == "AGENT"


# --- projection -----------------------------------------------------------


def test_projection_failure_returns_false_no_enqueue_safe_counters(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=8)
    started_at = datetime.now(UTC)
    incomplete = SpanRecord(
        trace_id="trace-d-1",
        span_id="span-d-1",
        parent_span_id=None,
        run_id="run-d-1",
        step_id=None,
        component="runtime",
        operation=RUNTIME_RUN_SPAN,
        started_at=started_at,
        completed_at=None,
        duration_ms=None,
        status=SpanStatus.UNSET,
        error_code=None,
        attributes={},
    )
    assert d.observe_completed_span(incomplete) is False
    health = d.health()
    assert health.accepted_total == 0
    assert health.dropped_total == 1
    assert health.last_safe_error_code == TRACE_EXPORT_PROJECTION_FAILED
    assert d.flush(5.0) is True
    assert exporter.received == []
    assert d.close(5.0) is True


def test_observe_rejects_non_span_record_content_free(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=8)
    assert d.observe_completed_span({"span": "dict"}) is False
    assert d.observe_completed_span(None) is False
    assert d.health().dropped_total == 0
    assert exporter.received == []
    d.close(5.0)


# --- compatibility --------------------------------------------------------


def test_current_compatible_envelope_accepted_and_sent(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=8)
    assert d.observe_completed_span(make_record()) is True
    assert d.flush(5.0) is True
    assert len(exporter.received) == 1
    assert isinstance(exporter.received[0], TraceExportEnvelope)
    assert d.health().last_safe_error_code is None


def test_compatibility_rejection_no_send_safe_accounting(monkeypatch, dispatcher_factory) -> None:
    from core.runtime import trace_export_dispatcher as dispatcher_module

    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=8)
    record = make_record()
    good = project_span(record)
    forged = dataclasses.replace(good, contract_fingerprint="0" * 64)
    monkeypatch.setattr(dispatcher_module, "project_span", lambda _record: forged)
    assert d.observe_completed_span(record) is False
    health = d.health()
    assert health.dropped_total == 1
    assert health.last_safe_error_code == TRACE_EXPORT_INCOMPATIBLE
    assert health.accepted_total == 0
    assert d.flush(5.0) is True
    assert exporter.received == []
    assert d.close(5.0) is True


# --- queue saturation -----------------------------------------------------


def test_queue_full_rejects_incoming_nonblocking(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=2)
    exporter.block_event = threading.Event()
    # item 1：worker 立即取走并阻塞在 send
    assert d.observe_completed_span(make_record(attributes={"plan_id": "p1"})) is True
    assert exporter.block_started.wait(5.0)
    # 填满 bounded queue（capacity 2）
    assert d.observe_completed_span(make_record(attributes={"plan_id": "p2"})) is True
    assert d.observe_completed_span(make_record(attributes={"plan_id": "p3"})) is True
    assert d.health().queue_depth == 2
    # 第 4 个：非阻塞拒绝，producer 不依赖 exporter release
    started = time.monotonic()
    assert d.observe_completed_span(make_record(attributes={"plan_id": "p4"})) is False
    elapsed = time.monotonic() - started
    health = d.health()
    assert health.dropped_total == 1
    assert health.failed_total == 0
    assert health.last_safe_error_code == TRACE_EXPORT_QUEUE_FULL
    assert elapsed < 0.5
    exporter.block_event.set()
    assert d.flush(5.0) is True
    assert d.close(5.0) is True
    assert len(exporter.received) == 3
    assert exporter.received[0].attributes["plan_id"] == "p1"


# --- 跨线程 submission ----------------------------------------------------


def test_cross_thread_submission_thread_safe_truthful_counts(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=1024)
    threads = 8
    per_thread = 40
    results: list[bool] = []
    results_lock = threading.Lock()

    def submit() -> None:
        local = [
            d.observe_completed_span(
                make_record(attributes={"plan_id": f"t{index}"})
            )
            for index in range(per_thread)
        ]
        with results_lock:
            results.extend(local)

    workers = [threading.Thread(target=submit) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10.0)
    accepted = sum(1 for result in results if result)
    assert accepted == threads * per_thread
    assert d.flush(5.0) is True
    health = d.health()
    assert health.accepted_total == accepted
    assert health.attempted_total == accepted
    assert health.sent_total == accepted
    assert health.failed_total == 0
    assert health.dropped_total == 0
    assert len(exporter.received) == accepted
    assert all(isinstance(item, TraceExportEnvelope) for item in exporter.received)
    assert d.close(5.0) is True


# --- 单 worker ------------------------------------------------------------


def test_single_worker_serial_send_no_caller_thread_send(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=64)
    for index in range(50):
        assert d.observe_completed_span(
            make_record(attributes={"plan_id": f"w{index}"})
        ) is True
    assert d.flush(5.0) is True
    worker_id = d._worker.ident
    assert exporter.send_thread_ids
    assert all(thread_id == worker_id for thread_id in exporter.send_thread_ids)
    assert exporter.max_in_flight == 1
    assert threading.get_ident() not in exporter.send_thread_ids
    assert d.close(5.0) is True


# --- transport 失败隔离 ---------------------------------------------------


def test_transport_failure_isolated_worker_survives_and_continues(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    exporter.send_fail_first = 1
    d = dispatcher_factory(exporter=exporter, capacity=16)
    assert d.observe_completed_span(make_record(attributes={"plan_id": "a"})) is True
    assert d.observe_completed_span(make_record(attributes={"plan_id": "b"})) is True
    # observe 从不抛 transport 异常：send 只在 worker 线程执行
    assert d.flush(5.0) is True
    health = d.health()
    assert health.attempted_total == 2
    assert health.sent_total == 1
    assert health.failed_total == 1
    assert health.dropped_total == 1
    assert health.last_safe_error_code == TRACE_EXPORT_TRANSPORT_FAILED
    assert health.state is TraceExportDispatcherState.RUNNING
    assert len(exporter.received) == 1
    assert exporter.received[0].attributes["plan_id"] == "b"
    assert d.close(5.0) is True


# --- flush barrier --------------------------------------------------------


def test_flush_barrier_does_not_wait_for_accepts_after_capture(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=16)
    exporter.block_event = threading.Event()
    # A：worker 阻塞在 send(A)
    assert d.observe_completed_span(make_record(attributes={"plan_id": "A"})) is True
    assert exporter.block_started.wait(5.0)
    flush_value: dict[str, bool] = {}

    def do_flush() -> None:
        flush_value["result"] = d.flush(5.0)

    thread = threading.Thread(target=do_flush, daemon=True)
    thread.start()
    # 释放 A：flush 的 barrier（target=1）达成
    exporter.block_event.set()
    thread.join(5.0)
    assert flush_value["result"] is True
    # B 在 flush barrier 之后才 accepted：不释放，证明 flush 成功不依赖 B
    exporter.block_event = threading.Event()
    exporter.block_started.clear()
    assert d.observe_completed_span(make_record(attributes={"plan_id": "B"})) is True
    assert exporter.block_started.wait(5.0)
    health = d.health()
    assert health.attempted_total == 2
    assert health.sent_total == 1
    exporter.block_event.set()
    assert d.close(5.0) is True
    assert len(exporter.received) == 2


def test_flush_counts_completion_for_success_and_failure(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    exporter.send_fail_first = 1
    d = dispatcher_factory(exporter=exporter, capacity=8)
    assert d.observe_completed_span(make_record()) is True
    assert d.observe_completed_span(make_record()) is True
    # send 失败也计为完成：flush 只需 attempt 结束
    assert d.flush(5.0) is True
    assert d.close(5.0) is True


def test_flush_timeout_truthful_worker_continues(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=8)
    exporter.block_event = threading.Event()
    assert d.observe_completed_span(make_record()) is True
    assert exporter.block_started.wait(5.0)
    assert d.flush(0.1) is False
    health = d.health()
    assert health.flush_failures == 1
    assert health.last_safe_error_code == TRACE_EXPORT_FLUSH_TIMEOUT
    assert health.state is TraceExportDispatcherState.RUNNING
    exporter.block_event.set()
    assert d.flush(5.0) is True
    assert d.close(5.0) is True


# --- close ----------------------------------------------------------------


def test_normal_close_drains_pending_then_adapter_close_once(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=8)
    assert d.observe_completed_span(make_record(attributes={"plan_id": "c1"})) is True
    assert d.observe_completed_span(make_record(attributes={"plan_id": "c2"})) is True
    assert d.close(5.0) is True
    assert exporter.close_calls == 1
    assert len(exporter.received) == 2
    health = d.health()
    assert health.state is TraceExportDispatcherState.CLOSED
    assert health.accepted_total == 2
    assert health.sent_total == 2
    d._worker.join(5.0)
    assert d._worker.is_alive() is False


def test_close_stops_new_submission(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=8)
    assert d.observe_completed_span(make_record()) is True
    assert d.close(5.0) is True
    assert d.observe_completed_span(make_record()) is False
    health = d.health()
    assert health.accepted_total == 1
    assert health.dropped_total == 1
    assert health.last_safe_error_code == TRACE_EXPORT_CLOSED


def test_close_idempotent_single_physical_adapter_close(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=8)
    assert d.close(5.0) is True
    assert exporter.close_calls == 1
    assert d.close(5.0) is True
    assert d.close(5.0) is True
    assert exporter.close_calls == 1
    assert d.health().state is TraceExportDispatcherState.CLOSED


def test_adapter_close_false_returns_false_closed_degraded(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    exporter.close_result = False
    d = dispatcher_factory(exporter=exporter, capacity=8)
    assert d.close(5.0) is False
    health = d.health()
    assert health.state is TraceExportDispatcherState.CLOSED
    assert health.close_failures == 1
    assert health.last_safe_error_code == TRACE_EXPORT_CLOSE_FAILED
    assert d.close(5.0) is False
    assert exporter.close_calls == 1


def test_adapter_close_exception_content_free(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    exporter.close_exception = RuntimeError("raw secret must not escape")
    d = dispatcher_factory(exporter=exporter, capacity=8)
    assert d.close(5.0) is False
    health = d.health()
    assert health.state is TraceExportDispatcherState.CLOSED
    assert health.close_failures == 1
    assert health.last_safe_error_code == TRACE_EXPORT_CLOSE_FAILED
    assert "raw secret must not escape" not in repr(health)
    assert "raw secret must not escape" not in str(health)
    assert d.close(5.0) is False
    assert exporter.close_calls == 1


def test_close_timeout_worker_blocked_then_later_close_continues(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=8)
    exporter.block_event = threading.Event()
    assert d.observe_completed_span(make_record()) is True
    assert exporter.block_started.wait(5.0)
    assert d.close(0.2) is False
    health = d.health()
    assert health.state is TraceExportDispatcherState.CLOSING
    assert health.close_failures == 0
    assert d._worker.is_alive()
    # 释放 worker：第二次 close 继续 bounded wait 并完成
    exporter.block_event.set()
    assert d.close(5.0) is True
    assert exporter.close_calls == 1
    assert d.health().state is TraceExportDispatcherState.CLOSED


# --- 并发 close -----------------------------------------------------------


def test_concurrent_close_single_physical_close_single_adapter_close(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=16)
    for index in range(10):
        assert d.observe_completed_span(
            make_record(attributes={"plan_id": f"cc{index}"})
        ) is True
    results: list[bool] = []
    results_lock = threading.Lock()

    def closer() -> None:
        with results_lock:
            results.append(d.close(5.0))

    callers = [threading.Thread(target=closer) for _ in range(4)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(10.0)
    assert exporter.close_calls == 1
    assert results and all(result is True for result in results)
    assert d.health().state is TraceExportDispatcherState.CLOSED
    assert len(exporter.received) == 10


# --- 状态 / FAILED --------------------------------------------------------


def test_internal_worker_fatal_transitions_failed_and_rejects_submission(dispatcher_factory) -> None:
    class FatalExporter(RecordingExporter):
        def send(self, envelope: TraceExportEnvelope) -> None:
            raise SystemExit("synthetic worker invariant failure")

    exporter = FatalExporter()
    d = dispatcher_factory(exporter=exporter, capacity=8)
    assert d.observe_completed_span(make_record()) is True
    deadline = time.monotonic() + 5.0
    while (
        d.health().state is not TraceExportDispatcherState.FAILED
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    health = d.health()
    assert health.state is TraceExportDispatcherState.FAILED
    assert health.last_safe_error_code == TRACE_EXPORT_WORKER_FAILED
    # 新 submission 被拒绝（不抛入 Runtime caller）
    assert d.observe_completed_span(make_record()) is False
    assert d.health().dropped_total == 1
    assert d.health().last_safe_error_code == TRACE_EXPORT_CLOSED
    # best-effort close：worker 已死，truthful 失败且不标记 CLOSED
    assert d.close(1.0) is False
    assert d.health().state is TraceExportDispatcherState.FAILED
    assert d.health().close_failures == 1
    assert d.close(1.0) is False
    assert d.health().close_failures == 1


# --- health ---------------------------------------------------------------


def test_health_snapshot_immutable_content_free(dispatcher_factory) -> None:
    exporter = RecordingExporter()
    d = dispatcher_factory(exporter=exporter, capacity=8)
    record = make_record(
        attributes={"plan_id": "plan-y", "session_id": MAKER_MARKER}
    )
    assert d.observe_completed_span(record) is True
    assert d.flush(5.0) is True
    health = d.health()
    assert isinstance(health, TraceExportHealthSnapshot)
    assert health.state is TraceExportDispatcherState.RUNNING
    assert health.queue_capacity == 8
    assert health.queue_depth == 0
    assert health.accepted_total == 1
    assert health.attempted_total == 1
    assert health.sent_total == 1
    assert health.dropped_total == 0
    assert health.failed_total == 0
    assert health.last_safe_error_code is None
    with pytest.raises(AttributeError):
        health.sent_total = 99  # type: ignore[misc]
    text = repr(health) + str(health)
    for forbidden in (MAKER_MARKER, "plan-y", "run-d-1", "span-d-1"):
        assert forbidden not in text
    d.close(5.0)


# --- forbidden feature 审计 ------------------------------------------------


def test_phase31_introduces_no_forbidden_features() -> None:
    root = Path(__file__).resolve().parents[1]
    targets = (
        "core/runtime/trace_exporter.py",
        "core/runtime/trace_export_dispatcher.py",
    )
    forbidden_tokens = (
        "httpx",
        "requests",
        "aiohttp",
        "urllib",
        "socket",
        "AgentEvalOps",
        "opentelemetry",
        "OpenTelemetryCompatibleSpanAdapter",
        "export_snapshot",
        "RetryExecutor",
        "RetryPolicy",
    )
    for rel in targets:
        source = (root / rel).read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in source, f"forbidden token {token!r} found in {rel}"
        assert "time.sleep" not in source
        assert "def retry" not in source


def test_worker_fatal_abandoned_drop_authoritative_health_exactly_once() -> None:
    """P1-03：worker fatal 时 abandoned pending items 计入权威 health.dropped_total。

    2 个 accepted：第 1 个在 send 中 SystemExit（attempted=1、in-flight 已出队），
    第 2 个仍 pending（queue qsize=1）。finalize 后权威 dropped_total 与 metric
    描述同一个 abandoned 数量；重复 close/health 不重复计数。
    """

    class FatalExporter(RecordingExporter):
        def send(self, envelope: TraceExportEnvelope) -> None:
            raise SystemExit("synthetic worker invariant failure")

    exporter = FatalExporter()
    d = TraceExportDispatcher(exporter=exporter, queue_capacity=8)
    assert d.observe_completed_span(make_record(attributes={"plan_id": "f1"})) is True
    assert d.observe_completed_span(make_record(attributes={"plan_id": "f2"})) is True
    deadline = time.monotonic() + 5.0
    while (
        d.health().state is not TraceExportDispatcherState.FAILED
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    health = d.health()
    assert health.state is TraceExportDispatcherState.FAILED
    assert d._worker.is_alive() is False
    assert health.accepted_total == 2
    assert health.attempted_total == 1
    assert health.sent_total == 0
    assert health.queue_depth == 1
    assert d.close(5.0) is False
    health = d.health()
    # 权威 health 包含被真实放弃的 1 个 pending item（不再为零）
    assert health.dropped_total == 1
    assert health.close_failures == 1
    assert health.last_safe_error_code == TRACE_EXPORT_CLOSE_FAILED
    # 幂等：重复 close / health 不重复计数
    assert d.close(5.0) is False
    assert d.health().dropped_total == 1
    assert d.health().close_failures == 1


def test_drop_reason_vocabulary_exactly_seven_with_worker_unavailable() -> None:
    """P1-04：批准 drop 词表恰好 7 个值，含 worker_unavailable；伪造值被拒。

    ``worker_unavailable`` = 唯一 export worker 不可用/已死导致 queue 无法
    drain 的永久放弃；``shutdown_timeout`` 只用于 final shutdown deadline
    实际到期。metrics.py descriptor 通过 import 同一常量自动传播（零修改）。
    """
    from core.runtime.metrics import (
        DEFAULT_RUNTIME_METRIC_REGISTRY,
        InMemoryMetricsRecorder,
    )

    expected = frozenset(
        {
            "projection_failed",
            "incompatible",
            "queue_full",
            "closed",
            "transport_failed",
            "shutdown_timeout",
            "worker_unavailable",
        }
    )
    assert TRACE_EXPORT_DROP_REASONS == expected
    assert len(TRACE_EXPORT_DROP_REASONS) == 7
    dropped = DEFAULT_RUNTIME_METRIC_REGISTRY["runtime_trace_export_dropped_total"]
    assert dropped.bounded_values["reason"] == TRACE_EXPORT_DROP_REASONS
    metrics = InMemoryMetricsRecorder()
    metrics.increment_counter(
        "runtime_trace_export_dropped_total",
        labels={"reason": "worker_unavailable"},
    )
    assert metrics.snapshot().counter(
        "runtime_trace_export_dropped_total", {"reason": "worker_unavailable"}
    ) == 1
    with pytest.raises(ValueError):
        metrics.increment_counter(
            "runtime_trace_export_dropped_total",
            labels={"reason": "RAW_FABRICATED_REASON"},
        )
