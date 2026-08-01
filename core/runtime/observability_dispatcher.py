#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""有界、固定双 Sink、故障隔离的 Runtime Observability Dispatcher。"""

from __future__ import annotations

import asyncio
import time

from core.runtime.cancellation import CancellationToken, RunCancelledError

from core.runtime.event_consumer import (
    EventConsumptionCheckpointStore,
    EventConsumptionStatus,
    IdempotentEventConsumer,
)
from core.runtime.event_journal import JournalRecord
from core.runtime.metrics import (
    RuntimeMetricsProjector,
    RuntimeMetricsRecorder,
    record_gauge_snapshot,
)
from core.runtime.observability import (
    NoopRuntimeGaugeProvider,
    NoopRuntimeInfrastructureMetricsHook,
    ObservabilityHealth,
    RuntimeGaugeProvider,
    RuntimeInfrastructureMetricsHook,
)
from core.runtime.structured_logging import StructuredLogProjector
from core.runtime.fault_injection import FaultInjectionController
from core.runtime.fault_injection_contract import (
    FaultAction,
    FaultMatchContext,
    FaultPoint,
    InjectedFaultError,
)


LOGGER_CONSUMER_ID = "runtime_structured_logger_v1"
METRICS_CONSUMER_ID = "runtime_metrics_projector_v1"
_STOP = object()


class ObservabilityOperationError(RuntimeError):
    """Fixed-code diagnostic failure with no record or sink payload."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)

    def __repr__(self) -> str:
        return f"ObservabilityOperationError(error_code={self.error_code!r})"


class RuntimeObservabilityDispatcher:
    def __init__(
        self,
        *,
        logger_projector: StructuredLogProjector,
        metrics_projector: RuntimeMetricsProjector,
        logger_checkpoint_store: EventConsumptionCheckpointStore,
        metrics_checkpoint_store: EventConsumptionCheckpointStore,
        queue_capacity: int = 256,
        infrastructure_hook: RuntimeInfrastructureMetricsHook | None = None,
        gauge_provider: RuntimeGaugeProvider | None = None,
    ) -> None:
        if (
            isinstance(queue_capacity, bool)
            or not isinstance(queue_capacity, int)
            or queue_capacity <= 0
        ):
            raise ValueError("queue_capacity 必须是正整数")
        self.queue_capacity = queue_capacity
        self.health = ObservabilityHealth()
        self.infrastructure_hook = (
            infrastructure_hook or NoopRuntimeInfrastructureMetricsHook()
        )
        self.gauge_provider = gauge_provider or NoopRuntimeGaugeProvider()
        self._metrics_projector = metrics_projector
        self._metrics_recorder: RuntimeMetricsRecorder = metrics_projector.recorder
        self._logger_consumer = IdempotentEventConsumer(
            consumer_id=LOGGER_CONSUMER_ID,
            checkpoint_store=logger_checkpoint_store,
            handler=logger_projector.project,
        )
        self._metrics_consumer = IdempotentEventConsumer(
            consumer_id=METRICS_CONSUMER_ID,
            checkpoint_store=metrics_checkpoint_store,
            handler=metrics_projector.project,
        )
        self._queue: asyncio.Queue[JournalRecord | object] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._closed = False
        self._stop_enqueued = False
        self._close_lock = asyncio.Lock()
        self._worker = asyncio.create_task(
            self._run(), name="runtime-observability"
        )

    @property
    def buffered_count(self) -> int:
        return self._queue.qsize()

    def try_submit(self, record: JournalRecord) -> bool:
        if not isinstance(record, JournalRecord):
            raise TypeError("Dispatcher 只接受 JournalRecord")
        return self._enqueue(record)

    async def submit(
        self,
        record: JournalRecord,
        *,
        fault_controller: FaultInjectionController | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> bool:
        """Best-effort operation entry used by the Journal-first publisher."""
        if not isinstance(record, JournalRecord):
            raise TypeError("Dispatcher only accepts JournalRecord")
        _validate_fault_controller(fault_controller)
        try:
            await _execute_observability_fault(
                fault_controller,
                FaultPoint.OBSERVABILITY_BEFORE_RECORD,
                cancellation_token=cancellation_token,
                timeout=None,
                event_type=record.event_type.value,
            )
        except RunCancelledError:
            self.health.record_failure(
                "record_failures", "OBSERVABILITY_RECORD_CANCELLED"
            )
            return False
        except InjectedFaultError:
            self.health.record_failure(
                "record_failures", "OBSERVABILITY_RECORD_FAILED"
            )
            return False
        return self._enqueue(record)

    def _enqueue(self, record: JournalRecord) -> bool:
        if self._closed:
            self._drop()
            return False
        try:
            self._queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            self._drop()
            return False

    def _drop(self) -> None:
        self.health.increment("dropped_records")
        try:
            self.infrastructure_hook.observability_record_dropped()
        except Exception:
            return

    def _duplicate(self, component: str) -> None:
        self.health.increment("duplicate_records")
        try:
            self.infrastructure_hook.event_duplicate_observed(
                component=component
            )
        except Exception:
            return

    async def _consume_one(self, record: JournalRecord) -> None:
        try:
            status = await self._logger_consumer.consume(record)
            if status is EventConsumptionStatus.DUPLICATE:
                self._duplicate("structured_logger")
        except Exception:
            self.health.record_failure(
                "logger_failures", "OBSERVABILITY_LOGGER_FAILED"
            )
        try:
            status = await self._metrics_consumer.consume(record)
            if status is EventConsumptionStatus.DUPLICATE:
                self._duplicate("metrics_projector")
        except Exception:
            self.health.record_failure(
                "metrics_failures", "OBSERVABILITY_METRICS_FAILED"
            )
        try:
            record_gauge_snapshot(self._metrics_recorder, self.gauge_provider)
        except Exception:
            self.health.record_failure(
                "metrics_failures", "OBSERVABILITY_METRICS_FAILED"
            )

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _STOP:
                    return
                if not isinstance(item, JournalRecord):
                    self.health.record_failure(
                        "worker_failures", "OBSERVABILITY_WORKER_FAILED"
                    )
                    continue
                try:
                    await self._consume_one(item)
                except Exception:
                    self.health.record_failure(
                        "worker_failures", "OBSERVABILITY_WORKER_FAILED"
                    )
            finally:
                self._queue.task_done()

    async def flush(
        self,
        timeout: float = 5.0,
        *,
        fault_controller: FaultInjectionController | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> bool:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout 必须是数字")
        if timeout < 0:
            raise ValueError("timeout 不能为负")
        _validate_fault_controller(fault_controller)
        started = time.monotonic()
        try:
            await _execute_observability_fault(
                fault_controller,
                FaultPoint.OBSERVABILITY_BEFORE_FLUSH,
                cancellation_token=cancellation_token,
                timeout=float(timeout),
                event_type=None,
            )
            remaining = max(0.0, float(timeout) - (time.monotonic() - started))
            await asyncio.wait_for(self._queue.join(), timeout=remaining)
            return True
        except RunCancelledError:
            self.health.record_failure(
                "flush_failures", "OBSERVABILITY_FLUSH_CANCELLED"
            )
            raise ObservabilityOperationError(
                "OBSERVABILITY_FLUSH_CANCELLED"
            ) from None
        except InjectedFaultError:
            self.health.record_failure(
                "flush_failures", "OBSERVABILITY_FLUSH_FAILED"
            )
            raise ObservabilityOperationError(
                "OBSERVABILITY_FLUSH_FAILED"
            ) from None
        except TimeoutError:
            self.health.record_failure(
                "flush_failures", "OBSERVABILITY_FLUSH_TIMEOUT"
            )
            return False

    async def close(self, timeout: float = 5.0) -> bool:
        async with self._close_lock:
            self._metrics_projector.clear_correlation_state()
            if self._worker.done():
                return not self._worker.cancelled()
            self._closed = True
            flushed = await self.flush(timeout)
            if not flushed:
                return False
            try:
                record_gauge_snapshot(
                    self._metrics_recorder, self.gauge_provider
                )
            except Exception:
                self.health.record_failure(
                    "metrics_failures", "OBSERVABILITY_METRICS_FAILED"
                )
            try:
                if not self._stop_enqueued:
                    self._queue.put_nowait(_STOP)
                    self._stop_enqueued = True
                await asyncio.wait_for(
                    asyncio.shield(self._worker), timeout=float(timeout)
                )
            except (asyncio.QueueFull, TimeoutError):
                return False
            return True


def _validate_fault_controller(
    controller: FaultInjectionController | None,
) -> None:
    if controller is not None and not isinstance(
        controller, FaultInjectionController
    ):
        raise TypeError("fault_controller must be FaultInjectionController or None")


async def _execute_observability_fault(
    controller: FaultInjectionController | None,
    point: FaultPoint,
    *,
    cancellation_token: CancellationToken | None,
    timeout: float | None,
    event_type: str | None,
) -> None:
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled()
    if controller is None:
        return
    task = asyncio.create_task(
        controller.execute_if_matched(
            FaultMatchContext(
                fault_point=point,
                component="observability_dispatcher",
                operation_kind=(
                    "OBSERVABILITY_RECORD"
                    if point is FaultPoint.OBSERVABILITY_BEFORE_RECORD
                    else "OBSERVABILITY_FLUSH"
                ),
                event_type=event_type,
            ),
            allowed_actions={
                FaultAction.RAISE_TYPED_ERROR,
                FaultAction.DELAY,
                FaultAction.BLOCK_UNTIL_RELEASED,
            },
        )
    )
    started = time.monotonic()
    try:
        while not task.done():
            if cancellation_token is not None:
                cancellation_token.raise_if_cancelled()
            if timeout is not None and time.monotonic() - started >= timeout:
                raise TimeoutError
            remaining = (
                None
                if timeout is None
                else max(0.0, timeout - (time.monotonic() - started))
            )
            await asyncio.wait(
                {task},
                timeout=0.01 if remaining is None else min(0.01, remaining),
            )
        await task
    except BaseException:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


__all__ = [
    "LOGGER_CONSUMER_ID",
    "METRICS_CONSUMER_ID",
    "ObservabilityOperationError",
    "RuntimeObservabilityDispatcher",
]
