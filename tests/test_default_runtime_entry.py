from __future__ import annotations

import inspect

import pytest

import server
from core.chat_service import ChatService
from core.runtime import ChatRuntimeMode
from core.settings import Settings


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


class _RoutingService:
    def __init__(
        self,
        mode: ChatRuntimeMode,
        *,
        legacy_error: Exception | None = None,
        coordinated_error: Exception | None = None,
    ) -> None:
        self.mode = mode
        self.legacy_error = legacy_error
        self.coordinated_error = coordinated_error
        self.mode_reads = 0
        self.legacy_calls = 0
        self.coordinated_calls = 0

    def selected_runtime_mode(self) -> ChatRuntimeMode:
        self.mode_reads += 1
        return self.mode

    def stream_chat(self, **kwargs):
        self.legacy_calls += 1
        if self.legacy_error is not None:
            raise self.legacy_error
        yield "legacy"

    async def stream_coordinated_agent_text(self, **kwargs):
        self.coordinated_calls += 1
        if self.coordinated_error is not None:
            raise self.coordinated_error
        yield "coordinated"


async def _request(monkeypatch, service):
    monkeypatch.setattr(server, "chat_service", service)
    response = await server.chat_endpoint(
        server.ChatRequest(
            agent_id="core_router",
            query="hello",
            run_id="49796282cdb643c7b8850942f7b66bd1",
        ),
        _ConnectedRequest(),
    )
    return response, [chunk async for chunk in response.body_iterator]


def test_default_settings_select_coordinated_and_snapshot_disabled(monkeypatch):
    monkeypatch.delenv("CHAT_RUNTIME_MODE", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_SNAPSHOT_ENABLED", raising=False)
    settings = Settings.load()
    assert settings.chat_runtime_mode is ChatRuntimeMode.COORDINATED
    assert settings.snapshot_store_enabled is False


def test_illegal_mode_fails_during_settings_load(monkeypatch):
    monkeypatch.setenv("CHAT_RUNTIME_MODE", "fallback")
    with pytest.raises(ValueError, match="CHAT_RUNTIME_MODE"):
        Settings.load()


@pytest.mark.asyncio
async def test_coordinated_and_legacy_routes_are_strictly_mutually_exclusive(
    monkeypatch,
):
    coordinated = _RoutingService(ChatRuntimeMode.COORDINATED)
    response, chunks = await _request(monkeypatch, coordinated)
    assert chunks == ["coordinated"]
    assert coordinated.mode_reads == 1
    assert coordinated.coordinated_calls == 1
    assert coordinated.legacy_calls == 0
    assert response.media_type == "text/plain"

    legacy = _RoutingService(ChatRuntimeMode.LEGACY)
    _response, chunks = await _request(monkeypatch, legacy)
    assert chunks == ["legacy"]
    assert legacy.mode_reads == 1
    assert legacy.legacy_calls == 1
    assert legacy.coordinated_calls == 0


@pytest.mark.asyncio
async def test_neither_runtime_falls_back_to_the_other_after_failure(monkeypatch):
    coordinated = _RoutingService(
        ChatRuntimeMode.COORDINATED,
        coordinated_error=RuntimeError("provider secret"),
    )
    _response, chunks = await _request(monkeypatch, coordinated)
    assert chunks == ["[runtime-error] RUNTIME_EXECUTION_FAILED\n"]
    assert coordinated.coordinated_calls == 1
    assert coordinated.legacy_calls == 0

    legacy = _RoutingService(
        ChatRuntimeMode.LEGACY,
        legacy_error=RuntimeError("provider secret"),
    )
    _response, chunks = await _request(monkeypatch, legacy)
    assert chunks == ["[runtime-error] RUNTIME_EXECUTION_FAILED\n"]
    assert legacy.legacy_calls == 1
    assert legacy.coordinated_calls == 0
    assert "provider secret" not in "".join(chunks)


@pytest.mark.asyncio
async def test_factory_missing_returns_fixed_configuration_error():
    service = ChatService(object())  # type: ignore[arg-type]
    chunks = [
        item
        async for item in service.stream_coordinated_agent_text(
            "core_router",
            "hello",
        )
    ]
    assert chunks == ["[runtime-error] RUNTIME_CONFIGURATION_ERROR\n"]


def test_coordinated_entry_contains_no_manual_runtime_assembly_fallback():
    source = inspect.getsource(ChatService.stream_coordinated_agent_events)
    for forbidden in (
        "create_run_context",
        "RuntimeEventChannel(",
        "RunCoordinator(",
        "SerialScheduler(",
        "ParallelExecutor(",
        "stream_chat(",
    ):
        assert forbidden not in source
