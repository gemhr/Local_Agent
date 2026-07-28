from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from core.runtime import (
    ConsumerErrorCode,
    EventConsumptionStatus,
    EventConsumerError,
    IdempotentEventConsumer,
    InMemoryEventConsumptionCheckpointStore,
    JournalRecord,
    RunStartedPayload,
    RuntimeEvent,
    RuntimeEventType,
    SQLiteEventConsumptionCheckpointStore,
)


def records(*sequences: int, run_id: str = "run-a"):
    return tuple(
        JournalRecord.from_event(
            RuntimeEvent(
                schema_version=1,
                event_id=uuid4().hex,
                run_id=run_id,
                trace_id=f"trace-{run_id}",
                sequence=sequence,
                event_type=RuntimeEventType.RUN_STARTED,
                emitted_at=datetime.now(UTC),
                component="test",
                payload=RunStartedPayload("RUNNING"),
            )
        )
        for sequence in sequences
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path: Path):
    value = (
        InMemoryEventConsumptionCheckpointStore()
        if request.param == "memory"
        else SQLiteEventConsumptionCheckpointStore(str(tmp_path / "checkpoint.db"))
    )
    yield value
    value.close()
    value.close()


@pytest.mark.asyncio
async def test_first_consume_duplicate_and_checkpoint_after_success(store):
    calls = []

    async def handler(record):
        calls.append(record.event_id)

    record = records(1)[0]
    consumer = IdempotentEventConsumer(
        consumer_id="projection-a",
        checkpoint_store=store,
        handler=handler,
    )
    assert await consumer.consume(record) is EventConsumptionStatus.PROCESSED
    assert await consumer.consume(record) is EventConsumptionStatus.DUPLICATE
    assert calls == [record.event_id]
    checkpoint = store.get("projection-a", record.event_id)
    assert checkpoint is not None
    assert checkpoint.run_id == record.run_id
    assert checkpoint.sequence == record.sequence


@pytest.mark.asyncio
async def test_concurrent_duplicate_runs_handler_once(store):
    calls = 0

    async def handler(_record):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)

    record = records(1)[0]
    consumer = IdempotentEventConsumer(
        consumer_id="projection-a",
        checkpoint_store=store,
        handler=handler,
    )
    statuses = await asyncio.gather(
        *(consumer.consume(record) for _ in range(20))
    )
    assert calls == 1
    assert statuses.count(EventConsumptionStatus.PROCESSED) == 1
    assert statuses.count(EventConsumptionStatus.DUPLICATE) == 19


@pytest.mark.asyncio
async def test_handler_failure_does_not_save_checkpoint(store):
    attempts = 0
    record = records(1)[0]

    def handler(_record):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("handler failed")

    consumer = IdempotentEventConsumer(
        consumer_id="projection-a",
        checkpoint_store=store,
        handler=handler,
    )
    with pytest.raises(RuntimeError):
        await consumer.consume(record)
    assert store.get("projection-a", record.event_id) is None
    with pytest.raises(RuntimeError):
        await consumer.consume(record)
    assert attempts == 2


@pytest.mark.asyncio
async def test_gap_allowed_but_unknown_lower_sequence_rejected(store):
    first, gap, lower = records(1, 5, 3)
    consumer = IdempotentEventConsumer(
        consumer_id="projection-a",
        checkpoint_store=store,
        handler=lambda _: None,
    )
    assert await consumer.consume(first) is EventConsumptionStatus.PROCESSED
    assert await consumer.consume(gap) is EventConsumptionStatus.PROCESSED
    with pytest.raises(EventConsumerError) as exc:
        await consumer.consume(lower)
    assert exc.value.error_code is ConsumerErrorCode.OUT_OF_ORDER


@pytest.mark.asyncio
async def test_consumers_and_runs_have_independent_progress(store):
    run_a = records(5, run_id="run-a")[0]
    run_b = records(1, run_id="run-b")[0]
    calls = []

    async def handler(record):
        calls.append(record.run_id)

    first = IdempotentEventConsumer(
        consumer_id="consumer-a", checkpoint_store=store, handler=handler
    )
    second = IdempotentEventConsumer(
        consumer_id="consumer-b", checkpoint_store=store, handler=handler
    )
    assert await first.consume(run_a) is EventConsumptionStatus.PROCESSED
    assert await first.consume(run_b) is EventConsumptionStatus.PROCESSED
    assert await second.consume(run_a) is EventConsumptionStatus.PROCESSED
    assert calls == ["run-a", "run-b", "run-a"]
