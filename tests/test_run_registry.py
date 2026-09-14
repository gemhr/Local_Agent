from datetime import UTC, datetime
import threading
import time

import pytest

from core.runtime import (
    ActiveRunControlHandle,
    CancellationReason,
    CancellationSource,
    RunRegistry,
)


def make_handle(run_id="a"):
    return ActiveRunControlHandle(
        run_id=run_id,
        runtime_mode="COORDINATED",
        cancellation_source=CancellationSource(),
        owner="test",
    )


def test_registry_register_cancel_snapshot_and_unregister():
    registry = RunRegistry(); handle = make_handle()
    registry.register(handle)
    assert registry.get("a") is handle
    assert registry.cancel("a", CancellationReason.REQUEST_CANCELLED) is True
    assert registry.cancel("a", CancellationReason.CLIENT_DISCONNECTED) is False
    assert registry.snapshot("a")["cancellation_reason"] == "REQUEST_CANCELLED"
    assert registry.unregister("a") is True
    assert registry.unregister("a") is False


def test_duplicate_and_cancel_all():
    registry = RunRegistry(); registry.register(make_handle("a")); registry.register(make_handle("b"))
    with pytest.raises(ValueError): registry.register(make_handle("a"))
    assert set(registry.cancel_all(CancellationReason.SERVER_SHUTDOWN)) == {"a", "b"}


def test_cancellation_first_wins_across_threads():
    source = CancellationSource(); barrier = threading.Barrier(3)
    def cancel(reason): barrier.wait(); source.cancel(reason, datetime.now(UTC))
    threads = [threading.Thread(target=cancel, args=(CancellationReason.REQUEST_CANCELLED,)), threading.Thread(target=cancel, args=(CancellationReason.CLIENT_DISCONNECTED,))]
    [thread.start() for thread in threads]; barrier.wait(); [thread.join() for thread in threads]
    assert source.token.reason in {CancellationReason.REQUEST_CANCELLED, CancellationReason.CLIENT_DISCONNECTED}
    assert source.token.cancelled_at is not None


def test_wait_until_empty_waits_for_unregister():
    registry = RunRegistry(); registry.register(make_handle())
    done = threading.Event()
    def unregister():
        done.wait(); registry.unregister("a")
    thread = threading.Thread(target=unregister); thread.start()
    done.set()
    assert registry.wait_until_empty(1) == ()
    thread.join()


def test_wait_until_empty_returns_safe_remaining_ids_after_timeout():
    registry = RunRegistry(); registry.register(make_handle("only-safe-id"))
    started = time.monotonic()
    assert registry.wait_until_empty(0.01) == ("only-safe-id",)
    assert time.monotonic() - started < 0.5
