from __future__ import annotations

import json

import pytest

from core.chat_service import ChatService
from core.runtime import (
    ChatRuntimeMode,
    ChatRuntimeSelector,
    CoordinatedRuntimeFactory,
    InMemoryRunEventJournal,
    InMemorySnapshotStore,
    JournalError,
    JournalErrorCode,
    RunRegistry,
    RuntimeEventType,
)
from tests._runtime_assembly_fixtures import FakeRouter, make_services


class _CountingRouter(FakeRouter):
    def __init__(self, *, output="full-e2e-output", error=None) -> None:
        super().__init__()
        self.output = output
        self.error = error
        self.calls = 0

    def complete_single_agent(self, *args, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.output


def _assembled_service(router):
    registry = RunRegistry()
    services = make_services(
        run_registry=registry,
        snapshot_enabled=False,
    )
    factory = CoordinatedRuntimeFactory(router, services)
    service = ChatService(
        router,
        runtime_selector=ChatRuntimeSelector(
            ChatRuntimeMode.COORDINATED
        ),
        coordinated_runtime_factory=factory,
        run_registry=registry,
    )
    return service, services, registry


@pytest.mark.asyncio
async def test_default_composition_root_model_output_and_terminal_matrix():
    router = _CountingRouter()
    service, services, registry = _assembled_service(router)

    events = [
        event
        async for event in service.stream_coordinated_agent_events(
            "core_router", "question", persist=False
        )
    ]

    event_types = [event.event_type for event in events]
    assert service.selected_runtime_mode() is ChatRuntimeMode.COORDINATED
    assert router.calls == 1
    assert event_types.count(RuntimeEventType.OUTPUT_DELTA) == 1
    assert event_types.count(RuntimeEventType.RUN_COMPLETED) == 1
    assert event_types[-1] is RuntimeEventType.RUN_COMPLETED
    assert registry.observability_snapshot()["active_runs"] == 0
    assert services.observability_dispatcher.gauge_provider.channels == set()


@pytest.mark.asyncio
async def test_enabled_snapshot_captures_dynamic_post_plan_checkpoint():
    class CountingSnapshotStore(InMemorySnapshotStore):
        def __init__(self):
            super().__init__()
            self.io_calls = 0

        def save(self, snapshot):
            self.io_calls += 1
            return super().save(snapshot)

        def get(self, snapshot_id):
            self.io_calls += 1
            return super().get(snapshot_id)

        def latest(self, run_id):
            self.io_calls += 1
            return super().latest(run_id)

        def list_for_run(self, run_id, limit):
            self.io_calls += 1
            return super().list_for_run(run_id, limit)

    store = CountingSnapshotStore()
    router = _CountingRouter()
    registry = RunRegistry()
    services = make_services(
        snapshot_store=store,
        run_registry=registry,
        snapshot_enabled=True,
    )
    service = ChatService(
        router,
        coordinated_runtime_factory=CoordinatedRuntimeFactory(
            router, services
        ),
        run_registry=registry,
    )

    chunks = [
        chunk
        async for chunk in service.stream_coordinated_agent_text(
            "core_router", "question", persist=False
        )
    ]

    assert router.calls == 1
    assert "full-e2e-output" in chunks
    assert services.snapshot_enabled is True
    assert services.recovery_enabled is True
    assert store.io_calls == 1
    assert len(store._records) == 1
    assert next(iter(store._records.values())).checkpoint_kind == (
        "POST_PLAN_PRE_EXECUTION"
    )


@pytest.mark.asyncio
async def test_terminal_journal_failure_never_reruns_business_and_cleans_scope():
    class TerminalFailingJournal(InMemoryRunEventJournal):
        def append(self, event):
            if event.event_type is RuntimeEventType.RUN_COMPLETED:
                raise JournalError(
                    JournalErrorCode.JOURNAL_APPEND_FAILED,
                    "journal append failed",
                )
            return super().append(event)

    router = _CountingRouter()
    registry = RunRegistry()
    services = make_services(
        journal=TerminalFailingJournal(),
        run_registry=registry,
        snapshot_enabled=False,
    )
    service = ChatService(
        router,
        coordinated_runtime_factory=CoordinatedRuntimeFactory(
            router, services
        ),
        run_registry=registry,
    )

    chunks = [
        chunk
        async for chunk in service.stream_coordinated_agent_text(
            "core_router", "question", persist=False
        )
    ]

    assert router.calls == 1
    assert chunks.count("full-e2e-output") == 1
    assert "".join(chunks).count(
        "[runtime-error] RUNTIME_EXECUTION_FAILED"
    ) == 1
    assert not any(
        '"event_type":"RUN_COMPLETED"' in chunk for chunk in chunks
    )
    assert registry.observability_snapshot()["active_runs"] == 0
    assert services.observability_dispatcher.gauge_provider.channels == set()
    assert services.snapshot_store is None
    assert services.recovery_validator is None


@pytest.mark.asyncio
async def test_runtime_error_is_safe_single_terminal_and_never_falls_back():
    router = _CountingRouter(
        error=RuntimeError("prompt token db/model/private/path")
    )
    service, services, registry = _assembled_service(router)
    legacy_calls = 0

    def forbidden_legacy(*args, **kwargs):
        nonlocal legacy_calls
        legacy_calls += 1
        yield "forbidden"

    service.stream_chat = forbidden_legacy  # type: ignore[method-assign]
    chunks = [
        chunk
        async for chunk in service.stream_coordinated_agent_text(
            "core_router", "question", persist=False
        )
    ]

    rendered = "".join(chunks)
    controls = [
        json.loads(chunk.removeprefix("[[ORCH]]"))
        for chunk in chunks
        if chunk.startswith("[[ORCH]]")
    ]
    assert router.calls == 1
    assert legacy_calls == 0
    assert sum(
        item["event_type"] == "RUN_COMPLETED" for item in controls
    ) == 1
    assert rendered.count(
        "[runtime-error] RUNTIME_EXECUTION_FAILED"
    ) == 1
    assert "private/path" not in rendered
    assert registry.observability_snapshot()["active_runs"] == 0
    assert services.observability_dispatcher.gauge_provider.channels == set()
