from __future__ import annotations

import asyncio

import pytest

import server
from core.runtime import ChatRuntimeMode


class _DisconnectRegistry:
    def __init__(self) -> None:
        self.cancelled = asyncio.Event()
        self.calls = 0

    def cancel(self, run_id, reason):
        self.calls += 1
        self.cancelled.set()
        return True


class _ImmediateDisconnect:
    async def is_disconnected(self) -> bool:
        return True


class _CoordinatedService:
    def __init__(self) -> None:
        self.run_registry = _DisconnectRegistry()
        self.closed = False

    def selected_runtime_mode(self):
        return ChatRuntimeMode.COORDINATED

    async def stream_coordinated_agent_text(self, **kwargs):
        try:
            await self.run_registry.cancelled.wait()
            yield "must-not-reach-client"
        finally:
            self.closed = True


@pytest.mark.asyncio
async def test_disconnect_watcher_stops_output_and_is_awaited(monkeypatch):
    service = _CoordinatedService()
    monkeypatch.setattr(server, "chat_service", service)
    before = set(asyncio.all_tasks())

    response = await server.chat_endpoint(
        server.ChatRequest(
            agent_id="core_router",
            query="hello",
            run_id="49796282cdb643c7b8850942f7b66bd1",
        ),
        _ImmediateDisconnect(),
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == []
    assert service.run_registry.calls == 1
    assert service.closed is True
    await asyncio.sleep(0)
    leaked = [
        task
        for task in asyncio.all_tasks() - before
        if not task.done() and task is not asyncio.current_task()
    ]
    assert leaked == []


class _CancelledService:
    def selected_runtime_mode(self):
        return ChatRuntimeMode.COORDINATED

    async def stream_coordinated_agent_text(self, **kwargs):
        raise asyncio.CancelledError()
        yield  # pragma: no cover


class _Connected:
    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_asgi_cancelled_error_is_cleaned_then_reraised(monkeypatch):
    monkeypatch.setattr(server, "chat_service", _CancelledService())
    response = await server.chat_endpoint(
        server.ChatRequest(
            agent_id="core_router",
            query="hello",
            run_id="49796282cdb643c7b8850942f7b66bd1",
        ),
        _Connected(),
    )
    with pytest.raises(asyncio.CancelledError):
        await anext(response.body_iterator)
