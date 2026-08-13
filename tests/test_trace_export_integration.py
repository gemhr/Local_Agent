#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP4-B Phase 3.2：Span completion observer 集成测试。

真实 InMemorySpanRecorder + 真实 TraceExportDispatcher + fake TraceExporter
的端到端验证。只使用 safe synthetic IDs 与合成 marker；不携带真实密钥或业务
正文。覆盖：基础 observer、锁边界、完成顺序、单次通知、observer 失败隔离、
noop span、real recorder→dispatcher 集成、raw 边界、projection 拒绝、close
final spans、producer barrier、cross-thread、close/completion race、禁用
observer 与 forbidden-feature 审计。
"""

from __future__ import annotations

import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.runtime.tracing import InMemorySpanRecorder, SpanRecord, SpanStatus
from core.runtime.trace_contract import RUNTIME_RUN_SPAN
from core.runtime.trace_export_contract import TraceExportEnvelope
from core.runtime.trace_export_dispatcher import (
    TRACE_EXPORT_PROJECTION_FAILED,
    TraceExportDispatcher,
)

MAKER_MARKER = "MARKER_SESSION_9F31"
RAW_MARKER = "MARKER_PROMPT_9F31"


class RecordingExporter:
    """记录每个 send 输入（typed assert）、事件顺序与 close 次数。"""

    def __init__(self) -> None:
        self.received: list[TraceExportEnvelope] = []
        self.events: list[tuple[object, ...]] = []
        self.close_calls = 0

    def send(self, envelope: object) -> None:
        assert isinstance(envelope, TraceExportEnvelope)
        assert not isinstance(envelope, SpanRecord)
        assert not isinstance(envelope, dict)
        self.received.append(envelope)
        self.events.append(
            ("send", envelope.span_id, envelope.status.value, envelope.error_code)
        )

    def close(self, timeout_seconds: float) -> bool:
        self.close_calls += 1
        self.events.append(("close",))
        return True


@pytest.fixture
def dispatchers():
    instances: list[TraceExportDispatcher] = []

    def factory(
        exporter: object | None = None, capacity: int = 16
    ) -> TraceExportDispatcher:
        value = TraceExportDispatcher(
            exporter=exporter if exporter is not None else RecordingExporter(),
            queue_capacity=capacity,
        )
        instances.append(value)
        return value

    yield factory
    for value in instances:
        value.close(5.0)


def start_span(recorder: InMemorySpanRecorder, *, trace_id: str, run_id: str, index: int, operation: str = RUNTIME_RUN_SPAN):
    return recorder.start_span(
        trace_id=trace_id,
        run_id=run_id,
        component="integration",
        operation=operation,
    )


# --- 基础 observer --------------------------------------------------------


def test_observer_absent_preserves_existing_recorder_behavior() -> None:
    recorder = InMemorySpanRecorder()
    assert recorder._completion_observer is None
    handle = start_span(recorder, trace_id="t-a", run_id="r-a", index=0)
    handle.end_ok()
    health = recorder.health_snapshot()
    assert len(recorder.snapshot()) == 1
    assert health.completed_span_count == 1
    assert health.dropped_span_count == 0
    assert health.status == "HEALTHY"


def test_observer_receives_successful_completion() -> None:
    seen: list[SpanRecord] = []
    recorder = InMemorySpanRecorder(completion_observer=seen.append)
    handle = start_span(recorder, trace_id="t-b", run_id="r-b", index=0)
    handle.end_ok()
    assert len(seen) == 1
    assert seen[0].status is SpanStatus.OK
    assert seen[0].span_id == handle.context.span_id


def test_observer_invoked_after_local_record() -> None:
    observed_after_local = threading.Event()
    recorder: InMemorySpanRecorder | None = None

    def observer(record: SpanRecord) -> None:
        # local completed record 必须先出现在 snapshot（且锁已释放，可重入）
        assert record in recorder.snapshot()  # type: ignore[union-attr]
        observed_after_local.set()

    recorder = InMemorySpanRecorder(completion_observer=observer)
    handle = start_span(recorder, trace_id="t-c", run_id="r-c", index=0)
    handle.end_ok()
    assert observed_after_local.wait(5.0)
    assert len(recorder.snapshot()) == 1


def test_observer_exactly_once_first_end_wins() -> None:
    seen: list[SpanRecord] = []
    recorder = InMemorySpanRecorder(completion_observer=seen.append)
    handle = start_span(recorder, trace_id="t-d", run_id="r-d", index=0)
    handle.end_ok()
    handle.end_error("LATE_ERROR")
    handle.end_cancelled("LATE_CANCELLED")
    assert len(seen) == 1
    assert seen[0].status is SpanStatus.OK
    assert len(recorder.snapshot()) == 1


def test_observer_failure_isolated_and_span_lifecycle_continues() -> None:
    calls = 0

    def failing_observer(record: SpanRecord) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError(RAW_MARKER)

    recorder = InMemorySpanRecorder(completion_observer=failing_observer)
    handle = start_span(recorder, trace_id="t-e", run_id="r-e", index=0)
    record = handle.end_ok()  # 不传播 observer 异常
    assert record is not None
    assert record in recorder.snapshot()
    assert recorder.health_snapshot().active_span_count == 0
    assert calls == 1
    # 后续 span lifecycle 继续工作
    second = start_span(recorder, trace_id="t-e", run_id="r-e", index=1)
    second.end_error("SECOND_ERROR")
    assert calls == 2
    assert len(recorder.snapshot()) == 2
    # recorder health/error 输出不含 raw marker
    assert RAW_MARKER not in repr(recorder.health_snapshot())


def test_observer_lock_release_deterministic() -> None:
    released = threading.Event()
    recorder: InMemorySpanRecorder | None = None

    def observer(record: SpanRecord) -> None:
        # 若 observer 在 recorder lock 内执行，snapshot() 会 self-deadlock，
        # released 永不 set → 测试 bounded 失败；锁外则成功。
        recorder.snapshot()  # type: ignore[union-attr]
        recorder.health_snapshot()  # type: ignore[union-attr]
        released.set()

    recorder = InMemorySpanRecorder(completion_observer=observer)
    handle = start_span(recorder, trace_id="t-f", run_id="r-f", index=0)
    handle.end_ok()
    assert released.wait(5.0)


def test_noop_span_never_notifies_observer() -> None:
    seen: list[SpanRecord] = []
    recorder = InMemorySpanRecorder(completion_observer=seen.append)
    recorder.close()
    noop = start_span(recorder, trace_id="t-g", run_id="r-g", index=0)
    assert noop.context is None
    noop.end_ok()
    noop.end_error()
    assert seen == []
    assert recorder.health_snapshot().dropped_span_count == 1  # recorder_start_failed


# --- real recorder -> dispatcher 集成 -------------------------------------


def test_real_recorder_to_dispatcher_integration(dispatchers) -> None:
    exporter = RecordingExporter()
    dispatcher = dispatchers(exporter=exporter, capacity=8)
    recorder = InMemorySpanRecorder(completion_observer=dispatcher.observe_completed_span)
    handle = start_span(recorder, trace_id="t-h", run_id="r-h", index=0)
    handle.end_ok()
    assert len(recorder.snapshot()) == 1
    assert dispatcher.flush(5.0) is True
    assert len(exporter.received) == 1
    envelope = exporter.received[0]
    assert isinstance(envelope, TraceExportEnvelope)
    assert envelope.span_id == handle.context.span_id
    assert envelope.status is SpanStatus.OK


def test_integration_internal_marker_absent_from_envelope(dispatchers) -> None:
    exporter = RecordingExporter()
    dispatcher = dispatchers(exporter=exporter, capacity=8)
    recorder = InMemorySpanRecorder(completion_observer=dispatcher.observe_completed_span)
    handle = start_span(recorder, trace_id="t-i", run_id="r-i", index=0)
    handle.set_safe_attribute("session_id", MAKER_MARKER)
    handle.set_safe_attribute("plan_id", "plan-x")
    handle.end_ok()
    assert dispatcher.flush(5.0) is True
    envelope = exporter.received[0]
    assert isinstance(envelope, TraceExportEnvelope)
    assert envelope.attributes["plan_id"] == "plan-x"
    assert "session_id" not in envelope.attributes
    assert MAKER_MARKER not in repr(envelope.attributes)


def test_integration_projection_rejection_preserves_local_record(dispatchers) -> None:
    exporter = RecordingExporter()
    dispatcher = dispatchers(exporter=exporter, capacity=8)
    recorder = InMemorySpanRecorder(completion_observer=dispatcher.observe_completed_span)
    handle = start_span(recorder, trace_id="t-j", run_id="r-j", index=0, operation="unknown.op")
    record = handle.end_ok()
    # 本地 recorder 仍记录（recorder 不校验 operation）
    assert record in recorder.snapshot()
    assert dispatcher.flush(5.0) is True
    health = dispatcher.health()
    assert health.accepted_total == 0
    assert health.dropped_total == 1
    assert health.last_safe_error_code == TRACE_EXPORT_PROJECTION_FAILED
    assert exporter.received == []
    assert len(recorder.snapshot()) == 1


def test_integration_dispatcher_closed_local_record_preserved(dispatchers) -> None:
    exporter = RecordingExporter()
    dispatcher = dispatchers(exporter=exporter, capacity=8)
    recorder = InMemorySpanRecorder(completion_observer=dispatcher.observe_completed_span)
    # 非生产顺序：先关 dispatcher，再完成 recorder span；必须 failure-safe
    assert dispatcher.close(5.0) is True
    handle = start_span(recorder, trace_id="t-k", run_id="r-k", index=0)
    record = handle.end_ok()
    assert record in recorder.snapshot()
    health = dispatcher.health()
    assert health.dropped_total == 1
    assert health.last_safe_error_code == "TRACE_EXPORT_CLOSED"
    assert exporter.received == []


def test_adapter_receives_envelope_only_typed_assert(dispatchers) -> None:
    exporter = RecordingExporter()
    dispatcher = dispatchers(exporter=exporter, capacity=8)
    recorder = InMemorySpanRecorder(completion_observer=dispatcher.observe_completed_span)
    for index in range(5):
        handle = start_span(recorder, trace_id="t-l", run_id="r-l", index=index)
        handle.end_ok()
    assert dispatcher.flush(5.0) is True
    assert len(exporter.received) == 5
    assert all(isinstance(item, TraceExportEnvelope) for item in exporter.received)
    assert all(not isinstance(item, (SpanRecord, dict)) for item in exporter.received)


# --- close final spans / producer barrier ---------------------------------


def test_close_generated_final_spans_reach_dispatcher(dispatchers) -> None:
    exporter = RecordingExporter()
    dispatcher = dispatchers(exporter=exporter, capacity=16)
    recorder = InMemorySpanRecorder(completion_observer=dispatcher.observe_completed_span)
    handles = [
        start_span(recorder, trace_id="t-m", run_id="r-m", index=index) for index in range(3)
    ]
    recorder.close()
    # 每个 active Span -> CANCELLED / RECORDER_CLOSED（既有 drop 契约保持）
    health = recorder.health_snapshot()
    assert health.active_span_count == 0
    assert health.dropped_span_count == 3
    # dispatcher 收到 3 个 final envelopes 并最终 attempt
    assert dispatcher.flush(5.0) is True
    assert len(exporter.received) == 3
    for envelope in exporter.received:
        assert envelope.status is SpanStatus.CANCELLED
        assert envelope.error_code == "RECORDER_CLOSED"
        assert envelope.span_id in {handle.context.span_id for handle in handles}
    assert dispatcher.health().sent_total == 3


def test_recorder_close_establishes_producer_barrier() -> None:
    seen: list[SpanRecord] = []
    recorder = InMemorySpanRecorder(completion_observer=seen.append)
    handles = [
        start_span(recorder, trace_id="t-n", run_id="r-n", index=index) for index in range(4)
    ]
    recorder.close()
    # close() 返回时所有 close-start active handles 已同步完成本地处理 + observer
    assert len(seen) == 4
    assert {record.span_id for record in seen} == {
        handle.context.span_id for handle in handles
    }
    assert all(record.status is SpanStatus.CANCELLED for record in seen)
    assert all(record.error_code == "RECORDER_CLOSED" for record in seen)


def test_recorder_close_before_dispatcher_send_before_adapter_close(dispatchers) -> None:
    exporter = RecordingExporter()
    dispatcher = dispatchers(exporter=exporter, capacity=8)
    recorder = InMemorySpanRecorder(completion_observer=dispatcher.observe_completed_span)
    handle = start_span(recorder, trace_id="t-o", run_id="r-o", index=0)
    recorder.close()  # 收口 -> final observer submission
    dispatcher.close(5.0)  # final drain -> adapter close
    assert dispatcher.health().state.value == "CLOSED"
    assert exporter.close_calls == 1
    assert len(exporter.received) == 1
    # adapter send 先于 adapter close；无 accepted final envelope 丢失
    assert exporter.events[-1] == ("close",)
    send_events = [event for event in exporter.events if event[0] == "send"]
    assert len(send_events) == 1
    assert send_events[0][2] == SpanStatus.CANCELLED.value
    assert send_events[0][3] == "RECORDER_CLOSED"
    assert dispatcher.health().sent_total == 1


# --- concurrency ----------------------------------------------------------


def test_cross_thread_completion_with_real_dispatcher(dispatchers) -> None:
    exporter = RecordingExporter()
    dispatcher = dispatchers(exporter=exporter, capacity=1024)
    recorder = InMemorySpanRecorder(completion_observer=dispatcher.observe_completed_span)
    threads = 8
    per_thread = 20

    def complete() -> None:
        for index in range(per_thread):
            handle = start_span(
                recorder, trace_id="t-p", run_id="r-p", index=index
            )
            handle.end_ok()

    workers = [threading.Thread(target=complete) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10.0)
    assert not any(worker.is_alive() for worker in workers)
    total = threads * per_thread
    assert len(recorder.snapshot()) == total
    assert dispatcher.flush(5.0) is True
    health = dispatcher.health()
    assert health.accepted_total == total
    assert health.attempted_total == total
    assert health.sent_total == total
    assert len(exporter.received) == total
    assert all(isinstance(item, TraceExportEnvelope) for item in exporter.received)


def test_close_completion_race_single_observer_single_attempt(dispatchers) -> None:
    exporter = RecordingExporter()
    dispatcher = dispatchers(exporter=exporter, capacity=8)
    recorder = InMemorySpanRecorder(completion_observer=dispatcher.observe_completed_span)
    handle = start_span(recorder, trace_id="t-q", run_id="r-q", index=0)
    span_id = handle.context.span_id
    barrier = threading.Barrier(2)

    def ender() -> None:
        barrier.wait()
        handle.end_ok()

    def closer() -> None:
        barrier.wait()
        recorder.close()

    ender_thread = threading.Thread(target=ender)
    closer_thread = threading.Thread(target=closer)
    ender_thread.start()
    closer_thread.start()
    ender_thread.join(5.0)
    closer_thread.join(5.0)
    assert not ender_thread.is_alive()
    assert not closer_thread.is_alive()
    # 每 Span 恰好一次 observer 通知、一次 envelope attempt，无重复
    assert dispatcher.flush(5.0) is True
    health = dispatcher.health()
    assert health.accepted_total == 1
    assert health.attempted_total == 1
    assert len([item for item in exporter.received if item.span_id == span_id]) == 1
    # 本地事实一致：completed 或 closed-drop 恰好一种，无重复记录
    snapshot_ids = [record.span_id for record in recorder.snapshot()]
    assert snapshot_ids.count(span_id) <= 1


# --- forbidden feature 审计 ------------------------------------------------


def test_phase32_introduces_no_forbidden_features() -> None:
    root = Path(__file__).resolve().parents[1]
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
    # Phase 3.1 新增生产文件整体审计（无既有豁免）
    for rel in (
        "core/runtime/trace_exporter.py",
        "core/runtime/trace_export_dispatcher.py",
    ):
        source = (root / rel).read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in source, f"forbidden token {token!r} in {rel}"
    # Phase 3.2 生产 diff（tracing.py / __init__.py）只审计新增行：
    # 既有 OTel-shaped helper 已存在于 tracing.py，不在本次 diff 内。
    # Windows GBK 环境下必须显式 UTF-8 解码 git 输出（strict），否则
    # reader thread 抛 UnicodeDecodeError（TEST-GAP-01 修复）。
    diff = subprocess.run(
        ["git", "diff", "-U0", "--", "core/runtime/tracing.py", "core/runtime/__init__.py"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        cwd=root,
        check=False,
    )
    added_lines = [
        line[1:] for line in diff.stdout.splitlines() if line.startswith("+")
    ]
    added = "\n".join(added_lines)
    for token in forbidden_tokens:
        assert token not in added, f"forbidden token {token!r} in Phase 3.2 diff"
    # 无 generic multi-observer / fan-out
    tracing_source = (root / "core/runtime/tracing.py").read_text(encoding="utf-8")
    for token in ("add_observer", "remove_observer", "CompositeSpanSink", "observer_registry"):
        assert token not in tracing_source
    assert "_observers" not in tracing_source
    assert tracing_source.count("_completion_observer") >= 1
    # recorder 不拥有 transport/queue/worker
    for token in ("queue.Queue", "threading.Thread", "import httpx"):
        assert token not in added
