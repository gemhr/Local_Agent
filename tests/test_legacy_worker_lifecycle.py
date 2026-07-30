from __future__ import annotations

import asyncio
import contextvars
from dataclasses import replace
import threading

import pytest

from core.runtime import (
    BlockingExecutorAdmissionTimeout,
    BlockingTaskKind,
    BoundedBlockingExecutor,
    GracefulShutdownCoordinator,
)
from tests._runtime_assembly_fixtures import make_services


def _submit(executor, operation, *, run_id="legacy-run"):
    return executor.submit_nowait(
        operation,
        kind=BlockingTaskKind.LEGACY_STREAM_STEP,
        run_id=run_id,
        operation_id="legacy_stream_next",
        cancellation_check=lambda: None,
    )


@pytest.mark.asyncio
async def test_legacy_worker_normal_completion_propagates_context_and_cleans():
    executor = BoundedBlockingExecutor(
        max_workers=1,
        max_pending_tasks=1,
        thread_name_prefix="test-legacy-step",
    )
    marker = contextvars.ContextVar("legacy_marker", default="missing")
    marker.set("captured")

    handle = _submit(executor, marker.get)

    assert await handle.result_async() == "captured"
    assert executor.wait_until_idle(0.2) is True
    snapshot = executor.snapshot()
    assert snapshot.active_count == 0
    assert snapshot.pending_count == 0
    assert snapshot.detached_count == 0
    assert executor.shutdown(timeout=0.2) is True


@pytest.mark.asyncio
async def test_cancelled_waiter_detaches_until_true_worker_completion():
    executor = BoundedBlockingExecutor(
        max_workers=1,
        max_pending_tasks=0,
        thread_name_prefix="test-legacy-step",
    )
    started = threading.Event()
    release = threading.Event()

    def blocking_step():
        started.set()
        release.wait(1)
        return "late"

    handle = _submit(executor, blocking_step)
    assert await asyncio.to_thread(started.wait, 0.2)
    waiter = asyncio.create_task(handle.result_async())
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    state = handle.cancel_or_detach()
    assert state.worker_terminated is False
    assert state.execution_detached is True
    assert executor.snapshot().detached_count == 1
    assert executor.wait_until_idle(0) is False

    release.set()
    assert await asyncio.to_thread(executor.wait_until_idle, 0.5)
    assert executor.snapshot().detached_count == 0
    assert executor.shutdown(timeout=0.2) is True


def test_legacy_worker_admission_is_bounded_and_nonblocking():
    executor = BoundedBlockingExecutor(
        max_workers=1,
        max_pending_tasks=0,
        thread_name_prefix="test-legacy-step",
    )
    started = threading.Event()
    release = threading.Event()
    first = _submit(
        executor,
        lambda: (started.set(), release.wait(1))[1],
        run_id="legacy-a",
    )
    assert started.wait(0.2)

    with pytest.raises(BlockingExecutorAdmissionTimeout):
        _submit(executor, lambda: None, run_id="legacy-b")

    release.set()
    assert first.result(0.5) is True
    assert executor.shutdown(timeout=0.2) is True


@pytest.mark.asyncio
async def test_shutdown_defers_model_close_while_legacy_worker_is_active():
    executor = BoundedBlockingExecutor(
        max_workers=1,
        max_pending_tasks=0,
        thread_name_prefix="test-legacy-step",
    )
    started = threading.Event()
    release = threading.Event()
    handle = _submit(
        executor,
        lambda: (started.set(), release.wait(1))[1],
    )
    assert await asyncio.to_thread(started.wait, 0.2)
    assert handle.cancel_or_detach().execution_detached is True

    class Model:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    model = Model()
    services = make_services(snapshot_enabled=False)
    services = replace(
        services,
        blocking_executors=(executor,),
        legacy_step_executor=executor,
        extra_closeables=(("model_engine_0", model),),
    )
    coordinator = GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.01,
    )

    report = await coordinator.shutdown()
    repeated = await coordinator.shutdown()

    assert repeated is report
    assert model.close_calls == 0
    assert (
        "RUNTIME_MODEL_CLOSE_DEFERRED_ACTIVE_WORKER"
        in report.error_codes
    )
    assert report.detached_worker_count == 1
    release.set()
    assert await asyncio.to_thread(executor.wait_until_idle, 0.5)
    assert executor.snapshot().detached_count == 0
