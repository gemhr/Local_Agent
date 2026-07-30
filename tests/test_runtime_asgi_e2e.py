from __future__ import annotations

import asyncio
import json

import pytest

import server
from core.runtime import CancellationReason, ChatRuntimeMode


class AsgiChatHarness:
    """Drive the FastAPI app through the real HTTP ASGI callable."""

    def __init__(self, *, send_error: type[Exception] | None = None) -> None:
        self.disconnect = asyncio.Event()
        self.messages: list[dict] = []
        self._request_sent = False
        self._send_error = send_error

    async def receive(self) -> dict:
        if not self._request_sent:
            self._request_sent = True
            return {
                "type": "http.request",
                "body": json.dumps(
                    {
                        "agent_id": "core_router",
                        "query": "question",
                        "run_id": "49796282cdb643c7b8850942f7b66bd1",
                    }
                ).encode(),
                "more_body": False,
            }
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: dict) -> None:
        self.messages.append(dict(message))
        if (
            self._send_error is not None
            and message["type"] == "http.response.body"
            and message.get("body")
        ):
            raise self._send_error("transport closed")

    async def run(self) -> None:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/chat",
            "raw_path": b"/api/chat",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"test"),
                (b"content-type", b"application/json"),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        }
        await server.app(scope, self.receive, self.send)

    @property
    def body(self) -> bytes:
        return b"".join(
            message.get("body", b"")
            for message in self.messages
            if message["type"] == "http.response.body"
        )


class _Registry:
    def __init__(self) -> None:
        self.cancel_calls: list[CancellationReason] = []
        self.cancelled = asyncio.Event()

    def cancel(self, run_id, reason):
        self.cancel_calls.append(reason)
        self.cancelled.set()
        return len(self.cancel_calls) == 1


class _AsgiService:
    def __init__(self, chunks=("one", "two"), *, block=False) -> None:
        self.run_registry = _Registry()
        self.chunks = chunks
        self.block = block
        self.started = asyncio.Event()
        self.closed = False

    def selected_runtime_mode(self):
        return ChatRuntimeMode.COORDINATED

    async def stream_coordinated_agent_text(self, **kwargs):
        self.started.set()
        try:
            if self.block:
                await self.run_registry.cancelled.wait()
            for chunk in self.chunks:
                yield chunk
                await asyncio.sleep(0)
        finally:
            self.closed = True


@pytest.mark.asyncio
async def test_real_asgi_normal_stream_completes_and_awaits_watcher(monkeypatch):
    service = _AsgiService()
    monkeypatch.setattr(server, "chat_service", service)
    harness = AsgiChatHarness()
    before = set(asyncio.all_tasks())

    await asyncio.wait_for(harness.run(), 1)

    assert harness.body == b"onetwo"
    assert service.closed is True
    assert service.run_registry.cancel_calls == []
    await asyncio.sleep(0)
    assert [
        task
        for task in asyncio.all_tasks() - before
        if not task.done() and task is not asyncio.current_task()
    ] == []


@pytest.mark.asyncio
async def test_real_asgi_disconnect_during_stream_sends_no_late_body(monkeypatch):
    service = _AsgiService(chunks=("late",), block=True)
    monkeypatch.setattr(server, "chat_service", service)
    harness = AsgiChatHarness()
    request_task = asyncio.create_task(harness.run())
    await asyncio.wait_for(service.started.wait(), 0.5)

    harness.disconnect.set()
    await asyncio.wait_for(request_task, 1)

    assert harness.body == b""
    assert service.closed is True
    assert service.run_registry.cancel_calls == [
        CancellationReason.CLIENT_DISCONNECTED
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("send_error", [BrokenPipeError, ConnectionResetError])
async def test_real_asgi_send_failure_closes_stream_without_safe_error_body(
    monkeypatch, send_error
):
    service = _AsgiService(chunks=("must-fail-send",))
    monkeypatch.setattr(server, "chat_service", service)
    harness = AsgiChatHarness(send_error=send_error)

    with pytest.raises(Exception) as captured:
        await asyncio.wait_for(harness.run(), 1)

    assert captured.value.__class__.__name__ == "ClientDisconnect"
    assert b"[runtime-error]" not in harness.body
    assert service.closed is True


@pytest.mark.asyncio
async def test_real_asgi_body_task_cancellation_is_not_swallowed(monkeypatch):
    service = _AsgiService(chunks=("late",), block=True)
    monkeypatch.setattr(server, "chat_service", service)
    harness = AsgiChatHarness()
    request_task = asyncio.create_task(harness.run())
    await asyncio.wait_for(service.started.wait(), 0.5)

    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert service.closed is True
    assert service.run_registry.cancel_calls == [
        CancellationReason.CLIENT_DISCONNECTED
    ]
