import asyncio
from datetime import UTC, datetime
import threading
import time

import pytest

from core.runtime import CancellationSource, ToolConcurrencyController


@pytest.mark.asyncio
async def test_same_resource_key_is_exclusive_across_runs():
    controller = ToolConcurrencyController(max_concurrency=4)
    first_source = CancellationSource()
    second_source = CancellationSource()
    first = await controller.acquire(
        tool_name="tool",
        tool_max_concurrency=4,
        resource_key="shared",
        cancellation_token=first_source.token,
        remaining_seconds=lambda: 1.0,
    )
    acquired = asyncio.Event()

    async def waiter():
        lease = await controller.acquire(
            tool_name="tool",
            tool_max_concurrency=4,
            resource_key="shared",
            cancellation_token=second_source.token,
            remaining_seconds=lambda: 1.0,
        )
        acquired.set()
        lease.release()

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.03)
    assert not acquired.is_set()
    first.release()
    await asyncio.wait_for(task, 1)
    assert acquired.is_set()


@pytest.mark.asyncio
async def test_different_resource_keys_can_run_in_parallel_and_release_is_idempotent():
    controller = ToolConcurrencyController(max_concurrency=2)
    source = CancellationSource()
    first, second = await asyncio.gather(
        controller.acquire(
            tool_name="tool",
            tool_max_concurrency=2,
            resource_key="a",
            cancellation_token=source.token,
            remaining_seconds=lambda: 1.0,
        ),
        controller.acquire(
            tool_name="tool",
            tool_max_concurrency=2,
            resource_key="b",
            cancellation_token=source.token,
            remaining_seconds=lambda: 1.0,
        ),
    )
    assert controller.is_resource_held("a")
    assert controller.is_resource_held("b")
    first.release()
    first.release()
    second.release()
    assert not controller.is_resource_held("a")
    assert not controller.is_resource_held("b")


@pytest.mark.asyncio
async def test_cancelled_resource_wait_releases_other_permits():
    controller = ToolConcurrencyController(max_concurrency=2)
    holder_source = CancellationSource()
    waiter_source = CancellationSource()
    holder = await controller.acquire(
        tool_name="tool",
        tool_max_concurrency=2,
        resource_key="shared",
        cancellation_token=holder_source.token,
        remaining_seconds=lambda: 1.0,
    )
    task = asyncio.create_task(
        controller.acquire(
            tool_name="tool",
            tool_max_concurrency=2,
            resource_key="shared",
            cancellation_token=waiter_source.token,
            remaining_seconds=lambda: 1.0,
        )
    )
    await asyncio.sleep(0.03)
    waiter_source.cancel()
    with pytest.raises(Exception):
        await task
    holder.release()
    lease = await controller.acquire(
        tool_name="tool",
        tool_max_concurrency=2,
        resource_key="shared",
        cancellation_token=holder_source.token,
        remaining_seconds=lambda: 1.0,
    )
    lease.release()


def test_worker_tracker_snapshot_wait_and_exactly_once_cleanup():
    controller = ToolConcurrencyController()
    controller.register_worker(
        invocation_id="safe-invocation",
        attempt_id="safe-attempt",
        started_at=datetime.now(UTC),
        tool_name="safe-tool",
        resource_key_digest="digest-only",
    )
    assert controller.mark_worker_detached("safe-attempt")
    snapshot = controller.worker_snapshot()
    assert snapshot["active_worker_count"] == 1
    assert snapshot["detached_worker_count"] == 1
    serialized = str(snapshot)
    assert "arguments" not in serialized
    assert "output" not in serialized
    assert "exception" not in serialized
    assert not controller.wait_until_idle(0.01)

    done = threading.Thread(
        target=lambda: (
            time.sleep(0.03),
            controller.complete_worker("safe-attempt"),
        )
    )
    done.start()
    assert controller.wait_until_idle(1)
    done.join()
    assert controller.active_worker_count == 0
    assert controller.detached_worker_count == 0
    assert not controller.complete_worker("safe-attempt")


def test_worker_tracker_is_independent_from_run_registry_lifetime():
    controller = ToolConcurrencyController()
    controller.register_worker(
        invocation_id="invocation-after-run",
        attempt_id="attempt-after-run",
        started_at=datetime.now(UTC),
        tool_name="tool",
        resource_key_digest=None,
    )
    # Tracker 不持有 RunHandle；RunRegistry 已清空不影响 Worker 可见性。
    assert controller.worker_snapshot()["active_worker_count"] == 1
    controller.complete_worker("attempt-after-run")
    assert controller.wait_until_idle(0)
