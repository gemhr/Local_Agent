#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""有界、固定双 Sink、故障隔离的 Runtime Observability Dispatcher。"""

from __future__ import annotations

import asyncio

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


LOGGER_CONSUMER_ID = "runtime_structured_logger_v1"
METRICS_CONSUMER_ID = "runtime_metrics_projector_v1"
_STOP = object()


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
            self.health.increment("logger_failures")
        try:
            status = await self._metrics_consumer.consume(record)
            if status is EventConsumptionStatus.DUPLICATE:
                self._duplicate("metrics_projector")
        except Exception:
            self.health.increment("metrics_failures")
        try:
            record_gauge_snapshot(self._metrics_recorder, self.gauge_provider)
        except Exception:
            self.health.increment("metrics_failures")

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _STOP:
                    return
                if not isinstance(item, JournalRecord):
                    self.health.increment("worker_failures")
                    continue
                try:
                    await self._consume_one(item)
                except Exception:
                    self.health.increment("worker_failures")
            finally:
                self._queue.task_done()

    async def flush(self, timeout: float = 5.0) -> bool:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout 必须是数字")
        if timeout < 0:
            raise ValueError("timeout 不能为负")
        try:
            await asyncio.wait_for(self._queue.join(), timeout=float(timeout))
            return True
        except TimeoutError:
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
                self.health.increment("metrics_failures")
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
