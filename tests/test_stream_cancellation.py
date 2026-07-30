from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from core.chat_service import ChatService
from core.runtime import (
    CancellationReason,
    BoundedBlockingExecutor,
    CoordinatedRuntimeFactory,
    EventChannelState,
    RuntimeEventType,
)
from tests._runtime_assembly_fixtures import FakeRouter, make_services


class _CancellationAwareRouter(FakeRouter):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()

    def complete_single_agent(self, agent_id, query, **kwargs):
        context = kwargs["run_context"]
        self.started.set()
        deadline = time.monotonic() + 1
        while (
            not context.cancellation_token.is_cancelled()
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        context.cancellation_token.raise_if_cancelled()
        return "late-output"


@pytest.mark.asyncio
async def test_external_aclose_cancels_and_drains_capacity_one_channel():
    router = _CancellationAwareRouter()
    base_services = make_services(snapshot_enabled=False)
    step_executor = BoundedBlockingExecutor(
        max_workers=1,
        max_pending_tasks=1,
        thread_name_prefix="test-coordinated-step",
    )
    services = replace(
        base_services,
        blocking_executors=(step_executor,),
        coordinated_step_executor=step_executor,
    )
    factory = CoordinatedRuntimeFactory(
        router, services, event_channel_capacity=1
    )
    service = ChatService(
        router,
        coordinated_runtime_factory=factory,
        run_registry=services.run_registry,
        disconnect_grace_seconds=1,
    )
    stream = service.stream_coordinated_agent_events(
        "agent-a", "question", persist=False
    )

    first = await anext(stream)
    assert first.event_type is RuntimeEventType.RUN_STARTED
    await stream.aclose()

    assert services.run_registry.observability_snapshot()["active_runs"] == 0
    records = services.event_journal.read_after(first.run_id, 0, 100)
    types = [record.event_type for record in records]
    assert types.count(RuntimeEventType.CANCELLATION) == 1
    assert types.count(RuntimeEventType.RUN_COMPLETED) == 1
    assert services.observability_dispatcher.gauge_provider.channels == set()
    assert step_executor.wait_until_idle(0.2) is True
    assert step_executor.shutdown(timeout=0.2) is True


def test_canonical_cancellation_reason_is_first_wins():
    from core.runtime import CancellationSource

    source = CancellationSource()
    assert source.cancel(CancellationReason.CLIENT_DISCONNECTED) is True
    assert source.cancel(CancellationReason.SERVER_SHUTDOWN) is False
    assert (
        source.token.reason
        is CancellationReason.CLIENT_DISCONNECTED
    )
