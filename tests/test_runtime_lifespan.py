from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import server
from core.runtime import ChatRuntimeMode, RuntimeLifecycleState


class ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


class LegacyOnlyService:
    def __init__(self) -> None:
        self.mode_reads = 0
        self.legacy_calls = 0

    def selected_runtime_mode(self) -> ChatRuntimeMode:
        self.mode_reads += 1
        return ChatRuntimeMode.COORDINATED

    def stream_chat(self, **kwargs):
        self.legacy_calls += 1
        yield "legacy"


@pytest.mark.asyncio
async def test_default_chat_endpoint_captures_mode_once_but_stays_legacy(
    monkeypatch,
) -> None:
    service = LegacyOnlyService()
    monkeypatch.setattr(server, "chat_service", service)

    response = await server.chat_endpoint(
        server.ChatRequest(
            agent_id="core_router",
            query="hello",
            run_id="49796282cdb643c7b8850942f7b66bd1",
        ),
        ConnectedRequest(),
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == ["legacy"]
    assert service.mode_reads == 1
    assert service.legacy_calls == 1


def test_lifecycle_states_and_legacy_source_boundary_are_explicit() -> None:
    assert {item.value for item in RuntimeLifecycleState} == {
        "STARTING",
        "READY",
        "SHUTTING_DOWN",
        "CLOSED",
    }
    source = inspect.getsource(server.chat_endpoint)
    assert "service.stream_chat(" in source
    assert "stream_coordinated_agent" not in source


def test_snapshot_production_assembly_is_fail_fast_and_independently_configured(
    monkeypatch,
) -> None:
    monkeypatch.delenv("LOCAL_AGENT_SNAPSHOT_ENABLED", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_SNAPSHOT_DB_PATH", raising=False)
    loaded = server.Settings.load()
    source = inspect.getsource(server.lifespan)

    assert loaded.snapshot_store_enabled is True
    assert Path(loaded.snapshot_store_db_path).name == "runtime_snapshots.db"
    assert "SQLiteSnapshotStore(settings.snapshot_store_db_path)" in source
    assert "InMemorySnapshotStore" not in source
