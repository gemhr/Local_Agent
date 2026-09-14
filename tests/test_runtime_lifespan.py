from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

import server
from core.runtime import RunRegistry, RuntimeLifecycleState


class ConnectedRequest:
    def __init__(self) -> None:
        self.state = SimpleNamespace(
            principal=SimpleNamespace(authz_domain_id="test-user")
        )

    async def is_disconnected(self) -> bool:
        return False


class RoutingService:
    def __init__(self) -> None:
        self.coordinated_calls = 0
        self.run_registry = RunRegistry()

    async def stream_coordinated_agent_text(self, **kwargs):
        self.coordinated_calls += 1
        yield "coordinated"


@pytest.mark.asyncio
async def test_chat_endpoint_routes_only_through_coordinated_runtime(
    monkeypatch,
) -> None:
    service = RoutingService()
    monkeypatch.setattr(server.app.state, "chat_service", service, raising=False)

    async def bind_for_routing_test(_request, *, run_id: str, agent_id: str) -> None:
        return None

    monkeypatch.setattr(
        server, "_bind_new_run_and_conversation", bind_for_routing_test
    )

    response = await server.chat_endpoint(
        server.ChatRequest(
            agent_id="core_router",
            query="hello",
            run_id="49796282cdb643c7b8850942f7b66bd1",
        ),
        ConnectedRequest(),
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == ["coordinated"]
    assert service.coordinated_calls == 1


def test_lifecycle_states_and_canonical_runtime_configuration_are_explicit(
    monkeypatch,
) -> None:
    assert {item.value for item in RuntimeLifecycleState} == {
        "STARTING",
        "READY",
        "SHUTTING_DOWN",
        "CLOSED",
    }
    loaded = server.Settings.load()
    assert not hasattr(loaded, "chat_runtime_mode")
    assert "CHAT_RUNTIME_MODE" not in inspect.getsource(server.Settings.load)


def test_snapshot_production_assembly_is_fail_fast_and_independently_configured(
    monkeypatch,
) -> None:
    monkeypatch.delenv("LOCAL_AGENT_SNAPSHOT_ENABLED", raising=False)
    loaded = server.Settings.load()
    source = inspect.getsource(server.lifespan)

    assert loaded.snapshot_store_enabled is False
    assert "PostgresSnapshotStore(persistence_database)" in source
    assert "SQLiteSnapshotStore" not in source
    assert "InMemorySnapshotStore" not in source
