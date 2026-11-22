from __future__ import annotations

import inspect

import pytest

import server
from core.runtime import RuntimeLifecycleState
from core.runtime.events import OutputDeltaPayload, RunCompletedPayload, RuntimeEvent, RuntimeEventDraft, RuntimeEventType
from core.runtime.state import RunStatus, StopReason
from tests.test_runtime_execute_endpoint import _ConnectedRequest, _install_chat_service, _result


class RoutingService:
    def __init__(self) -> None:
        self.coordinated_calls = 0
        self.run_registry = server.RunRegistry()

    async def stream_coordinated_agent_events(self, *args, _result_out=None, **kwargs):
        self.coordinated_calls += 1
        run_id = kwargs["run_id"]
        _result_out.append(_result(RunStatus.SUCCEEDED, StopReason.COMPLETED, run_id=run_id))
        for sequence, (event_type, payload) in enumerate((
            (RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload("coordinated")),
            (RuntimeEventType.RUN_COMPLETED, RunCompletedPayload("SUCCEEDED", "COMPLETED")),
        ), 1):
            yield RuntimeEvent.from_draft(RuntimeEventDraft(
                run_id=run_id, trace_id="test-trace", event_type=event_type, component="test", payload=payload,
            ), sequence)


@pytest.mark.asyncio
async def test_chat_endpoint_routes_only_through_coordinated_runtime(
    monkeypatch,
) -> None:
    service = RoutingService()
    _install_chat_service(monkeypatch, service)

    async def bind_for_routing_test(_request, **kwargs) -> None:
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
        _ConnectedRequest(),
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert [chunk for chunk in chunks if not chunk.startswith("[[ORCH]]")] == ["coordinated"]
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
