#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TraceExportDispatcher：application-scoped、thread-safe、bounded export 分发器。

WP4-B Phase 3.1 核心实现。Dispatcher 是 ``project_span()`` invocation、
``TraceCompatibilityEvaluator`` consumption、bounded ``queue.Queue``、单
daemon worker、submission/drop 计数、health、flush/close 的唯一 WP4-B Owner。

- producer 路径（``observe_completed_span``）只做 type check、projection、
  compatibility、状态/计数同步与 ``put_nowait``：无 I/O、sleep、await、
  blocking put、worker join、serialization 或 exporter 调用。
- worker 是 dispatcher 拥有的唯一 adapter 调用者：串行 ``send``，每个
  accepted envelope 至多一次 transport attempt；adapter 普通异常只记录
  safe failure 并继续，只有 worker 内部不变量失败才进入 ``FAILED``。
- delivery = ``BEST_EFFORT``；accepted envelope =
  ``AT_MOST_ONE_TRANSPORT_ATTEMPT_PER_ACCEPTED_ENVELOPE``；不代表
  at-most-once remote delivery。
- 可选注入 ``metrics_recorder``：dispatcher 事件/状态的最小 best-effort 投影
  （health counters 仍是权威内部事实）；recorder 缺失或失败不影响任何语义。
- 本模块不含 transport、vendor、HTTP、generic serialization、durability 或
  Recovery 语义；不读取环境变量、不 import Settings。
"""

from __future__ import annotations

import math
import queue
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum

from core.runtime.tracing import SpanRecord
from core.runtime.trace_export_contract import (
    TraceCompatibilityEvaluator,
    TraceExportEnvelope,
    project_span,
)

# --- 固定 content-free safe error codes（bounded vocabulary） --------------
TRACE_EXPORT_PROJECTION_FAILED = "TRACE_EXPORT_PROJECTION_FAILED"
TRACE_EXPORT_INCOMPATIBLE = "TRACE_EXPORT_INCOMPATIBLE"
TRACE_EXPORT_QUEUE_FULL = "TRACE_EXPORT_QUEUE_FULL"
TRACE_EXPORT_CLOSED = "TRACE_EXPORT_CLOSED"
TRACE_EXPORT_TRANSPORT_FAILED = "TRACE_EXPORT_TRANSPORT_FAILED"
TRACE_EXPORT_WORKER_FAILED = "TRACE_EXPORT_WORKER_FAILED"
TRACE_EXPORT_FLUSH_TIMEOUT = "TRACE_EXPORT_FLUSH_TIMEOUT"
TRACE_EXPORT_CLOSE_FAILED = "TRACE_EXPORT_CLOSE_FAILED"

# --- 有限 metric reason/stage 词表（dispatcher 是唯一 semantic Owner） ------
# reason 词表：projection_failed / incompatible / queue_full / closed /
# transport_failed / shutdown_timeout（final shutdown deadline 实际到期导致
# queued envelope 被永久放弃）/ worker_unavailable（唯一 export worker 不可用/
# 已死，queue 无法再 drain，queued envelope 在 finalization 中被永久放弃）。
# stage 词表：failure stage 回答"哪个组件失败"，与 drop reason 是互补事实。
TRACE_EXPORT_DROP_REASONS = frozenset(
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
TRACE_EXPORT_FAILURE_STAGES = frozenset(
    {
        "projection",
        "compatibility",
        "transport",
        "worker",
        "flush",
        "close",
    }
)

# --- exporter metrics 名称（单 owner；metrics.py descriptor 引用同一常量） --
TRACE_EXPORT_METRIC_ACCEPTED_TOTAL = "runtime_trace_export_accepted_total"
TRACE_EXPORT_METRIC_ATTEMPTED_TOTAL = "runtime_trace_export_attempted_total"
TRACE_EXPORT_METRIC_SENT_TOTAL = "runtime_trace_export_sent_total"
TRACE_EXPORT_METRIC_DROPPED_TOTAL = "runtime_trace_export_dropped_total"
TRACE_EXPORT_METRIC_FAILURES_TOTAL = "runtime_trace_export_failures_total"
TRACE_EXPORT_METRIC_QUEUE_DEPTH = "runtime_trace_export_queue_depth"
TRACE_EXPORT_METRIC_FLUSH_DURATION_SECONDS = (
    "runtime_trace_export_flush_duration_seconds"
)

# --- delivery 语义（内部文档/审计用；不是 delivery guarantee） -------------
DELIVERY_SEMANTICS = "BEST_EFFORT"
TRANSPORT_ATTEMPT_SEMANTICS = "AT_MOST_ONE_TRANSPORT_ATTEMPT_PER_ACCEPTED_ENVELOPE"

_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class TraceExportDispatcherState(str, Enum):
    """Dispatcher 显式有限状态机。"""

    RUNNING = "RUNNING"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class TraceExportHealthSnapshot:
    """不可变、content-free 的 dispatcher 运行期 health 快照。

    只包含 state、queue 深度/容量与聚合计数，以及 bounded safe error code；
    不含 span/envelope ID、fingerprint、endpoint、exception 或任何正文。
    """

    state: TraceExportDispatcherState
    queue_depth: int
    queue_capacity: int
    accepted_total: int
    attempted_total: int
    sent_total: int
    dropped_total: int
    failed_total: int
    flush_failures: int
    close_failures: int
    last_safe_error_code: str | None

    def __post_init__(self) -> None:
        for name in (
            "queue_depth",
            "queue_capacity",
            "accepted_total",
            "attempted_total",
            "sent_total",
            "dropped_total",
            "failed_total",
            "flush_failures",
            "close_failures",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.queue_depth > self.queue_capacity:
            raise ValueError("queue_depth must not exceed queue_capacity")
        if self.last_safe_error_code is not None and (
            not isinstance(self.last_safe_error_code, str)
            or not _SAFE_ERROR_CODE.fullmatch(self.last_safe_error_code)
        ):
            raise ValueError("last_safe_error_code must be a safe identifier")


def _validate_timeout(timeout_seconds: float) -> float:
    """flush/close 的 bounded deadline 输入校验：finite、非负、拒绝 bool。"""
    if isinstance(timeout_seconds, bool) or not isinstance(
        timeout_seconds, (int, float)
    ):
        raise TypeError("timeout_seconds must be a number")
    if not math.isfinite(timeout_seconds):
        raise ValueError("timeout_seconds must be finite")
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must be non-negative")
    return float(timeout_seconds)


class TraceExportDispatcher:
    """Bounded、非阻塞、application-scoped 的 Trace export 分发器。

    构造即启动唯一 daemon worker 并进入 ``RUNNING``；worker 启动失败则构造
    抛错，不返回半初始化对象。``close`` bounded/idempotent，物理 lifecycle
    只执行一次（单 sentinel、单 adapter close）。
    """

    def __init__(
        self,
        *,
        exporter: object,
        queue_capacity: int,
        metrics_recorder: object | None = None,
    ) -> None:
        if not callable(getattr(exporter, "send", None)):
            raise TypeError("exporter must provide a callable send()")
        if not callable(getattr(exporter, "close", None)):
            raise TypeError("exporter must provide a callable close()")
        if isinstance(queue_capacity, bool) or not isinstance(queue_capacity, int):
            raise TypeError("queue_capacity must be an int")
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be greater than 0")
        self._exporter = exporter
        self._metrics_recorder = metrics_recorder
        self._queue_capacity = queue_capacity
        self._queue: queue.Queue[object] = queue.Queue(maxsize=queue_capacity)
        self._condition = threading.Condition()
        self._state = TraceExportDispatcherState.RUNNING
        self._accepted_total = 0
        self._attempted_total = 0
        self._sent_total = 0
        self._dropped_total = 0
        self._failed_total = 0
        self._flush_failures = 0
        self._close_failures = 0
        self._completed_attempt_count = 0
        self._last_safe_error_code: str | None = None
        # lifecycle control（非 export data path）
        self._sentinel = object()
        self._close_started = False
        self._sentinel_enqueued = False
        self._close_target = 0
        self._adapter_close_timeout_remaining = 0.0
        self._close_result: bool | None = None
        self._close_finalized = False
        self._worker = threading.Thread(
            target=self._worker_main, name="trace-export-worker", daemon=True
        )
        self._worker.start()

    # --- metrics projection（best-effort；failure 只作为诊断） -------------

    def _metric(
        self, method: str, name: str, *args: object, **kwargs: object
    ) -> None:
        """把 dispatcher 事件投影为指标；recorder 缺失或失败均不影响语义。

        health counters 仍是权威内部事实；metrics 只是 projection，不构成
        第二套计数状态机。任何 recorder 异常都被隔离，不进入 producer/
        worker/lifecycle 路径。
        """
        recorder = self._metrics_recorder
        if recorder is None:
            return
        try:
            fn = getattr(recorder, method)
            fn(name, *args, **kwargs)
        except Exception:
            pass

    # --- producer path ------------------------------------------------------

    def observe_completed_span(self, record: SpanRecord) -> bool:
        """把内部已完成 ``SpanRecord`` 投影/校验后非阻塞入队；返回是否被接受。

        ``True`` 只表示一个合法且当前兼容的 ``TraceExportEnvelope`` 已进入
        本进程 queue（在此 state/submission 锁内完成）；不代表 attempted、
        sent 或 delivered。``False`` 表示 projection/compatibility/state/
        queue-full 拒绝，绝不向 Runtime caller 抛异常。producer 路径不做任何
        I/O、sleep、retry 或 blocking put。
        """
        if not isinstance(record, SpanRecord):
            return False
        try:
            envelope = project_span(record)
        except Exception:
            with self._condition:
                self._dropped_total += 1
                self._last_safe_error_code = TRACE_EXPORT_PROJECTION_FAILED
            self._metric(
                "increment_counter",
                TRACE_EXPORT_METRIC_DROPPED_TOTAL,
                labels={"reason": "projection_failed"},
            )
            return False
        try:
            decision = TraceCompatibilityEvaluator.evaluate(envelope)
        except Exception:
            with self._condition:
                self._dropped_total += 1
                self._last_safe_error_code = TRACE_EXPORT_INCOMPATIBLE
            self._metric(
                "increment_counter",
                TRACE_EXPORT_METRIC_DROPPED_TOTAL,
                labels={"reason": "incompatible"},
            )
            return False
        if not decision.accepted:
            with self._condition:
                self._dropped_total += 1
                self._last_safe_error_code = TRACE_EXPORT_INCOMPATIBLE
            self._metric(
                "increment_counter",
                TRACE_EXPORT_METRIC_DROPPED_TOTAL,
                labels={"reason": "incompatible"},
            )
            return False
        # submission/state 同步边界：state 检查 + put + accepted 计数在同一锁内，
        # 保证 close barrier 不漏计已 accepted 的 item。
        with self._condition:
            if self._state is not TraceExportDispatcherState.RUNNING:
                self._dropped_total += 1
                self._last_safe_error_code = TRACE_EXPORT_CLOSED
                self._metric(
                    "increment_counter",
                    TRACE_EXPORT_METRIC_DROPPED_TOTAL,
                    labels={"reason": "closed"},
                )
                return False
            try:
                self._queue.put_nowait(envelope)
            except queue.Full:
                self._dropped_total += 1
                self._last_safe_error_code = TRACE_EXPORT_QUEUE_FULL
                self._metric(
                    "increment_counter",
                    TRACE_EXPORT_METRIC_DROPPED_TOTAL,
                    labels={"reason": "queue_full"},
                )
                return False
            self._accepted_total += 1
            self._metric(
                "increment_counter", TRACE_EXPORT_METRIC_ACCEPTED_TOTAL
            )
            self._metric(
                "set_gauge",
                TRACE_EXPORT_METRIC_QUEUE_DEPTH,
                float(self._queue.qsize()),
            )
            return True

    # --- flush --------------------------------------------------------------

    def flush(self, timeout_seconds: float) -> bool:
        """Bounded flush：等待调用时已 accepted 的 envelopes 各自完成其一次
        transport-attempt 处理（成功或 transport 失败），或 deadline 到期。

        成功只表示 barrier 内 items 已完成 attempt 处理，不代表 remote
        durable/ack。timeout 返回 ``False`` 并增加 flush failure，不丢弃或
        重试 item，worker 继续。多个 flush caller 可并发等待各自的 barrier。
        """
        timeout = _validate_timeout(timeout_seconds)
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        with self._condition:
            if self._state is TraceExportDispatcherState.CLOSED:
                succeeded = True
            else:
                target = self._accepted_total
                while (
                    self._completed_attempt_count < target
                    and time.monotonic() < deadline
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(timeout=remaining)
                succeeded = self._completed_attempt_count >= target
        elapsed = max(0.0, time.monotonic() - started)
        # flush 成功与 timeout 均观察实际 bounded 时长（无 labels）。
        self._metric(
            "observe_histogram",
            TRACE_EXPORT_METRIC_FLUSH_DURATION_SECONDS,
            elapsed,
        )
        if not succeeded:
            self._record_flush_timeout()
            return False
        return True

    # --- close --------------------------------------------------------------

    def close(self, timeout_seconds: float) -> bool:
        """Bounded/idempotent close。物理 lifecycle（sentinel + adapter close +
        worker 退出）只执行一次，结果缓存；并发 caller 等待同一结果。"""
        timeout = _validate_timeout(timeout_seconds)
        deadline = time.monotonic() + timeout
        with self._condition:
            if self._close_finalized:
                return self._close_result
            if not self._close_started:
                self._close_started = True
                if (
                    self._state is TraceExportDispatcherState.FAILED
                    and not self._worker.is_alive()
                ):
                    # worker 已因内部致命失败退出，accepted barrier 不可满足。
                    self._finalize_close_dead_worker_locked()
                    return False
                if self._state in (
                    TraceExportDispatcherState.RUNNING,
                    TraceExportDispatcherState.FAILED,
                ):
                    self._state = TraceExportDispatcherState.CLOSING
                self._close_target = self._accepted_total
        # Phase A：等待 final accepted barrier（bounded；worker 死亡即终止）。
        while True:
            with self._condition:
                if (
                    self._close_finalized
                    or self._completed_attempt_count >= self._close_target
                    or not self._worker.is_alive()
                ):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)
        with self._condition:
            if self._close_finalized:
                return self._close_result
            if not self._worker.is_alive():
                # worker 在 barrier 前死亡：close 永久不可完成，truthful 失败。
                self._finalize_close_dead_worker_locked()
                return False
            if self._completed_attempt_count < self._close_target:
                # deadline 到期而 worker 仍存活：degraded/unknown，不标记 CLOSED；
                # 后续 close 调用可继续 bounded wait。
                return False
        # Phase B：只入队一次 control sentinel（lifecycle path 可在剩余 deadline
        # 内等待 queue slot；producer 仍永不 blocking）。
        remaining = deadline - time.monotonic()
        should_enqueue = False
        with self._condition:
            if not self._sentinel_enqueued:
                if remaining <= 0:
                    return False
                self._sentinel_enqueued = True
                self._adapter_close_timeout_remaining = remaining
                should_enqueue = True
        if should_enqueue:
            try:
                self._queue.put(self._sentinel, block=True, timeout=remaining)
            except queue.Full:
                with self._condition:
                    self._sentinel_enqueued = False
                return False
        # Phase C：等待 worker 完成 adapter.close 并 finalize（bounded）。
        with self._condition:
            while not self._close_finalized and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)
            if self._close_finalized:
                return self._close_result
            return False

    # --- health -------------------------------------------------------------

    def health(self) -> TraceExportHealthSnapshot:
        """返回线程安全、不可变、content-free 的 health 快照。"""
        with self._condition:
            return TraceExportHealthSnapshot(
                state=self._state,
                queue_depth=self._queue.qsize(),
                queue_capacity=self._queue_capacity,
                accepted_total=self._accepted_total,
                attempted_total=self._attempted_total,
                sent_total=self._sent_total,
                dropped_total=self._dropped_total,
                failed_total=self._failed_total,
                flush_failures=self._flush_failures,
                close_failures=self._close_failures,
                last_safe_error_code=self._last_safe_error_code,
            )

    # --- worker -------------------------------------------------------------

    def _worker_main(self) -> None:
        try:
            while True:
                item = self._queue.get()
                try:
                    if item is self._sentinel:
                        self._perform_adapter_close()
                        return
                    self._handle_envelope(item)
                finally:
                    self._queue.task_done()
        except BaseException:
            # 只有 dispatcher 内部不变量/loop 失败才进入 FAILED；adapter 普通
            # 异常在 _handle_envelope 内已被隔离，不会到达这里。
            self._mark_worker_failed()

    def _handle_envelope(self, envelope: TraceExportEnvelope) -> None:
        """单个 accepted envelope 的一次 transport attempt；item 被消费。"""
        with self._condition:
            self._attempted_total += 1
        self._metric(
            "increment_counter", TRACE_EXPORT_METRIC_ATTEMPTED_TOTAL
        )
        try:
            self._exporter.send(envelope)
        except Exception:
            with self._condition:
                self._failed_total += 1
                self._dropped_total += 1
                self._last_safe_error_code = TRACE_EXPORT_TRANSPORT_FAILED
            self._metric(
                "increment_counter",
                TRACE_EXPORT_METRIC_DROPPED_TOTAL,
                labels={"reason": "transport_failed"},
            )
            self._metric(
                "increment_counter",
                TRACE_EXPORT_METRIC_FAILURES_TOTAL,
                labels={"stage": "transport"},
            )
        else:
            with self._condition:
                self._sent_total += 1
            self._metric(
                "increment_counter", TRACE_EXPORT_METRIC_SENT_TOTAL
            )
        finally:
            with self._condition:
                self._completed_attempt_count += 1
                self._condition.notify_all()
                self._metric(
                    "set_gauge",
                    TRACE_EXPORT_METRIC_QUEUE_DEPTH,
                    float(self._queue.qsize()),
                )

    def _perform_adapter_close(self) -> None:
        """worker 线程内的物理 adapter close（至多一次），并 finalize 状态。"""
        with self._condition:
            remaining = self._adapter_close_timeout_remaining
        try:
            ok = self._exporter.close(remaining)
        except Exception:
            ok = False
            with self._condition:
                self._close_failures += 1
                self._last_safe_error_code = TRACE_EXPORT_CLOSE_FAILED
        else:
            if not ok:
                with self._condition:
                    self._close_failures += 1
                    self._last_safe_error_code = TRACE_EXPORT_CLOSE_FAILED
        finally:
            with self._condition:
                self._close_result = ok
                self._close_finalized = True
                self._state = TraceExportDispatcherState.CLOSED
                self._condition.notify_all()
                self._metric(
                    "set_gauge",
                    TRACE_EXPORT_METRIC_QUEUE_DEPTH,
                    float(self._queue.qsize()),
                )
        if not ok:
            self._metric(
                "increment_counter",
                TRACE_EXPORT_METRIC_FAILURES_TOTAL,
                labels={"stage": "close"},
            )

    def _finalize_close_dead_worker_locked(self) -> None:
        """worker 已死导致 close 永久不可完成时的 truthful finalize（锁内调用）。

        剩余 queued items 因唯一 export worker 不可用而被永久放弃：权威
        ``dropped_total`` 与 metric projection 必须描述同一个 abandoned 数量
        （health counters 是权威内部事实，metrics 是 best-effort projection）。
        drop reason 为 ``worker_unavailable``（不是 ``shutdown_timeout``——
        后者只用于 final shutdown deadline 实际到期的放弃）。close 本身记一次
        ``close`` stage failure。只执行一次（``_close_finalized`` 守卫）。
        """
        self._close_failures += 1
        self._last_safe_error_code = TRACE_EXPORT_CLOSE_FAILED
        self._close_result = False
        self._close_finalized = True
        self._condition.notify_all()
        abandoned = self._queue.qsize()
        if abandoned > 0:
            # 权威 health 与 metric projection 同源同数（不重复计数：
            # attempted 的 in-flight item 已被 worker 出队，不在 qsize 内；
            # sentinel 在 dead-worker finalize 前从未入队）。
            self._dropped_total += abandoned
            self._metric(
                "increment_counter",
                TRACE_EXPORT_METRIC_DROPPED_TOTAL,
                float(abandoned),
                labels={"reason": "worker_unavailable"},
            )
        self._metric(
            "increment_counter",
            TRACE_EXPORT_METRIC_FAILURES_TOTAL,
            labels={"stage": "close"},
        )

    def _mark_worker_failed(self) -> None:
        with self._condition:
            if self._state is TraceExportDispatcherState.CLOSED:
                return
            self._state = TraceExportDispatcherState.FAILED
            self._failed_total += 1
            self._last_safe_error_code = TRACE_EXPORT_WORKER_FAILED
            self._condition.notify_all()
            self._metric(
                "increment_counter",
                TRACE_EXPORT_METRIC_FAILURES_TOTAL,
                labels={"stage": "worker"},
            )

    def _record_flush_timeout(self) -> None:
        with self._condition:
            self._flush_failures += 1
            self._last_safe_error_code = TRACE_EXPORT_FLUSH_TIMEOUT
        self._metric(
            "increment_counter",
            TRACE_EXPORT_METRIC_FAILURES_TOTAL,
            labels={"stage": "flush"},
        )


__all__ = [
    "DELIVERY_SEMANTICS",
    "TRACE_EXPORT_CLOSE_FAILED",
    "TRACE_EXPORT_CLOSED",
    "TRACE_EXPORT_DROP_REASONS",
    "TRACE_EXPORT_FAILURE_STAGES",
    "TRACE_EXPORT_FLUSH_TIMEOUT",
    "TRACE_EXPORT_INCOMPATIBLE",
    "TRACE_EXPORT_METRIC_ACCEPTED_TOTAL",
    "TRACE_EXPORT_METRIC_ATTEMPTED_TOTAL",
    "TRACE_EXPORT_METRIC_DROPPED_TOTAL",
    "TRACE_EXPORT_METRIC_FAILURES_TOTAL",
    "TRACE_EXPORT_METRIC_FLUSH_DURATION_SECONDS",
    "TRACE_EXPORT_METRIC_QUEUE_DEPTH",
    "TRACE_EXPORT_METRIC_SENT_TOTAL",
    "TRACE_EXPORT_PROJECTION_FAILED",
    "TRACE_EXPORT_QUEUE_FULL",
    "TRACE_EXPORT_TRANSPORT_FAILED",
    "TRACE_EXPORT_WORKER_FAILED",
    "TRANSPORT_ATTEMPT_SEMANTICS",
    "TraceExportDispatcher",
    "TraceExportDispatcherState",
    "TraceExportHealthSnapshot",
]
