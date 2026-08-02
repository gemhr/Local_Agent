from __future__ import annotations

import asyncio
import statistics
import time
import tracemalloc

import pytest

from core.chat_service import ChatService
from core.runtime import (
    CancellationReason,
    ChatRuntimeMode,
    ChatRuntimeSelector,
    CoordinatedRuntimeFactory,
    InMemorySpanRecorder,
    RunRegistry,
    create_run_context,
)
from tests._runtime_assembly_fixtures import FakeRouter, make_services


def _baseline_service():
    registry = RunRegistry()
    spans = InMemorySpanRecorder()
    router = FakeRouter()
    services = make_services(
        run_registry=registry,
        span_recorder=spans,
        snapshot_enabled=False,
    )
    service = ChatService(
        router,
        runtime_selector=ChatRuntimeSelector(ChatRuntimeMode.COORDINATED),
        coordinated_runtime_factory=CoordinatedRuntimeFactory(router, services),
        run_registry=registry,
    )
    return service, services, registry, spans


async def _one_run(service):
    started = time.perf_counter()
    events = [
        event
        async for event in service.stream_coordinated_agent_events(
            "baseline-agent", "offline-query", persist=False
        )
    ]
    return time.perf_counter() - started, events


def _assert_owner_counts_zero(services, registry, spans) -> None:
    assert registry.observability_snapshot()["active_runs"] == 0
    assert services.observability_dispatcher.gauge_provider.channels == set()
    assert spans.health_snapshot().active_span_count == 0


@pytest.mark.asyncio
async def test_sequential_50_run_offline_machine_baseline_and_cleanup() -> None:
    service, services, registry, spans = _baseline_service()
    samples = []
    journal_records = []
    for _ in range(50):
        elapsed, events = await _one_run(service)
        samples.append(elapsed)
        run_id = events[0].run_id
        sequences = [event.sequence for event in events]
        assert sequences == list(range(1, len(events) + 1))
        journal_records.append(
            len(services.event_journal.read_after(run_id, 0, 100))
        )
        _assert_owner_counts_zero(services, registry, spans)

    ordered = sorted(samples)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    machine_baseline = {
        "total_seconds": sum(samples),
        "average_seconds": statistics.fmean(samples),
        "median_seconds": statistics.median(samples),
        "p95_seconds": p95,
    }
    assert all(value >= 0 for value in machine_baseline.values())
    assert len(set(journal_records)) == 1
    assert services.snapshot_store is None


@pytest.mark.asyncio
async def test_concurrent_10_run_baseline_has_independent_sequences_and_zero_owners() -> None:
    service, services, registry, spans = _baseline_service()
    results = await asyncio.gather(*(_one_run(service) for _ in range(10)))
    event_batches = [events for _elapsed, events in results]
    run_ids = {events[0].run_id for events in event_batches}

    assert len(run_ids) == 10
    for events in event_batches:
        assert [event.sequence for event in events] == list(
            range(1, len(events) + 1)
        )
    _assert_owner_counts_zero(services, registry, spans)


def test_cancellation_batch_is_bounded_first_wins_and_run_local() -> None:
    pairs = [create_run_context(entry_agent_id="baseline") for _ in range(10)]
    for _context, source in pairs[:5]:
        assert source.cancel(CancellationReason.REQUEST_CANCELLED) is True
        assert source.cancel(CancellationReason.SERVER_SHUTDOWN) is False

    assert all(
        context.cancellation_token.reason is CancellationReason.REQUEST_CANCELLED
        for context, _source in pairs[:5]
    )
    assert all(
        not context.cancellation_token.is_cancelled()
        for context, _source in pairs[5:]
    )


@pytest.mark.asyncio
async def test_tracemalloc_warmup_and_repeated_batches_report_trend_without_sla() -> None:
    service, services, registry, spans = _baseline_service()
    for _ in range(5):
        await _one_run(service)

    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    for _ in range(10):
        await _one_run(service)
    middle = tracemalloc.take_snapshot()
    for _ in range(10):
        await _one_run(service)
    after = tracemalloc.take_snapshot()
    first_trend = sum(item.size_diff for item in middle.compare_to(before, "lineno"))
    second_trend = sum(item.size_diff for item in after.compare_to(middle, "lineno"))
    tracemalloc.stop()

    assert isinstance(first_trend, int)
    assert isinstance(second_trend, int)
    _assert_owner_counts_zero(services, registry, spans)

