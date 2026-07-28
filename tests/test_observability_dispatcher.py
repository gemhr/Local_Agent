from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from core.runtime import (
    InMemoryEventConsumptionCheckpointStore,
    InMemoryMetricsRecorder,
    InMemoryStructuredRuntimeLogger,
    JournalRecord,
    LOGGER_CONSUMER_ID,
    METRICS_CONSUMER_ID,
    NoopRuntimeGaugeProvider,
    RecorderInfrastructureMetricsHook,
    RunStartedPayload,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeMetricsProjector,
    RuntimeObservabilityDispatcher,
    StructuredLogProjector,
)


def record(sequence=1, event_id="event-1"):
    return JournalRecord.from_event(
        RuntimeEvent(
            schema_version=1,
            event_id=event_id,
            run_id="run-a",
            trace_id="trace-a",
            sequence=sequence,
            event_type=RuntimeEventType.RUN_STARTED,
            emitted_at=datetime.now(UTC),
            component="test",
            payload=RunStartedPayload("RUNNING"),
        )
    )


def dispatcher(*, capacity=8, logger=None, metrics=None, logger_store=None, metrics_store=None):
    logger = logger or InMemoryStructuredRuntimeLogger()
    metrics = metrics or InMemoryMetricsRecorder()
    logger_store = logger_store or InMemoryEventConsumptionCheckpointStore()
    metrics_store = metrics_store or InMemoryEventConsumptionCheckpointStore()
    value = RuntimeObservabilityDispatcher(
        logger_projector=StructuredLogProjector(logger),
        metrics_projector=RuntimeMetricsProjector(metrics),
        logger_checkpoint_store=logger_store,
        metrics_checkpoint_store=metrics_store,
        queue_capacity=capacity,
        infrastructure_hook=RecorderInfrastructureMetricsHook(metrics),
        gauge_provider=NoopRuntimeGaugeProvider(),
    )
    return value, logger, metrics, logger_store, metrics_store


@pytest.mark.asyncio
async def test_live_duplicate_and_concurrent_duplicate_are_idempotent_per_sink():
    value, logger, metrics, logger_store, metrics_store = dispatcher()
    source = record()
    assert value.try_submit(source)
    assert value.try_submit(source)
    assert await value.flush()
    assert len(logger.records) == 1
    assert metrics.snapshot().counter("runtime_runs_started_total") == 1
    assert logger_store.get(LOGGER_CONSUMER_ID, source.event_id) is not None
    assert metrics_store.get(METRICS_CONSUMER_ID, source.event_id) is not None
    assert value.health.snapshot().duplicate_records == 2
    snapshot = metrics.snapshot()
    assert snapshot.counter(
        "runtime_event_duplicates_total",
        {"component": "structured_logger"},
    ) == 1
    assert snapshot.counter(
        "runtime_event_duplicates_total",
        {"component": "metrics_projector"},
    ) == 1
    assert await value.close()
    assert value._metrics_projector.correlation_state_size == 0
    assert await value.close()


@pytest.mark.asyncio
async def test_logger_and_metrics_checkpoints_are_independent_on_logger_failure():
    class FailingLogger:
        def write(self, _record):
            raise RuntimeError("raw secret that must not escape")

    value, _, metrics, logger_store, metrics_store = dispatcher(logger=FailingLogger())
    source = record()
    assert value.try_submit(source)
    assert await value.flush()
    assert logger_store.get(LOGGER_CONSUMER_ID, source.event_id) is None
    assert metrics_store.get(METRICS_CONSUMER_ID, source.event_id) is not None
    assert metrics.snapshot().counter("runtime_runs_started_total") == 1
    assert value.health.snapshot().logger_failures == 1
    assert await value.close()


@pytest.mark.asyncio
async def test_metrics_failure_does_not_rollback_logger_checkpoint():
    class FailingRecorder(InMemoryMetricsRecorder):
        def increment_counter(self, *args, **kwargs):
            raise RuntimeError("metrics down")

    value, logger, _, logger_store, metrics_store = dispatcher(
        metrics=FailingRecorder()
    )
    source = record()
    assert value.try_submit(source)
    assert await value.flush()
    assert len(logger.records) == 1
    assert logger_store.get(LOGGER_CONSUMER_ID, source.event_id) is not None
    assert metrics_store.get(METRICS_CONSUMER_ID, source.event_id) is None
    assert value.health.snapshot().metrics_failures >= 1
    assert await value.close()


@pytest.mark.asyncio
async def test_queue_full_drops_without_blocking_and_close_is_bounded():
    value, *_ = dispatcher(capacity=1)
    value._worker.cancel()
    await asyncio.gather(value._worker, return_exceptions=True)
    assert value.try_submit(record())
    assert not value.try_submit(record(2, "event-2"))
    assert value.health.snapshot().dropped_records == 1
    assert not await value.flush(0.01)
    assert not await value.close(0.01)
