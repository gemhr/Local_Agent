from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import server
from core.agent_router import AgentRouter
from core.chat_service import ChatService
from core.memory_manager import MemoryManager
from core.runtime import (
    ChatRuntimeMode,
    ChatRuntimeSelector,
    CoordinatedRuntimeFactory,
    RunRegistry,
)
from tests._runtime_assembly_fixtures import make_coordinated_chat_service, make_services


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


class _FakeModel:
    def __init__(self, output="offline answer", error=None) -> None:
        self.output = output
        self.error = error
        self.calls = 0

    def generate(self, messages, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if "LocalAgent Planner" in messages[0]["content"]:
            yield json.dumps(
                {
                    "schema_version": 1,
                    "decision": "DIRECT_ANSWER",
                    "agent_id": "core_router",
                    "reason_code": "MODEL_DIRECT",
                }
            )
        else:
            yield self.output


async def _endpoint_chunks(service):
    server.chat_service = service
    response = await server.chat_endpoint(
        server.ChatRequest(
            agent_id="core_router",
            query="question",
            run_id="49796282cdb643c7b8850942f7b66bd1",
        ),
        _ConnectedRequest(),
    )
    return response, [chunk async for chunk in response.body_iterator]


def _terminal_count(chunks: list[str]) -> int:
    return sum(
        json.loads(chunk.removeprefix("[[ORCH]]"))["event_type"]
        == "RUN_COMPLETED"
        for chunk in chunks
        if chunk.startswith("[[ORCH]]")
    )


@pytest.mark.asyncio
async def test_api_to_factory_to_output_delta_to_terminal_happy_path(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        model = _FakeModel()
        router = AgentRouter(
            model,
            MemoryManager(str(Path(directory) / "memory.db")),
            orchestration_enabled=False,
        )
        registry = RunRegistry()
        service = make_coordinated_chat_service(
            router,
            run_registry=registry,
        )
        monkeypatch.setattr(server, "chat_service", service)
        response, chunks = await _endpoint_chunks(service)

    wire_text = "".join(
        chunk
        for chunk in chunks
        if not chunk.startswith("[[ORCH]]")
        and not chunk.startswith("[runtime-error]")
    )
    assert response.media_type == "text/plain"
    assert wire_text == "offline answer"
    assert _terminal_count(chunks) == 1
    assert registry.observability_snapshot()["active_runs"] == 0
    # planning + final answer + post-delivery Semantic Formation extraction
    assert model.calls == 3


@pytest.mark.asyncio
async def test_api_coordinated_failure_has_no_legacy_fallback(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        model = _FakeModel(error=RuntimeError("provider secret"))
        router = AgentRouter(
            model,
            MemoryManager(str(Path(directory) / "memory.db")),
            orchestration_enabled=False,
        )
        service = make_coordinated_chat_service(router)

        def forbidden_legacy(**kwargs):
            raise AssertionError("legacy must not run")
            yield  # pragma: no cover

        monkeypatch.setattr(service, "stream_chat", forbidden_legacy)
        monkeypatch.setattr(server, "chat_service", service)
        _response, chunks = await _endpoint_chunks(service)

    rendered = "".join(chunks)
    assert rendered.count("[runtime-error] RUNTIME_EXECUTION_FAILED") == 1
    assert "provider secret" not in rendered
    assert _terminal_count(chunks) == 1
    assert model.calls == 1


@pytest.mark.asyncio
async def test_explicit_legacy_api_does_not_create_coordinated_scope(monkeypatch):
    class ExplodingFactory(CoordinatedRuntimeFactory):
        async def create_run_scope(self, *args, **kwargs):
            raise AssertionError("coordinated scope must not be created")

    with tempfile.TemporaryDirectory() as directory:
        model = _FakeModel("legacy answer")
        router = AgentRouter(
            model,
            MemoryManager(str(Path(directory) / "memory.db")),
            orchestration_enabled=False,
        )
        registry = RunRegistry()
        services = make_services(
            run_registry=registry,
            snapshot_enabled=False,
        )
        service = ChatService(
            router,
            runtime_selector=ChatRuntimeSelector(ChatRuntimeMode.LEGACY),
            coordinated_runtime_factory=ExplodingFactory(router, services),
            run_registry=registry,
        )
        monkeypatch.setattr(server, "chat_service", service)
        response, chunks = await _endpoint_chunks(service)

    assert response.media_type == "text/plain"
    assert "".join(chunks) == "legacy answer"
    assert model.calls == 1
    assert registry.observability_snapshot()["active_runs"] == 0
