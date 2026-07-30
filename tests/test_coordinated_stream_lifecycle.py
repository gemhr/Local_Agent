from __future__ import annotations

import json

import pytest

from core.chat_service import ChatService
from core.runtime import (
    ChatStreamCompatibilityAdapter,
    ChatStreamProtocolError,
    CoordinatedRuntimeFactory,
    EventChannelState,
    RunRegistry,
)
from tests._runtime_assembly_fixtures import FakeRouter, make_services


class _CountingRunRegistry(RunRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.register_count = 0
        self.unregister_count = 0

    def register(self, handle):
        self.register_count += 1
        return super().register(handle)

    def unregister(self, run_id: str) -> bool:
        self.unregister_count += 1
        return super().unregister(run_id)


class _RecordingFactory(CoordinatedRuntimeFactory):
    def __init__(self, router, services, **kwargs) -> None:
        super().__init__(router, services, **kwargs)
        self.create_count = 0
        self.scopes = []

    async def create_run_scope(self, *args, **kwargs):
        self.create_count += 1
        scope = await super().create_run_scope(*args, **kwargs)
        self.scopes.append(scope)
        return scope


def _service(router=None, *, capacity=32):
    active_router = router or FakeRouter()
    registry = _CountingRunRegistry()
    services = make_services(
        run_registry=registry,
        snapshot_enabled=False,
    )
    factory = _RecordingFactory(
        active_router,
        services,
        event_channel_capacity=capacity,
    )
    service = ChatService(
        active_router,
        coordinated_runtime_factory=factory,
        run_registry=registry,
    )
    return service, registry, services, factory


def _control_types(chunks: list[str]) -> list[str]:
    return [
        json.loads(chunk.removeprefix("[[ORCH]]"))["event_type"]
        for chunk in chunks
        if chunk.startswith("[[ORCH]]")
    ]


@pytest.mark.asyncio
async def test_normal_completion_closes_scope_channel_and_registry_once():
    service, registry, services, factory = _service()

    chunks = [
        chunk
        async for chunk in service.stream_coordinated_agent_text(
            "agent-a",
            "question",
            persist=False,
        )
    ]

    assert factory.create_count == 1
    assert len(factory.scopes) == 1
    scope = factory.scopes[0]
    assert scope.is_executed is True
    assert scope.is_closed is True
    assert scope.event_channel.state is EventChannelState.CLOSED
    assert registry.register_count == 1
    assert registry.unregister_count == 1
    assert registry.observability_snapshot()["active_runs"] == 0
    assert services.observability_dispatcher.gauge_provider.channels == set()
    assert [chunk for chunk in chunks if not chunk.startswith("[[ORCH]]")] == [
        "assembled-output"
    ]
    assert _control_types(chunks).count("RUN_COMPLETED") == 1


@pytest.mark.asyncio
async def test_runtime_failure_is_safe_has_one_terminal_and_does_not_double_run():
    class FailingRouter(FakeRouter):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def complete_single_agent(self, *args, **kwargs) -> str:
            self.calls += 1
            raise RuntimeError("C:/secret provider token=abc")

    router = FailingRouter()
    service, registry, services, factory = _service(router)

    chunks = [
        chunk
        async for chunk in service.stream_coordinated_agent_text(
            "agent-a",
            "question",
            persist=False,
        )
    ]

    rendered = "".join(chunks)
    assert router.calls == 1
    assert factory.create_count == 1
    assert rendered.count("[runtime-error] RUNTIME_EXECUTION_FAILED") == 1
    assert "secret" not in rendered.lower()
    assert _control_types(chunks).count("RUN_COMPLETED") == 1
    assert registry.observability_snapshot()["active_runs"] == 0
    assert factory.scopes[0].is_closed is True
    assert services.observability_dispatcher.gauge_provider.channels == set()


@pytest.mark.asyncio
async def test_adapter_encoding_failure_aborts_current_scope_and_cleans_registry(
    monkeypatch,
):
    service, registry, services, factory = _service(capacity=1)
    original = ChatStreamCompatibilityAdapter.adapt
    calls = 0

    def fail_second(self, event):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ChatStreamProtocolError("RUNTIME_STREAM_ENCODING_FAILED")
        return original(self, event)

    monkeypatch.setattr(ChatStreamCompatibilityAdapter, "adapt", fail_second)

    chunks = [
        chunk
        async for chunk in service.stream_coordinated_agent_text(
            "agent-a",
            "question",
            persist=False,
        )
    ]

    assert chunks[-1] == "[runtime-error] RUNTIME_STREAM_ENCODING_FAILED\n"
    scope = factory.scopes[0]
    assert scope.is_closed is True
    assert scope.event_channel.state is EventChannelState.ABORTED
    assert registry.observability_snapshot()["active_runs"] == 0
    assert services.observability_dispatcher.gauge_provider.channels == set()


@pytest.mark.asyncio
async def test_scope_creation_failure_is_fixed_safe_error_and_never_calls_legacy():
    class BrokenFactory(CoordinatedRuntimeFactory):
        async def create_run_scope(self, *args, **kwargs):
            raise RuntimeError("D:/secret/runtime.db")

    router = FakeRouter()
    services = make_services(snapshot_enabled=False)
    service = ChatService(
        router,
        coordinated_runtime_factory=BrokenFactory(router, services),
    )
    service.stream_chat = lambda **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("legacy must not run")
    )

    chunks = [
        chunk
        async for chunk in service.stream_coordinated_agent_text(
            "agent-a",
            "question",
        )
    ]
    assert chunks == ["[runtime-error] RUNTIME_SCOPE_CREATION_FAILED\n"]
    assert "secret" not in "".join(chunks).lower()


@pytest.mark.asyncio
async def test_scope_close_and_abort_are_bounded_idempotent_owners():
    services = make_services(snapshot_enabled=False)
    scope = await CoordinatedRuntimeFactory(
        FakeRouter(),
        services,
    ).create_run_scope("agent-a", "question")

    await scope.abort()
    await scope.abort()
    await scope.close()

    assert scope.is_closed is True
    assert scope.event_channel.state is EventChannelState.ABORTED
    assert services.observability_dispatcher.gauge_provider.channels == set()
