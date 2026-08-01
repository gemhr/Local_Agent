from __future__ import annotations

import pytest

import server
from core.runtime import ChatRuntimeMode
from core.settings import Settings


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


class _SelectedService:
    def __init__(self, mode: ChatRuntimeMode) -> None:
        self.mode = mode
        self.mode_reads = 0
        self.legacy_calls = 0
        self.coordinated_calls = 0

    def selected_runtime_mode(self) -> ChatRuntimeMode:
        self.mode_reads += 1
        return self.mode

    def stream_chat(self, **kwargs):
        self.legacy_calls += 1
        raise RuntimeError("legacy failure")
        yield  # pragma: no cover

    async def stream_coordinated_agent_text(self, **kwargs):
        self.coordinated_calls += 1
        raise RuntimeError("coordinated failure")
        yield  # pragma: no cover


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [ChatRuntimeMode.COORDINATED, ChatRuntimeMode.LEGACY])
async def test_selected_runtime_fails_in_place_without_cross_runtime_fallback(
    monkeypatch, mode
) -> None:
    service = _SelectedService(mode)
    monkeypatch.setattr(server, "chat_service", service)
    response = await server.chat_endpoint(
        server.ChatRequest(
            agent_id="agent-a",
            query="question",
            run_id="49796282cdb643c7b8850942f7b66bd1",
        ),
        _ConnectedRequest(),
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == ["[runtime-error] RUNTIME_EXECUTION_FAILED\n"]
    assert service.mode_reads == 1
    assert service.coordinated_calls == int(mode is ChatRuntimeMode.COORDINATED)
    assert service.legacy_calls == int(mode is ChatRuntimeMode.LEGACY)


def test_default_is_coordinated_and_legacy_requires_explicit_configuration(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CHAT_RUNTIME_MODE", raising=False)
    assert Settings.load().chat_runtime_mode is ChatRuntimeMode.COORDINATED
    monkeypatch.setenv("CHAT_RUNTIME_MODE", "LEGACY")
    assert Settings.load().chat_runtime_mode is ChatRuntimeMode.LEGACY
