from __future__ import annotations

from typing import Iterator

import pytest

import server
from core.runtime import CancellationReason, ChatRuntimeMode, RunCancelledError


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


class _ScriptedStream(Iterator[str]):
    def __init__(self, terminal_error: Exception) -> None:
        self._terminal_error = terminal_error
        self._yielded = False
        self.closed = False

    def __iter__(self) -> "_ScriptedStream":
        return self

    def __next__(self) -> str:
        if not self._yielded:
            self._yielded = True
            return "partial"
        raise self._terminal_error

    def close(self) -> None:
        self.closed = True


class _FakeChatService:
    def __init__(self, stream: _ScriptedStream) -> None:
        self.stream = stream

    def selected_runtime_mode(self) -> ChatRuntimeMode:
        return ChatRuntimeMode.LEGACY

    def stream_chat(
        self,
        agent_id: str,
        query: str,
        file_path: str = "",
        run_id: str | None = None,
    ) -> Iterator[str]:
        return self.stream


async def _response_chunks(response) -> list[str]:
    return [chunk async for chunk in response.body_iterator]


@pytest.mark.asyncio
async def test_chat_stream_treats_run_cancellation_as_normal_completion(monkeypatch) -> None:
    stream = _ScriptedStream(RunCancelledError(CancellationReason.USER_CANCELLED))
    monkeypatch.setattr(server, "chat_service", _FakeChatService(stream))

    response = await server.chat_endpoint(
        server.ChatRequest(
            agent_id="core_router",
            query="hello",
            run_id="49796282cdb643c7b8850942f7b66bd1",
        ),
        _ConnectedRequest(),
    )

    assert await _response_chunks(response) == ["partial"]
    assert stream.closed is True


@pytest.mark.asyncio
async def test_chat_stream_projects_unexpected_errors_safely(monkeypatch) -> None:
    stream = _ScriptedStream(RuntimeError("unexpected"))
    monkeypatch.setattr(server, "chat_service", _FakeChatService(stream))

    response = await server.chat_endpoint(
        server.ChatRequest(
            agent_id="core_router",
            query="hello",
            run_id="49796282cdb643c7b8850942f7b66bd1",
        ),
        _ConnectedRequest(),
    )

    assert await _response_chunks(response) == [
        "partial",
        "[runtime-error] RUNTIME_EXECUTION_FAILED\n",
    ]
    assert stream.closed is True
