from __future__ import annotations

import asyncio

import pytest

from core.runtime import (
    ApplicationRuntimeServices,
    FaultPoint,
    GracefulShutdownCoordinator,
    InMemoryRunEventJournal,
    NoopMetricsRecorder,
    NoopSpanRecorder,
    NoopStructuredRuntimeLogger,
    RunRegistry,
    SQLiteRunEventJournal,
)
from tests._runtime_assembly_fixtures import FakeDispatcher, make_services
from tests._shutdown_fault_fixtures import (
    RecordingResource,
    RecordingWorker,
    shutdown_controller,
    shutdown_rule,
)
from tests.test_event_journal import event


def services_for_components(
    *,
    journal,
    snapshot=None,
    extra=(),
    workers=(),
):
    return ApplicationRuntimeServices(
        event_journal=journal,
        observability_dispatcher=FakeDispatcher(),
        structured_logger=NoopStructuredRuntimeLogger(),
        runtime_metrics_recorder=NoopMetricsRecorder(),
        span_recorder=NoopSpanRecorder(),
        snapshot_store=snapshot,
        recovery_validator=object() if snapshot is not None else None,
        model_invocation_router=object(),
        tool_execution_service=object(),
        retrieval_execution_service=object(),
        blocking_executors=workers,
        worker_trackers=(),
        run_registry=RunRegistry(),
        snapshot_enabled=snapshot is not None,
        recovery_enabled=snapshot is not None,
        extra_closeables=extra,
    )


@pytest.mark.asyncio
async def test_journal_uses_specific_seam_only_and_later_components_continue():
    calls: list[str] = []
    journal = RecordingResource("journal", calls)
    model = RecordingResource("model", calls)
    remaining = RecordingResource("remaining", calls)
    services = services_for_components(
        journal=journal,
        extra=(("model_engine_0", model), ("remaining_store", remaining)),
    )
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_BEFORE_JOURNAL_CLOSE,
            rule_id="journal-specific",
            shutdown_component="event_journal",
        ),
        shutdown_rule(
            FaultPoint.SHUTDOWN_COMPONENT_CLOSE,
            rule_id="journal-generic",
            shutdown_component="event_journal",
        ),
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    assert journal.close_calls == 0
    assert model.close_calls == 1
    assert remaining.close_calls == 1
    counters = {item.rule_id: item for item in controller.snapshot().counters}
    assert counters["journal-specific"].hit_count == 1
    assert counters["journal-generic"].match_count == 0
    assert "RUNTIME_JOURNAL_CLOSE_INJECTED_FAILURE" in report.error_codes


@pytest.mark.asyncio
async def test_model_uses_specific_seam_and_is_not_reported_deferred():
    calls: list[str] = []
    model = RecordingResource("model", calls)
    remaining = RecordingResource("remaining", calls)
    services = services_for_components(
        journal=RecordingResource("journal", calls),
        extra=(("model_engine_0", model), ("remaining_store", remaining)),
    )
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_BEFORE_MODEL_CLOSE,
            rule_id="model-specific",
            shutdown_component="model_engine_0",
        ),
        shutdown_rule(
            FaultPoint.SHUTDOWN_COMPONENT_CLOSE,
            rule_id="model-generic",
            shutdown_component="model_engine_0",
        ),
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    assert model.close_calls == 0
    assert remaining.close_calls == 1
    counters = {item.rule_id: item for item in controller.snapshot().counters}
    assert counters["model-specific"].hit_count == 1
    assert counters["model-generic"].match_count == 0
    assert "RUNTIME_MODEL_CLOSE_INJECTED_FAILURE" in report.error_codes
    assert "RUNTIME_MODEL_CLOSE_DEFERRED_ACTIVE_WORKER" not in report.error_codes


@pytest.mark.asyncio
async def test_snapshot_generic_fault_does_not_skip_journal_or_model():
    calls: list[str] = []
    snapshot = RecordingResource("snapshot", calls)
    journal = RecordingResource("journal", calls)
    model = RecordingResource("model", calls)
    services = services_for_components(
        journal=journal,
        snapshot=snapshot,
        extra=(("model_engine_0", model),),
    )
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_COMPONENT_CLOSE,
            shutdown_component="snapshot_store",
        )
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    assert snapshot.close_calls == 0
    assert journal.close_calls == 1
    assert model.close_calls == 1
    assert "RUNTIME_COMPONENT_CLOSE_INJECTED_FAILURE" in report.error_codes


@pytest.mark.asyncio
async def test_nonexistent_close_does_not_consume_rule_and_shared_object_closes_once():
    calls: list[str] = []
    shared = RecordingResource("shared", calls)
    services = services_for_components(
        journal=RecordingResource("journal", calls),
        extra=(("model_engine_0", shared), ("remaining_store", shared)),
    )
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_COMPONENT_CLOSE,
            shutdown_component="missing_component",
        )
    )

    await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    assert shared.close_calls == 1
    counter = controller.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (0, 0)


@pytest.mark.asyncio
async def test_deferred_shared_model_cannot_close_through_remaining_alias():
    calls: list[str] = []
    shared = RecordingResource("shared", calls)
    worker = RecordingWorker(calls, active=1, idle_result=False)
    services = services_for_components(
        journal=RecordingResource("journal", calls),
        extra=(("model_engine_0", shared), ("remaining_store", shared)),
        workers=(worker,),
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown()

    assert shared.close_calls == 0
    assert "RUNTIME_MODEL_CLOSE_DEFERRED_ACTIVE_WORKER" in report.error_codes


@pytest.mark.asyncio
async def test_real_component_failure_does_not_skip_later_component():
    calls: list[str] = []
    failing = RecordingResource("failing", calls, fail_close=True)
    later = RecordingResource("later", calls)
    services = services_for_components(
        journal=RecordingResource("journal", calls),
        extra=(("remaining_store", failing), ("http_client", later)),
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown()

    assert failing.close_calls == 1
    assert later.close_calls == 1
    assert "RUNTIME_COMPONENT_CLOSE_FAILED" in report.error_codes
    assert "provider-secret-error" not in repr(report)


@pytest.mark.asyncio
async def test_component_close_timeout_is_distinct_and_later_component_runs():
    calls: list[str] = []

    class SlowResource:
        async def close(self):
            calls.append("slow.close")
            await asyncio.sleep(1)

    later = RecordingResource("later", calls)
    services = services_for_components(
        journal=RecordingResource("journal", calls),
        extra=(("remaining_store", SlowResource()), ("http_client", later)),
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.02,
    ).shutdown()

    assert "RUNTIME_COMPONENT_CLOSE_TIMEOUT" in report.error_codes
    assert later.close_calls == 1


@pytest.mark.asyncio
async def test_model_specific_rule_not_evaluated_when_worker_gate_defers_close():
    calls: list[str] = []
    model = RecordingResource("model", calls)
    worker = RecordingWorker(calls, active=1, idle_result=False)
    services = services_for_components(
        journal=RecordingResource("journal", calls),
        extra=(("model_engine_0", model),),
        workers=(worker,),
    )
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_BEFORE_MODEL_CLOSE,
            shutdown_component="model_engine_0",
        )
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    assert model.close_calls == 0
    counter = controller.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (0, 0)
    assert "RUNTIME_MODEL_CLOSE_DEFERRED_ACTIVE_WORKER" in report.error_codes


@pytest.mark.asyncio
@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
async def test_journal_close_fault_preserves_records_for_memory_and_sqlite(
    journal_kind, tmp_path
):
    journal = (
        InMemoryRunEventJournal()
        if journal_kind == "memory"
        else SQLiteRunEventJournal(str(tmp_path / "shutdown-journal.db"))
    )
    record = event(1)
    journal.append(record)
    services = make_services(journal=journal, snapshot_enabled=False)
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_BEFORE_JOURNAL_CLOSE,
            shutdown_component="event_journal",
        )
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    persisted = journal.get_by_event_id(record.event_id)
    assert persisted is not None
    persisted.verify()
    assert "RUNTIME_JOURNAL_CLOSE_INJECTED_FAILURE" in report.error_codes
    journal.close()
