from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import server
from core.chat_service import ChatService
from core.request_payload import REQUEST_PAYLOAD_POLICY
from core.runtime import (
    BudgetLedger,
    ChatRuntimeMode,
    ChatRuntimeSelector,
    CoordinatedRuntimeFactory,
    RunBudget,
    RunCoordinatorResult,
    RunRegistry,
    RunStatus,
    StopReason,
)
from core.runtime.state import AgentState
from tests._runtime_assembly_fixtures import FakeRouter, make_services


class _ConnectedRequest:
    """面向 chat_endpoint 的最小连接探针，模拟始终在线的客户端。"""

    async def is_disconnected(self) -> bool:
        return False


class _EmptyChannel:
    """Stub scope 的空事件通道：立即结束并支持关闭。"""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def close(self) -> None:
        return None


class _StubScope:
    """只回放脚本化 RunCoordinatorResult 的最小 CoordinatedRunScope 替身。"""

    def __init__(self, result: RunCoordinatorResult) -> None:
        self._result = result
        self.agent_state = AgentState.for_run_context(result.run_id)
        self._channel = _EmptyChannel()

    @property
    def run_id(self) -> str:
        return self._result.run_id

    @property
    def event_channel(self) -> _EmptyChannel:
        return self._channel

    async def execute(self) -> RunCoordinatorResult:
        return self._result

    def bind_producer_task(self, task) -> None:
        return None

    @property
    def is_closed(self) -> bool:
        return True

    async def close(self, *, abort: bool = False) -> None:
        return None

    async def request_cancel(self, reason) -> bool:
        return False

    async def drain_and_close(self, timeout: float) -> bool:
        return True

    async def force_abort(self, reason) -> None:
        return None


class _ScriptedFactory(CoordinatedRuntimeFactory):
    """记录调用并把 create_run_scope 替换为脚本化 stub scope 的 factory。"""

    def __init__(self, router, services, result, **kwargs) -> None:
        super().__init__(router, services, **kwargs)
        self._result = result
        self.create_count = 0
        self.last_kwargs: dict | None = None

    async def create_run_scope(self, *args, **kwargs):
        self.create_count += 1
        self.last_kwargs = kwargs
        return _StubScope(self._result)


class _ExplodingFactory(CoordinatedRuntimeFactory):
    """任何 create_run_scope 调用都失败的 factory，用于证明未进入 Runtime。"""

    def __init__(self, router, services, **kwargs) -> None:
        super().__init__(router, services, **kwargs)
        self.create_count = 0

    async def create_run_scope(self, *args, **kwargs):
        self.create_count += 1
        raise AssertionError("coordinated scope must not be created")


def _result(
    status: RunStatus,
    stop_reason: StopReason,
    *,
    run_id: str | None = None,
    error_code: str | None = None,
    safe_message: str = "",
) -> RunCoordinatorResult:
    ledger = BudgetLedger(RunBudget())
    succeeded = ("answer",) if status is RunStatus.SUCCEEDED else ()
    failed = ("answer",) if status is RunStatus.FAILED else ()
    cancelled = ("answer",) if status is RunStatus.CANCELLED else ()
    return RunCoordinatorResult(
        run_id=run_id or uuid.uuid4().hex,
        plan_id="plan-1",
        status=status,
        stop_reason=stop_reason,
        succeeded_step_ids=succeeded,
        failed_step_ids=failed,
        cancelled_step_ids=cancelled,
        blocked_step_ids=(),
        budget_snapshot=ledger.snapshot(),
        cleanup_error_codes=(),
        error_code=error_code,
        safe_message=safe_message,
    )


def _scripted_service(result: RunCoordinatorResult):
    router = FakeRouter()
    registry = RunRegistry()
    services = make_services(run_registry=registry, snapshot_enabled=False)
    factory = _ScriptedFactory(router, services, result)
    service = ChatService(
        router,
        coordinated_runtime_factory=factory,
        run_registry=registry,
    )
    return service, factory, registry


def _payload(
    *,
    run_id: str | None = None,
    agent_id: str = "core_router",
    query: str = "test",
    timeout_seconds: float = 30.0,
) -> server.RuntimeExecuteRequest:
    return server.RuntimeExecuteRequest(
        agent_id=agent_id,
        query=query,
        run_id=run_id or uuid.uuid4().hex,
        timeout_seconds=timeout_seconds,
    )


async def _execute(request: server.RuntimeExecuteRequest):
    return await server.runtime_execute_endpoint(request)


# ---------------------------------------------------------------------------
# Test Group A — strict request validation（fail closed）
# ---------------------------------------------------------------------------


def test_valid_request_accepts_all_required_fields():
    run_id = uuid.uuid4().hex
    request = server.RuntimeExecuteRequest(
        agent_id="core_router",
        query="test",
        run_id=run_id,
        timeout_seconds=30.0,
    )
    assert request.run_id == run_id
    assert request.timeout_seconds == 30.0


@pytest.mark.parametrize("missing", ["run_id", "timeout_seconds"])
def test_missing_required_field_is_rejected(missing):
    payload = {
        "agent_id": "core_router",
        "query": "test",
        "run_id": uuid.uuid4().hex,
        "timeout_seconds": 30.0,
    }
    payload.pop(missing)
    with pytest.raises(ValidationError):
        server.RuntimeExecuteRequest(**payload)


def test_extra_field_is_rejected_by_extra_forbid():
    payload = {
        "agent_id": "core_router",
        "query": "test",
        "run_id": uuid.uuid4().hex,
        "timeout_seconds": 30.0,
        "file_path": "x",
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        server.RuntimeExecuteRequest(**payload)


@pytest.mark.asyncio
async def test_invalid_run_id_rejected_before_runtime(monkeypatch):
    service, factory, registry = _scripted_service(
        _result(RunStatus.SUCCEEDED, StopReason.COMPLETED)
    )
    monkeypatch.setattr(server, "chat_service", service)
    with pytest.raises(HTTPException) as exc_info:
        await _execute(
            server.RuntimeExecuteRequest(
                agent_id="core_router",
                query="test",
                run_id="not-a-uuid",
                timeout_seconds=30.0,
            )
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "invalid run_id"
    assert factory.create_count == 0
    assert registry.observability_snapshot()["active_runs"] == 0


@pytest.mark.parametrize(
    "bad_timeout",
    [0, -1, float("nan"), float("inf"), 3_600.01],
)
def test_invalid_timeout_boundaries_rejected(bad_timeout):
    with pytest.raises(ValidationError):
        server.RuntimeExecuteRequest(
            agent_id="core_router",
            query="test",
            run_id=uuid.uuid4().hex,
            timeout_seconds=bad_timeout,
        )


def test_timeout_upper_bound_3600_accepted():
    request = server.RuntimeExecuteRequest(
        agent_id="core_router",
        query="test",
        run_id=uuid.uuid4().hex,
        timeout_seconds=3_600.0,
    )
    assert request.timeout_seconds == 3_600.0


def test_agent_id_length_reuses_request_policy():
    with pytest.raises(ValidationError):
        server.RuntimeExecuteRequest(
            agent_id="a" * (REQUEST_PAYLOAD_POLICY.AGENT_ID_MAX_CHARS + 1),
            query="test",
            run_id=uuid.uuid4().hex,
            timeout_seconds=30.0,
        )
    request = server.RuntimeExecuteRequest(
        agent_id="a" * REQUEST_PAYLOAD_POLICY.AGENT_ID_MAX_CHARS,
        query="test",
        run_id=uuid.uuid4().hex,
        timeout_seconds=30.0,
    )
    assert len(request.agent_id) == REQUEST_PAYLOAD_POLICY.AGENT_ID_MAX_CHARS


def test_query_length_reuses_request_policy():
    with pytest.raises(ValidationError):
        server.RuntimeExecuteRequest(
            agent_id="core_router",
            query="x" * (REQUEST_PAYLOAD_POLICY.CHAT_QUERY_MAX_CHARS + 1),
            run_id=uuid.uuid4().hex,
            timeout_seconds=30.0,
        )


# ---------------------------------------------------------------------------
# Test Group B — COORDINATED-only admission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinated_mode_routes_through_structured_helper(monkeypatch):
    run_id = uuid.uuid4().hex
    result = _result(
        RunStatus.SUCCEEDED,
        StopReason.COMPLETED,
        run_id=run_id,
        safe_message="运行已成功完成",
    )
    service, factory, _registry = _scripted_service(result)
    monkeypatch.setattr(server, "chat_service", service)

    response = await _execute(_payload(run_id=run_id))

    assert response.status_code == 200
    assert factory.create_count == 1
    assert factory.last_kwargs["run_id"] == run_id
    assert factory.last_kwargs["timeout_seconds"] == 30.0
    body = json.loads(response.body)
    assert body["status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_legacy_mode_rejected_without_fallback(monkeypatch):
    router = FakeRouter()
    registry = RunRegistry()
    services = make_services(run_registry=registry, snapshot_enabled=False)
    factory = _ExplodingFactory(router, services)
    service = ChatService(
        router,
        runtime_selector=ChatRuntimeSelector(ChatRuntimeMode.LEGACY),
        coordinated_runtime_factory=factory,
        run_registry=registry,
    )

    def forbidden_legacy(**kwargs):
        raise AssertionError("legacy must not run")

    monkeypatch.setattr(service, "stream_chat", forbidden_legacy)
    monkeypatch.setattr(server, "chat_service", service)

    with pytest.raises(HTTPException) as exc_info:
        await _execute(_payload())
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "COORDINATED_RUNTIME_REQUIRED"
    assert factory.create_count == 0
    assert registry.observability_snapshot()["active_runs"] == 0


@pytest.mark.asyncio
async def test_closed_admission_rejects_without_creating_run(monkeypatch):
    service, factory, registry = _scripted_service(
        _result(RunStatus.SUCCEEDED, StopReason.COMPLETED)
    )
    service.admission_gate.close_admission()
    monkeypatch.setattr(server, "chat_service", service)

    with pytest.raises(HTTPException) as exc_info:
        await _execute(_payload())
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "RUNTIME_SHUTTING_DOWN"
    assert factory.create_count == 0
    assert registry.observability_snapshot()["active_runs"] == 0


# ---------------------------------------------------------------------------
# Test Group C — terminal projection（HTTP 只投影，不重算 terminal）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_succeeded_result_projected_verbatim(monkeypatch):
    run_id = uuid.uuid4().hex
    result = _result(
        RunStatus.SUCCEEDED,
        StopReason.COMPLETED,
        run_id=run_id,
        safe_message="运行已成功完成",
    )
    service, _factory, _registry = _scripted_service(result)
    monkeypatch.setattr(server, "chat_service", service)

    response = await _execute(_payload(run_id=run_id))

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "run_id": run_id,
        "status": "SUCCEEDED",
        "stop_reason": "COMPLETED",
        "error_code": None,
        "safe_message": "运行已成功完成",
    }


@pytest.mark.asyncio
async def test_failed_result_is_http_200_projection(monkeypatch):
    run_id = uuid.uuid4().hex
    result = _result(
        RunStatus.FAILED,
        StopReason.UNHANDLED_ERROR,
        run_id=run_id,
        error_code="RUNTIME_AGENT_FAILURE",
        safe_message="运行未能完成",
    )
    service, _factory, _registry = _scripted_service(result)
    monkeypatch.setattr(server, "chat_service", service)

    response = await _execute(_payload(run_id=run_id))

    # Runtime FAILED 仍是一次成功完成协议调用，HTTP 层不得重算成 500。
    assert response.status_code == 200
    assert json.loads(response.body) == {
        "run_id": run_id,
        "status": "FAILED",
        "stop_reason": "UNHANDLED_ERROR",
        "error_code": "RUNTIME_AGENT_FAILURE",
        "safe_message": "运行未能完成",
    }


@pytest.mark.asyncio
async def test_cancelled_result_projected_without_route_reinterpretation(
    monkeypatch,
):
    run_id = uuid.uuid4().hex
    result = _result(
        RunStatus.CANCELLED,
        StopReason.USER_CANCELLED,
        run_id=run_id,
        error_code="RUN_CANCELLED",
        safe_message="运行已由用户取消",
    )
    service, _factory, _registry = _scripted_service(result)
    monkeypatch.setattr(server, "chat_service", service)

    response = await _execute(_payload(run_id=run_id))

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "run_id": run_id,
        "status": "CANCELLED",
        "stop_reason": "USER_CANCELLED",
        "error_code": "RUN_CANCELLED",
        "safe_message": "运行已由用户取消",
    }


# ---------------------------------------------------------------------------
# Content-free response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_is_content_free_terminal_fact(monkeypatch):
    run_id = uuid.uuid4().hex
    result = _result(
        RunStatus.SUCCEEDED,
        StopReason.COMPLETED,
        run_id=run_id,
        safe_message="运行已成功完成",
    )
    service, _factory, _registry = _scripted_service(result)
    monkeypatch.setattr(server, "chat_service", service)

    response = await _execute(_payload(run_id=run_id))
    body = json.loads(response.body)

    assert set(body.keys()) == {
        "run_id",
        "status",
        "stop_reason",
        "error_code",
        "safe_message",
    }
    for forbidden in (
        "final_answer",
        "output",
        "retrieved_items",
        "documents",
        "tool_result",
        "memory",
        "trace",
        "prompt",
    ):
        assert forbidden not in body


# ---------------------------------------------------------------------------
# Test Group F — /api/chat wire compatibility（低成本 smoke，不复制既有 suite）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_chat_still_text_stream_with_run_id_header(monkeypatch):
    class _ChatServiceSpy:
        def selected_runtime_mode(self) -> ChatRuntimeMode:
            return ChatRuntimeMode.COORDINATED

        async def stream_coordinated_agent_text(self, **kwargs):
            yield "coordinated"

    monkeypatch.setattr(server, "chat_service", _ChatServiceSpy())
    run_id = "49796282cdb643c7b8850942f7b66bd1"
    response = await server.chat_endpoint(
        server.ChatRequest(
            agent_id="core_router",
            query="hello",
            run_id=run_id,
        ),
        _ConnectedRequest(),
    )

    assert response.media_type == "text/plain"
    assert response.headers["X-Run-Id"] == run_id
    assert [chunk async for chunk in response.body_iterator] == ["coordinated"]
