from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
import uuid

import pytest

import server
from core.chat_service import ChatService
from core.runtime import (
    CancellationReason,
    CoordinatedRuntimeFactory,
    RunCancelledError,
    RunRegistry,
    RunStatus,
)
from tests._runtime_assembly_fixtures import FakeRouter, make_services


class _RecordingFactory(CoordinatedRuntimeFactory):
    """记录 create_run_scope 的 timeout_seconds 参数并保留创建的 scope。"""

    def __init__(self, router, services, **kwargs) -> None:
        super().__init__(router, services, **kwargs)
        self.create_count = 0
        self.last_kwargs: dict | None = None
        self.scopes = []

    async def create_run_scope(self, *args, **kwargs):
        self.create_count += 1
        self.last_kwargs = kwargs
        scope = await super().create_run_scope(*args, **kwargs)
        self.scopes.append(scope)
        return scope


class _BlockingRouter(FakeRouter):
    """在 answer 步骤内阻塞，直到 run 被取消或被显式释放。"""

    def __init__(self, started: threading.Event, release: threading.Event):
        super().__init__()
        self.started = started
        self.release = release

    def complete_single_agent(
        self, agent_id: str, query: str, *, run_context=None, **kwargs
    ) -> str:
        self.started.set()
        token = run_context.cancellation_token
        while not token.is_cancelled() and not self.release.is_set():
            time.sleep(0.005)
        if token.is_cancelled():
            raise RunCancelledError(CancellationReason.REQUEST_CANCELLED)
        return "assembled-output"


def _service(router=None, *, registry=None):
    active_router = router or FakeRouter()
    active_registry = registry or RunRegistry()
    services = make_services(run_registry=active_registry, snapshot_enabled=False)
    factory = _RecordingFactory(active_router, services)
    service = ChatService(
        active_router,
        coordinated_runtime_factory=factory,
        run_registry=active_registry,
    )
    return service, active_registry, services, factory


def _remaining_bounds(scope) -> None:
    """断言 deadline 已被真实创建且在请求的 timeout 范围内。"""
    remaining = scope.run_context.remaining_seconds()
    assert remaining is not None
    assert scope.run_context.data.deadline_at is not None
    assert 0 < remaining <= 2.0


# ---------------------------------------------------------------------------
# Test Group D — timeout_seconds deadline propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_seconds_reaches_run_context_deadline():
    service, registry, _services, factory = _service()
    run_id = uuid.uuid4().hex

    _output, result = await service.run_coordinated_agent(
        "core_router",
        "test",
        run_id=run_id,
        timeout_seconds=2.0,
        persist=False,
    )

    assert factory.create_count == 1
    assert factory.last_kwargs["run_id"] == run_id
    assert factory.last_kwargs["timeout_seconds"] == 2.0
    _remaining_bounds(factory.scopes[0])
    assert result.run_id == run_id
    assert result.status is RunStatus.SUCCEEDED
    assert registry.observability_snapshot()["active_runs"] == 0


@pytest.mark.asyncio
async def test_timeout_propagates_through_http_endpoint(monkeypatch):
    service, registry, _services, factory = _service()
    monkeypatch.setattr(server, "chat_service", service)
    run_id = uuid.uuid4().hex

    response = await server.runtime_execute_endpoint(
        server.RuntimeExecuteRequest(
            agent_id="core_router",
            query="test",
            run_id=run_id,
            timeout_seconds=2.0,
        )
    )

    assert response.status_code == 200
    assert factory.create_count == 1
    assert factory.last_kwargs["timeout_seconds"] == 2.0
    _remaining_bounds(factory.scopes[0])
    body = json.loads(response.body)
    assert body["run_id"] == run_id
    assert body["status"] == "SUCCEEDED"
    assert registry.observability_snapshot()["active_runs"] == 0


@pytest.mark.asyncio
async def test_no_timeout_means_no_deadline():
    service, _registry, _services, factory = _service()

    await service.run_coordinated_agent(
        "core_router",
        "test",
        run_id=uuid.uuid4().hex,
        timeout_seconds=None,
        persist=False,
    )

    scope = factory.scopes[0]
    assert scope.run_context.remaining_seconds() is None
    assert scope.run_context.data.deadline_at is None


def test_structured_path_adds_no_second_deadline_primitive():
    """新 structured 调用链只透传 timeout，不构造第二套 deadline primitive。"""
    sources = [
        inspect.getsource(server.runtime_execute_endpoint),
        inspect.getsource(ChatService.run_coordinated_agent),
        inspect.getsource(ChatService.stream_coordinated_agent_events),
        inspect.getsource(ChatService._stream_factory_coordinated_events),
    ]
    for forbidden in ("Deadline(", "create_run_context(", "threading.Timer"):
        assert all(forbidden not in source for source in sources)


# ---------------------------------------------------------------------------
# Test Group E — existing cancel route locates structured endpoint run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_cancel_route_finds_structured_endpoint_run(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    router = _BlockingRouter(started, release)
    service, registry, _services, _factory = _service(router)
    monkeypatch.setattr(server, "chat_service", service)
    run_id = uuid.uuid4().hex

    task = asyncio.create_task(
        server.runtime_execute_endpoint(
            server.RuntimeExecuteRequest(
                agent_id="core_router",
                query="test",
                run_id=run_id,
                timeout_seconds=30.0,
            )
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 5.0)
        # structured endpoint 使用 caller 提供的同一 run_id 注册到既有 RunRegistry。
        handle = registry.get(run_id)
        assert handle is not None
        assert handle.run_id == run_id

        cancel_response = await server.cancel_run_endpoint(run_id)
        assert cancel_response == {"status": "cancelled", "run_id": run_id}
        assert handle.cancellation_source.token.reason is (
            CancellationReason.REQUEST_CANCELLED
        )

        response = await asyncio.wait_for(task, timeout=10.0)
        body = json.loads(response.body)
        assert body["status"] == "CANCELLED"
        assert body["stop_reason"] == "USER_CANCELLED"
        assert body["error_code"] == "REQUEST_CANCELLED"
        assert registry.observability_snapshot()["active_runs"] == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
