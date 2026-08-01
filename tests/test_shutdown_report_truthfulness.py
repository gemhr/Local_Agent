from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from core.runtime import (
    FaultAction,
    FaultBlocker,
    FaultPoint,
    GracefulShutdownCoordinator,
    RuntimeAdmissionState,
    RuntimeLifecycleState,
)
from tests._runtime_assembly_fixtures import make_services
from tests._shutdown_fault_fixtures import (
    RecordingResource,
    RecordingWorker,
    shutdown_controller,
    shutdown_rule,
)


@pytest.mark.asyncio
async def test_report_distinguishes_unexecuted_drain_and_deferred_model():
    calls: list[str] = []
    worker = RecordingWorker(calls, active=1, detached=1)
    model = RecordingResource("model", calls)
    services = replace(
        make_services(snapshot_enabled=False),
        blocking_executors=(worker,),
        extra_closeables=(("model_engine_0", model),),
    )
    controller = shutdown_controller(
        shutdown_rule(FaultPoint.SHUTDOWN_BEFORE_WORKER_DRAIN)
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    assert report.lifecycle_state is RuntimeLifecycleState.CLOSED
    assert report.state is RuntimeAdmissionState.CLOSED
    assert report.worker_drain_status == "FAILED"
    assert report.active_worker_count == 1
    assert report.detached_worker_count == 1
    model_result = next(
        item for item in report.components if item.component == "model_engine_0"
    )
    assert model_result.status == "DEFERRED"
    assert model.close_calls == 0
    assert report.duration_seconds >= 0


@pytest.mark.asyncio
async def test_shutdown_task_cancellation_propagates_and_never_reopens_admission():
    calls: list[str] = []
    worker = RecordingWorker(calls, active=1)
    services = replace(
        make_services(snapshot_enabled=False),
        blocking_executors=(worker,),
    )
    blocker = FaultBlocker(timeout_seconds=2)
    rule = shutdown_rule(
        FaultPoint.SHUTDOWN_BEFORE_WORKER_DRAIN,
        rule_id="worker-block",
        action=FaultAction.BLOCK_UNTIL_RELEASED,
    )
    controller = shutdown_controller(
        rule,
        blockers={"worker-block": blocker},
    )
    coordinator = GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=1,
    )
    task = asyncio.create_task(coordinator.shutdown(controller))
    await asyncio.wait_for(blocker.entered.wait(), 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert services.lifecycle_state is RuntimeLifecycleState.SHUTTING_DOWN
    assert services.admission_gate.state is RuntimeAdmissionState.DRAINING
    assert worker.wait_calls == 0
    controller.close()


@pytest.mark.asyncio
async def test_shutdown_report_and_errors_contain_no_sensitive_runtime_data():
    markers = (
        "SECRET_PROMPT_TEXT",
        "MODEL_OUTPUT_SECRET",
        "TOOL_ARGUMENT_SECRET",
        "TOOL_OUTPUT_SECRET",
        "RAG_CHUNK_SECRET",
        "MEMORY_SECRET",
        r"C:\Users\private-user",
        "provider-secret-error",
        "raw-idempotency-key",
        "raw-resource-key",
        "raw-snapshot-payload",
        "run-id-plaintext",
        "thread-id-plaintext",
    )
    calls: list[str] = []
    failing = RecordingResource("failing", calls, fail_close=True)
    services = replace(
        make_services(snapshot_enabled=False),
        extra_closeables=(("remaining_store", failing),),
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown()

    rendered = repr(report)
    assert all(marker not in rendered for marker in markers)
    assert all("run_id" not in item.component for item in report.components)
    assert all("thread" not in item.component for item in report.components)
