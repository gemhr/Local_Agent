from __future__ import annotations

from datetime import UTC, datetime
import threading

import pytest

from core.runtime import (
    ApplicationRuntimeGaugeProvider,
    BoundedBlockingExecutor,
    BlockingTaskKind,
    InMemoryEventConsumptionCheckpointStore,
    InMemoryMetricsRecorder,
    InMemoryRunEventJournal,
    InMemoryStructuredRuntimeLogger,
    JournalAppendStatus,
    ModelCircuitBreakerRegistry,
    RecorderInfrastructureMetricsHook,
    RunEventEmitter,
    RunStartedPayload,
    RuntimeEventChannel,
    RuntimeEventDraft,
    RuntimeEventType,
    RuntimeMetricsProjector,
    RuntimeMetricsCollector,
    RuntimeObservabilityDispatcher,
    StructuredLogProjector,
    ToolConcurrencyController,
    process_run_registry,
)


@pytest.mark.asyncio
async def test_journal_first_dispatch_then_ui_channel_without_competition():
    metrics = InMemoryMetricsRecorder()
    logger = InMemoryStructuredRuntimeLogger()
    hook = RecorderInfrastructureMetricsHook(metrics)
    journal = InMemoryRunEventJournal(metrics_hook=hook)
    gauge = ApplicationRuntimeGaugeProvider(
        run_registry=process_run_registry,
        blocking_executor=BoundedBlockingExecutor(max_workers=1, max_pending_tasks=0),
        tool_workers=ToolConcurrencyController(),
        circuit_registry=ModelCircuitBreakerRegistry(),
    )
    dispatcher = RuntimeObservabilityDispatcher(
        logger_projector=StructuredLogProjector(logger),
        metrics_projector=RuntimeMetricsProjector(metrics),
        logger_checkpoint_store=InMemoryEventConsumptionCheckpointStore(),
        metrics_checkpoint_store=InMemoryEventConsumptionCheckpointStore(),
        queue_capacity=8,
        infrastructure_hook=hook,
        gauge_provider=gauge,
    )
    channel = RuntimeEventChannel(
        4,
        run_id="run-a",
        journal=journal,
        observability_dispatcher=dispatcher,
    )
    gauge.register_channel(channel)
    emitter = RunEventEmitter(run_id="run-a", trace_id="trace-a", channel=channel)
    published = await emitter.emit(
        RuntimeEventType.RUN_STARTED,
        RunStartedPayload("RUNNING"),
        component="test",
    )
    assert journal.get_by_event_id(published.event_id) is not None
    assert channel.buffered_count == 1
    assert (
        metrics.snapshot(gauge_provider=gauge).gauge(
            "runtime_event_channel_buffered"
        )
        == 1
    )
    await channel.close()
    ui_events = [event async for event in channel]
    assert [event.event_id for event in ui_events] == [published.event_id]
    assert (
        metrics.snapshot(gauge_provider=gauge).gauge(
            "runtime_event_channel_buffered"
        )
        == 0
    )
    assert await dispatcher.flush()
    assert [item.event_id for item in logger.records] == [published.event_id]
    assert metrics.snapshot().counter("runtime_runs_started_total") == 1
    assert len(journal.read_after("run-a", 0, 10)) == 1
    gauge.unregister_channel(channel)
    assert await dispatcher.close()
    gauge.blocking_executor.shutdown(wait=True, timeout=1)


def test_gauge_provider_reads_real_component_snapshots_and_close_state():
    executor = BoundedBlockingExecutor(max_workers=1, max_pending_tasks=1)
    gauge = ApplicationRuntimeGaugeProvider(
        run_registry=process_run_registry,
        blocking_executor=executor,
        tool_workers=ToolConcurrencyController(),
        circuit_registry=ModelCircuitBreakerRegistry(),
    )
    values = gauge.snapshot()
    assert values["runtime_active_runs"] == 0
    assert values["runtime_active_steps"] == 0
    assert values["runtime_detached_tool_workers"] == 0
    assert values["runtime_detached_retrieval_workers"] == 0
    assert values["runtime_blocking_executor_active"] == 0
    assert values["runtime_blocking_executor_pending"] == 0
    assert values["runtime_event_channel_buffered"] == 0
    assert values["runtime_circuit_breakers_open"] == 0
    assert executor.shutdown(wait=True, timeout=1)


def test_journal_hook_failure_isolated_from_append():
    class BrokenHook:
        def journal_append_succeeded(self, **_kwargs):
            raise RuntimeError("hook failed")

        def journal_append_failed(self, **_kwargs):
            raise RuntimeError("hook failed")

        def observability_record_dropped(self):
            raise RuntimeError("hook failed")

        def event_duplicate_observed(self, **_kwargs):
            raise RuntimeError("hook failed")

    journal = InMemoryRunEventJournal(metrics_hook=BrokenHook())
    channel_event = __import__("core.runtime", fromlist=["RuntimeEvent"]).RuntimeEvent(
        schema_version=1,
        event_id="event-1",
        run_id="run-a",
        trace_id="trace-a",
        sequence=1,
        event_type=RuntimeEventType.RUN_STARTED,
        emitted_at=datetime.now(UTC),
        component="test",
        payload=RunStartedPayload("RUNNING"),
    )
    assert journal.append(channel_event).value == "APPENDED"


def test_journal_duplicate_metric_is_detection_occurrence():
    metrics = InMemoryMetricsRecorder()
    journal = InMemoryRunEventJournal(
        metrics_hook=RecorderInfrastructureMetricsHook(metrics)
    )
    event = __import__("core.runtime", fromlist=["RuntimeEvent"]).RuntimeEvent(
        schema_version=1,
        event_id="event-duplicate",
        run_id="run-a",
        trace_id="trace-a",
        sequence=1,
        event_type=RuntimeEventType.RUN_STARTED,
        emitted_at=datetime.now(UTC),
        component="test",
        payload=RunStartedPayload("RUNNING"),
    )
    assert journal.append(event) is JournalAppendStatus.APPENDED
    assert journal.append(event) is JournalAppendStatus.DUPLICATE
    assert (
        metrics.snapshot().counter(
            "runtime_event_duplicates_total", {"component": "journal"}
        )
        == 1
    )


@pytest.mark.asyncio
async def test_journal_duplicate_is_not_submitted_to_observability():
    class DuplicateJournal:
        def last_sequence(self, _run_id):
            return None

        def append(self, _event):
            return JournalAppendStatus.DUPLICATE

    class Submitter:
        calls = 0

        def try_submit(self, _record):
            self.calls += 1
            return True

    submitter = Submitter()
    channel = RuntimeEventChannel(
        2,
        run_id="run-a",
        journal=DuplicateJournal(),
        observability_dispatcher=submitter,
    )
    await channel.publish(
        RuntimeEventDraft(
            run_id="run-a",
            trace_id="trace-a",
            event_type=RuntimeEventType.RUN_STARTED,
            component="test",
            payload=RunStartedPayload("RUNNING"),
        )
    )
    assert submitter.calls == 0
    await channel.abort()


def test_metrics_collect_refreshes_detached_worker_without_new_event():
    executor = BoundedBlockingExecutor(max_workers=1, max_pending_tasks=0)
    workers = ToolConcurrencyController()
    gauge = ApplicationRuntimeGaugeProvider(
        run_registry=process_run_registry,
        blocking_executor=executor,
        tool_workers=workers,
        circuit_registry=ModelCircuitBreakerRegistry(),
    )
    recorder = InMemoryMetricsRecorder()
    collector = RuntimeMetricsCollector(recorder, gauge)
    workers.register_worker(
        invocation_id="invocation-a",
        attempt_id="attempt-a",
        started_at=datetime.now(UTC),
        tool_name="safe-tool",
        resource_key_digest=None,
    )
    workers.mark_worker_detached("attempt-a")
    assert (
        collector.collect_snapshot().gauge(
            "runtime_detached_tool_workers"
        )
        == 1
    )
    workers.complete_worker("attempt-a")
    assert (
        collector.collect_snapshot().gauge(
            "runtime_detached_tool_workers"
        )
        == 0
    )
    assert executor.shutdown(wait=True, timeout=1)


def test_metrics_collect_refreshes_retrieval_worker_after_natural_finish():
    executor = BoundedBlockingExecutor(max_workers=1, max_pending_tasks=0)
    gauge = ApplicationRuntimeGaugeProvider(
        run_registry=process_run_registry,
        blocking_executor=executor,
        circuit_registry=ModelCircuitBreakerRegistry(),
    )
    collector = RuntimeMetricsCollector(InMemoryMetricsRecorder(), gauge)
    started = threading.Event()
    release = threading.Event()

    def operation():
        started.set()
        release.wait(1)

    handle = executor.submit(
        operation,
        kind=BlockingTaskKind.VECTOR_QUERY,
        run_id="run-a",
        operation_id="retrieval-a",
        cancellation_check=lambda: None,
        remaining_seconds=lambda: 1.0,
    )
    assert started.wait(1)
    assert (
        collector.collect_snapshot().gauge(
            "runtime_blocking_executor_active"
        )
        == 1
    )
    wait_state = handle.cancel_or_detach()
    assert wait_state.execution_detached
    assert (
        collector.collect_snapshot().gauge(
            "runtime_detached_retrieval_workers"
        )
        == 1
    )
    release.set()
    assert executor.wait_until_idle(1)
    snapshot = collector.collect_snapshot()
    assert snapshot.gauge("runtime_detached_retrieval_workers") == 0
    assert snapshot.gauge("runtime_blocking_executor_active") == 0
    assert executor.shutdown(wait=True, timeout=1)
