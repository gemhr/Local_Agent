"""Stage5-Phase7-WP2 Tool Approval HTTP transport ASGI integration tests.

覆盖 WP2 Frozen Contract §17 Mandatory Test Matrix：

1. stream high-risk tool：requested control projection 只含 allowlist 字段，
   decision 前 zero TOOL_STARTED；
2. POST approve：200 DTO -> decided(APPROVED) -> 恰好一次 TOOL_STARTED /
   一次执行；
3. POST reject：200 DTO -> REJECTED -> zero TOOL_STARTED / 执行；
4. duplicate approve：第二次 200 + idempotent=true，恰好一次执行；
5. approve 后 reject：409 APPROVAL_DECISION_CONFLICT，原 decision 不变；
6. approval A + 其他 binding digest：409 APPROVAL_BINDING_MISMATCH，零执行；
7. active run unknown approval -> 404；missing run -> 410 APPROVAL_RUN_INACTIVE；
8. disconnect 后 late approve：410（invalidated/inactive），zero 执行；
9. run deadline 后 late approve：410，zero 执行；
10. decided streaming projection（APPROVED/REJECTED 经真实 stream 验证）；
11. malformed path/body/schema -> 422 且 Registry 零调用。

Transport 层面：全部走真实 ``server.app`` ASGI 栈（routing / path validation /
Pydantic validation / status code / JSON）。需要"pending 中并发命令"的场景使用
同 event loop 的最小 ASGI 调用器（真实全双工流，不启动真实 backend、不依赖外部
模型）；纯请求-响应场景另用官方 ``TestClient`` 验证。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server
from core.chat_service import ChatService
from core.runtime import (
    CoordinatedRuntimeFactory,
    RunRegistry,
    RunStatus,
    RuntimeEventType,
)
from core.runtime.cancellation import CancellationReason
from tests._runtime_assembly_fixtures import FakeRouter, make_services
from tests.test_tool_governance import production_registry, production_service

APPROVE_PATH = "/api/runtime/runs/{run_id}/tool-approvals/{approval_id}/approve"
REJECT_PATH = "/api/runtime/runs/{run_id}/tool-approvals/{approval_id}/reject"


def _tool_args(operation_id: str) -> str:
    return json.dumps(
        {
            "operation_id": operation_id,
            "resource_key": "wp2-http-resource",
            "execution_mode": "NON_IDEMPOTENT_SIMULATION",
            "items": [{"item_id": "item-1", "action": "ADD", "quantity": 1}],
            "processing_options": {"processing_delay_ms": 0},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _real_tool_router(tool_args: str):
    """真实 AgentRouter 工具链桩（复用 WP1 router integration 模式）。"""
    from core.agent_router import AgentRouter
    from core.runtime.tool_execution import ToolExecutionService

    registry = production_registry()
    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    router.tool_governance_service = production_service(registry)
    router.tool_execution_service = ToolExecutionService()
    router._build_messages = lambda **_: [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "query"},
    ]
    router._plan_tool_call = lambda _messages, _agent_id: (
        "complex_workflow_simulator",
        tool_args,
    )
    return router, registry


class _ToolChainDriverRouter(FakeRouter):
    """planning 走 DIRECT_ANSWER；single-agent 步骤委托真实 AgentRouter 工具链。

    因此 approval request / decided / TOOL_STARTED 均为真实 Journal-first
    evidence，工具执行是真实 ToolExecutionService 副作用。
    """

    def __init__(self, real_router) -> None:
        super().__init__()
        self._real_router = real_router

    def complete_planning_decision(self, user_request: str, **kwargs) -> str:
        return (
            '{"schema_version": 1, "decision": "DIRECT_ANSWER", '
            '"agent_id": "core_router", "reason_code": "HITL"}'
        )

    def calls_for_agent(self, agent_id: str) -> None:
        if not hasattr(self, "_calls"):
            self._calls = {}
        self._calls[agent_id] = self._calls.get(agent_id, 0) + 1

    def complete_single_agent(self, agent_id: str, query: str, **kwargs) -> str:
        self.calls_for_agent(agent_id)
        self._real_router._prepare_answer_messages(
            agent_id,
            query,
            run_context=kwargs.get("run_context"),
            event_emitter=kwargs.get("event_emitter"),
            approval_controller=kwargs.get("approval_controller"),
        )
        return "tool-answer"


# ---------------------------------------------------------------------------
# Minimal same-loop ASGI transport（真实 FastAPI 栈 + 全双工流）
# ---------------------------------------------------------------------------


def _asgi_scope(method: str, path: str, payload: bytes) -> dict:
    headers = [(b"content-type", b"application/json")]
    if payload:
        headers.append((b"content-length", str(len(payload)).encode("ascii")))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 123),
        "server": ("testserver", 80),
    }


async def asgi_json_request(
    app, method: str, path: str, body: dict | None = None
) -> tuple[int, dict]:
    """同 loop 的一次性 ASGI JSON 请求（真实 routing/validation/JSON）。"""
    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    scope = _asgi_scope(method, path, payload)
    messages: list[dict] = []
    response_complete = asyncio.Event()
    body_sent = not payload

    async def receive() -> dict:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {
                "type": "http.request",
                "body": payload,
                "more_body": False,
            }
        await response_complete.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)
        if (
            message["type"] == "http.response.body"
            and not message.get("more_body", False)
        ):
            response_complete.set()

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body_bytes = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    data = json.loads(body_bytes.decode("utf-8")) if body_bytes else None
    return start["status"], data


class _StreamingASGICall:
    """同 loop 全双工 ASGI 调用：/api/chat chunk 实时入队，可在 run pending 时
    并发发出 approve/reject 命令请求。"""

    def __init__(self, app, method: str, path: str, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self._app = app
        self._scope = _asgi_scope(method, path, payload)
        self._payload = payload
        self._body_sent = False
        self._response_complete = asyncio.Event()
        self._response_started = asyncio.Event()
        self.chunks: asyncio.Queue = asyncio.Queue()
        self.status_code: int | None = None
        self.headers: dict[str, str] = {}
        self.task: asyncio.Task | None = None

    async def _receive(self) -> dict:
        if not self._body_sent:
            self._body_sent = True
            return {
                "type": "http.request",
                "body": self._payload,
                "more_body": False,
            }
        await self._response_complete.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict) -> None:
        if message["type"] == "http.response.start":
            self.status_code = message["status"]
            self.headers = {
                key.decode(): value.decode()
                for key, value in message.get("headers", [])
            }
            self._response_started.set()
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            if chunk:
                await self.chunks.put(chunk)
            if not message.get("more_body", False):
                self._response_complete.set()
                await self.chunks.put(None)

    async def start(self) -> None:
        self.task = asyncio.create_task(
            self._app(self._scope, self._receive, self._send)
        )
        await asyncio.wait_for(self._response_started.wait(), 15)

    async def read_until(self, predicate, timeout: float = 20.0) -> str:
        buffer = ""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate(buffer):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(f"stream 未出现期望事件: {buffer[-2000:]}")
            try:
                chunk = await asyncio.wait_for(self.chunks.get(), remaining)
            except TimeoutError as exc:
                raise AssertionError(f"stream 读取超时: {buffer[-2000:]}") from exc
            if chunk is None:
                raise AssertionError(f"stream 提前结束: {buffer[-2000:]}")
            buffer += chunk.decode("utf-8", errors="replace")
        return buffer

    async def aclose(self) -> None:
        """模拟客户端断开：取消请求 task（与 uvicorn 断连语义一致）。"""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def wait_finished(self, timeout: float = 30.0) -> None:
        assert self.task is not None
        await asyncio.wait_for(self.task, timeout)


def _orch_events(buffer: str) -> list[dict]:
    events: list[dict] = []
    for line in buffer.splitlines():
        if line.startswith("[[ORCH]]"):
            try:
                events.append(json.loads(line[len("[[ORCH]]") :]))
            except json.JSONDecodeError:
                # chunk 边界上的半行；read_until 会持续重试整个 buffer。
                continue
    return events


async def _wait_stream_event(
    call: _StreamingASGICall, event_type: str
) -> tuple[dict, str]:
    def _has(buffer: str) -> bool:
        return any(
            e.get("event_type") == event_type for e in _orch_events(buffer)
        )

    buffer = await call.read_until(_has)
    event = next(
        e for e in _orch_events(buffer) if e.get("event_type") == event_type
    )
    return event, buffer


async def _poll(condition, timeout: float = 15.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = condition()
        if value:
            return value
        await asyncio.sleep(0.02)
    return condition()


# ---------------------------------------------------------------------------
# Pending-run harness（真实 Coordinated Runtime + /api/chat ASGI 流）
# ---------------------------------------------------------------------------


class _PendingRun:
    def __init__(
        self,
        *,
        run_id: str,
        call: _StreamingASGICall,
        run_registry: RunRegistry,
        services,
        tool_registry,
        tool_store,
    ) -> None:
        self.run_id = run_id
        self.call = call
        self.run_registry = run_registry
        self.services = services
        self.tool_registry = tool_registry
        self.tool_store = tool_store

    def journal_records(self):
        return self.services.event_journal.read_after(self.run_id, 0, 1000)

    def journal_types(self):
        return [r.event_type for r in self.journal_records()]

    @property
    def executed_operations(self) -> int:
        return len(self.tool_store.committed_operations)


async def _start_pending_run(
    monkeypatch, *, operation_id: str
) -> tuple[_PendingRun, dict, str]:
    tool_args = _tool_args(operation_id)
    real_router, tool_registry = _real_tool_router(tool_args)
    driver_router = _ToolChainDriverRouter(real_router)
    run_registry = RunRegistry()
    services = make_services(run_registry=run_registry, snapshot_enabled=False)
    factory = CoordinatedRuntimeFactory(driver_router, services)
    service = ChatService(
        driver_router,
        event_journal=services.event_journal,
        observability_dispatcher=services.observability_dispatcher,
        gauge_provider=services.observability_dispatcher.gauge_provider,
        coordinated_runtime_factory=factory,
        run_registry=run_registry,
    )
    monkeypatch.setattr(server, "chat_service", service)
    run_id = uuid.uuid4().hex
    call = _StreamingASGICall(
        server.app,
        "POST",
        "/api/chat",
        {"agent_id": "core_router", "query": "question", "run_id": run_id},
    )
    await call.start()
    assert call.status_code == 200
    requested, buffer = await _wait_stream_event(call, "TOOL_APPROVAL_REQUESTED")
    pending = _PendingRun(
        run_id=run_id,
        call=call,
        run_registry=run_registry,
        services=services,
        tool_registry=tool_registry,
        tool_store=tool_registry.require(
            "complex_workflow_simulator"
        ).adapter._state_store,
    )
    return pending, requested, buffer


async def _shutdown_pending_run(pending: _PendingRun) -> None:
    await pending.call.aclose()
    await _poll(lambda: pending.run_registry.get(pending.run_id) is None)


# ---------------------------------------------------------------------------
# Mandatory 1：stream requested projection / decision 前 zero TOOL_STARTED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_requested_projection_with_zero_tool_started_before_decision(
    monkeypatch,
):
    pending, requested, stream_buffer = await _start_pending_run(
        monkeypatch, operation_id="wp2-stream-1"
    )
    try:
        payload = requested["payload"]
        assert set(payload) == {
            "approval_id",
            "tool_name",
            "invocation_binding_digest",
            "risk_level",
            "risk_facts",
        }
        assert payload["tool_name"] == "complex_workflow_simulator"
        assert payload["risk_level"] == "HIGH"
        assert len(payload["invocation_binding_digest"]) == 64
        # decision 前：zero TOOL_STARTED（client stream 与 journal 双重确认）。
        assert not any(
            e.get("event_type") == "TOOL_STARTED"
            for e in _orch_events(stream_buffer)
        )
        assert not any(
            r.event_type is RuntimeEventType.TOOL_STARTED
            for r in pending.journal_records()
        )
        assert pending.executed_operations == 0
    finally:
        await _shutdown_pending_run(pending)


# ---------------------------------------------------------------------------
# Mandatory 2：POST approve -> 200 -> decided(APPROVED) -> 恰好一次执行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_approve_executes_tool_exactly_once(monkeypatch):
    pending, requested, _ = await _start_pending_run(
        monkeypatch, operation_id="wp2-approve-1"
    )
    try:
        approval_id = requested["payload"]["approval_id"]
        digest = requested["payload"]["invocation_binding_digest"]
        status, body = await asgi_json_request(
            server.app,
            "POST",
            APPROVE_PATH.format(run_id=pending.run_id, approval_id=approval_id),
            {
                "invocation_binding_digest": digest,
                "actor_id": "reviewer-a",
            },
        )
        assert status == 200
        # Public DTO 形状（content-free、与 Runtime internal type 解耦）。
        assert set(body) == {
            "run_id",
            "approval_id",
            "effective_status",
            "idempotent",
            "error_code",
            "decided_at",
        }
        assert body["run_id"] == pending.run_id
        assert body["approval_id"] == approval_id
        assert body["effective_status"] in {"APPROVED", "EXECUTION_CLAIMED"}
        assert body["idempotent"] is False
        assert body["error_code"] is None
        assert body["decided_at"] is not None and "T" in body["decided_at"]
        # 不回显 actor identity / digest 以外的内部数据。
        assert "reviewer-a" not in json.dumps(body)

        decided, _ = await _wait_stream_event(pending.call, "TOOL_APPROVAL_DECIDED")
        assert decided["payload"]["decision_status"] == "APPROVED"
        assert set(decided["payload"]) == {
            "approval_id",
            "invocation_binding_digest",
            "decision_status",
        }
        await pending.call.wait_finished()

        types = pending.journal_types()
        assert types.count(RuntimeEventType.TOOL_STARTED) == 1
        requested_idx = types.index(RuntimeEventType.TOOL_APPROVAL_REQUESTED)
        decided_idx = types.index(RuntimeEventType.TOOL_APPROVAL_DECIDED)
        started_idx = types.index(RuntimeEventType.TOOL_STARTED)
        assert requested_idx < decided_idx < started_idx
        assert pending.executed_operations == 1
    finally:
        await _shutdown_pending_run(pending)


# ---------------------------------------------------------------------------
# Mandatory 3：POST reject -> 200 -> zero TOOL_STARTED / zero 执行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_reject_zero_execution(monkeypatch):
    pending, requested, _ = await _start_pending_run(
        monkeypatch, operation_id="wp2-reject-1"
    )
    try:
        approval_id = requested["payload"]["approval_id"]
        digest = requested["payload"]["invocation_binding_digest"]
        status, body = await asgi_json_request(
            server.app,
            "POST",
            REJECT_PATH.format(run_id=pending.run_id, approval_id=approval_id),
            {"invocation_binding_digest": digest},
        )
        assert status == 200
        assert body["effective_status"] == "REJECTED"
        assert body["error_code"] is None
        assert body["idempotent"] is False

        decided, _ = await _wait_stream_event(pending.call, "TOOL_APPROVAL_DECIDED")
        assert decided["payload"]["decision_status"] == "REJECTED"
        await pending.call.wait_finished()

        assert not any(
            r.event_type is RuntimeEventType.TOOL_STARTED
            for r in pending.journal_records()
        )
        assert pending.executed_operations == 0
    finally:
        await _shutdown_pending_run(pending)


# ---------------------------------------------------------------------------
# Mandatory 4：duplicate approve -> 第二次 200 + idempotent，恰好一次执行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_approve_is_idempotent_200(monkeypatch):
    pending, requested, _ = await _start_pending_run(
        monkeypatch, operation_id="wp2-dup-1"
    )
    try:
        approval_id = requested["payload"]["approval_id"]
        digest = requested["payload"]["invocation_binding_digest"]
        path = APPROVE_PATH.format(run_id=pending.run_id, approval_id=approval_id)
        body = {"invocation_binding_digest": digest, "actor_id": "reviewer-b"}
        first_status, first_body = await asgi_json_request(
            server.app, "POST", path, body
        )
        second_status, second_body = await asgi_json_request(
            server.app, "POST", path, body
        )
        assert first_status == 200 and first_body["idempotent"] is False
        assert second_status == 200
        assert second_body["idempotent"] is True
        assert second_body["error_code"] is None
        # 已 claim 后的重复 approve 只投影既有 claim，绝不新增执行。
        assert second_body["effective_status"] in {
            "APPROVED",
            "EXECUTION_CLAIMED",
        }
        await pending.call.wait_finished()
        assert pending.executed_operations == 1
        assert (
            pending.journal_types().count(RuntimeEventType.TOOL_STARTED) == 1
        )
    finally:
        await _shutdown_pending_run(pending)


# ---------------------------------------------------------------------------
# Mandatory 5：approve 后 reject -> 409 conflict，原 decision 不变
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_then_reject_conflict_409(monkeypatch):
    pending, requested, _ = await _start_pending_run(
        monkeypatch, operation_id="wp2-conflict-1"
    )
    try:
        approval_id = requested["payload"]["approval_id"]
        digest = requested["payload"]["invocation_binding_digest"]
        approve_path = APPROVE_PATH.format(
            run_id=pending.run_id, approval_id=approval_id
        )
        reject_path = REJECT_PATH.format(
            run_id=pending.run_id, approval_id=approval_id
        )
        approve_status, _ = await asgi_json_request(
            server.app, "POST", approve_path, {"invocation_binding_digest": digest}
        )
        assert approve_status == 200
        conflict_status, conflict_body = await asgi_json_request(
            server.app, "POST", reject_path, {"invocation_binding_digest": digest}
        )
        assert conflict_status == 409
        assert conflict_body["error_code"] == "APPROVAL_DECISION_CONFLICT"
        assert conflict_body["effective_status"] in {"APPROVED", "EXECUTION_CLAIMED"}
        await pending.call.wait_finished()
        # 原 decision 不变：恰好一次执行；decided 仅 APPROVED 一个人类决定。
        assert pending.executed_operations == 1
        decided_records = [
            r
            for r in pending.journal_records()
            if r.event_type is RuntimeEventType.TOOL_APPROVAL_DECIDED
        ]
        assert [
            r.safe_payload["decision_status"] for r in decided_records
        ].count("REJECTED") == 0
    finally:
        await _shutdown_pending_run(pending)


# ---------------------------------------------------------------------------
# Mandatory 6：approval A + 其他 binding digest -> 409 mismatch，零执行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_binding_mismatch_409_zero_execution(monkeypatch):
    pending, requested, _ = await _start_pending_run(
        monkeypatch, operation_id="wp2-mismatch-1"
    )
    try:
        approval_id = requested["payload"]["approval_id"]
        wrong_digest = "f" * 64
        assert wrong_digest != requested["payload"]["invocation_binding_digest"]
        status, body = await asgi_json_request(
            server.app,
            "POST",
            APPROVE_PATH.format(run_id=pending.run_id, approval_id=approval_id),
            {"invocation_binding_digest": wrong_digest},
        )
        assert status == 409
        assert body["error_code"] == "APPROVAL_BINDING_MISMATCH"
        assert pending.executed_operations == 0
        assert not any(
            r.event_type is RuntimeEventType.TOOL_STARTED
            for r in pending.journal_records()
        )
        # mismatch 零状态改变：正确 digest 仍可批准并恰好执行一次。
        ok_status, _ = await asgi_json_request(
            server.app,
            "POST",
            APPROVE_PATH.format(run_id=pending.run_id, approval_id=approval_id),
            {
                "invocation_binding_digest": requested["payload"][
                    "invocation_binding_digest"
                ]
            },
        )
        assert ok_status == 200
        await pending.call.wait_finished()
        assert pending.executed_operations == 1
    finally:
        await _shutdown_pending_run(pending)


# ---------------------------------------------------------------------------
# Mandatory 7：active unknown -> 404；missing run -> 410（不合并）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_run_unknown_approval_returns_404(monkeypatch):
    pending, requested, _ = await _start_pending_run(
        monkeypatch, operation_id="wp2-unknown-1"
    )
    try:
        status, body = await asgi_json_request(
            server.app,
            "POST",
            APPROVE_PATH.format(
                run_id=pending.run_id, approval_id=uuid.uuid4().hex
            ),
            {"invocation_binding_digest": "a" * 64},
        )
        assert status == 404
        assert body["error_code"] == "APPROVAL_UNKNOWN"
        assert pending.executed_operations == 0
    finally:
        await _shutdown_pending_run(pending)


@pytest.mark.asyncio
async def test_missing_run_returns_410_approval_run_inactive(monkeypatch):
    pending, requested, _ = await _start_pending_run(
        monkeypatch, operation_id="wp2-inactive-1"
    )
    try:
        unknown_run_id = uuid.uuid4().hex
        status, body = await asgi_json_request(
            server.app,
            "POST",
            REJECT_PATH.format(
                run_id=unknown_run_id,
                approval_id=requested["payload"]["approval_id"],
            ),
            {"invocation_binding_digest": "a" * 64},
        )
        assert status == 410
        assert body["error_code"] == "APPROVAL_RUN_INACTIVE"
        assert pending.executed_operations == 0
    finally:
        await _shutdown_pending_run(pending)


# ---------------------------------------------------------------------------
# Mandatory 8：disconnect 后 late approve -> 410，zero 执行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_then_late_approve_410_zero_execution(monkeypatch):
    pending, requested, _ = await _start_pending_run(
        monkeypatch, operation_id="wp2-disconnect-1"
    )
    approval_id = requested["payload"]["approval_id"]
    digest = requested["payload"]["invocation_binding_digest"]
    # 客户端断开：取消 /api/chat 请求 task -> watcher/cancellation 语义接管。
    await pending.call.aclose()
    await _poll(lambda: pending.run_registry.get(pending.run_id) is None, 15)
    try:
        status, body = await asgi_json_request(
            server.app,
            "POST",
            APPROVE_PATH.format(run_id=pending.run_id, approval_id=approval_id),
            {"invocation_binding_digest": digest},
        )
        # transport race：handle 已注销 -> RUN_INACTIVE；controller 已失效 ->
        # INVALIDATED。二者都必须是 410，且零执行。
        assert status == 410
        assert body["error_code"] in {
            "APPROVAL_RUN_INACTIVE",
            "APPROVAL_INVALIDATED",
        }
        assert body["effective_status"] in {
            "PENDING",
            "INVALIDATED_CANCELLED",
        }
        await _poll(lambda: pending.run_registry.get(pending.run_id) is None, 15)
        assert pending.executed_operations == 0
        assert not any(
            r.event_type is RuntimeEventType.TOOL_STARTED
            for r in pending.journal_records()
        )
    finally:
        await _shutdown_pending_run(pending)


# ---------------------------------------------------------------------------
# Mandatory 9：run deadline 后 late approve -> 410，zero 执行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_deadline_then_late_approve_410_zero_execution(monkeypatch):
    real_router, tool_registry = _real_tool_router(_tool_args("wp2-timeout-1"))
    driver_router = _ToolChainDriverRouter(real_router)
    run_registry = RunRegistry()
    services = make_services(run_registry=run_registry, snapshot_enabled=False)
    factory = CoordinatedRuntimeFactory(driver_router, services)
    service = ChatService(
        driver_router,
        event_journal=services.event_journal,
        observability_dispatcher=services.observability_dispatcher,
        gauge_provider=services.observability_dispatcher.gauge_provider,
        coordinated_runtime_factory=factory,
        run_registry=run_registry,
    )
    monkeypatch.setattr(server, "chat_service", service)
    scope = await factory.create_run_scope(
        "core_router", "question", timeout_seconds=0.5
    )
    run_id = scope.run_id
    execute_task = asyncio.create_task(scope.execute())
    try:
        controller = await _poll(
            lambda: scope.coordinator.tool_approval_controller or None
        )
        assert controller is not None

        def _pending_state():
            requested = [
                r
                for r in services.event_journal.read_after(run_id, 0, 1000)
                if r.event_type is RuntimeEventType.TOOL_APPROVAL_REQUESTED
            ]
            if requested and controller.pending_count() >= 1:
                return requested[0]
            return None

        requested_record = await _poll(_pending_state)
        assert requested_record is not None
        approval_id = requested_record.safe_payload["approval_id"]
        approval_request = controller.get(approval_id)
        assert approval_request is not None
        digest = approval_request.invocation_binding_digest
        # 等 Run deadline 收口（approval wait 消耗 wall-clock deadline）。
        run_result = await asyncio.wait_for(execute_task, 30)
        assert run_result.status in {RunStatus.CANCELLED, RunStatus.FAILED}
        status, body = await asgi_json_request(
            server.app,
            "POST",
            APPROVE_PATH.format(run_id=run_id, approval_id=approval_id),
            {"invocation_binding_digest": digest},
        )
        assert status == 410
        assert body["error_code"] in {
            "APPROVAL_RUN_INACTIVE",
            "APPROVAL_INVALIDATED",
        }
        assert len(tool_registry.require(
            "complex_workflow_simulator"
        ).adapter._state_store.committed_operations) == 0
        assert not any(
            r.event_type is RuntimeEventType.TOOL_STARTED
            for r in services.event_journal.read_after(run_id, 0, 1000)
        )
    finally:
        if not execute_task.done():
            scope.request_cancel(CancellationReason.REQUEST_CANCELLED)
            await asyncio.gather(execute_task, return_exceptions=True)
        await scope.close()


# ---------------------------------------------------------------------------
# Mandatory 11：validation matrix -> 422 且 Registry 零调用（官方 TestClient）
# ---------------------------------------------------------------------------


class _CountingRunRegistry(RunRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.decide_calls = 0

    async def decide_tool_approval(self, *args, **kwargs):
        self.decide_calls += 1
        return await super().decide_tool_approval(*args, **kwargs)


@pytest.mark.parametrize(
    ("run_id", "approval_id", "body"),
    [
        # invalid run UUID
        ("not-a-uuid", str(uuid.uuid4()), {"invocation_binding_digest": "a" * 64}),
        # invalid approval UUID
        (str(uuid.uuid4()), "nope", {"invocation_binding_digest": "a" * 64}),
        # digest too short
        (str(uuid.uuid4()), str(uuid.uuid4()), {"invocation_binding_digest": "a" * 63}),
        # digest uppercase
        (str(uuid.uuid4()), str(uuid.uuid4()), {"invocation_binding_digest": "A" * 64}),
        # digest non-hex
        (str(uuid.uuid4()), str(uuid.uuid4()), {"invocation_binding_digest": "g" * 64}),
        # empty actor_id
        (
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            {"invocation_binding_digest": "a" * 64, "actor_id": ""},
        ),
        # actor too long
        (
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            {"invocation_binding_digest": "a" * 64, "actor_id": "x" * 129},
        ),
        # extra body field
        (
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            {
                "invocation_binding_digest": "a" * 64,
                "decision": "APPROVE",
            },
        ),
        # missing digest
        (str(uuid.uuid4()), str(uuid.uuid4()), {"actor_id": "reviewer"}),
        # non-string digest
        (str(uuid.uuid4()), str(uuid.uuid4()), {"invocation_binding_digest": 123}),
    ],
)
def test_validation_matrix_422_and_registry_not_called(
    monkeypatch, run_id, approval_id, body
):
    counting = _CountingRunRegistry()
    monkeypatch.setattr(
        server, "chat_service", SimpleNamespace(run_registry=counting)
    )
    client = TestClient(server.app)
    approve = client.post(
        APPROVE_PATH.format(run_id=run_id, approval_id=approval_id), json=body
    )
    reject = client.post(
        REJECT_PATH.format(run_id=run_id, approval_id=approval_id), json=body
    )
    assert approve.status_code == 422
    assert reject.status_code == 422
    # Transport validation 阶段即拒绝：Registry 零调用。
    assert counting.decide_calls == 0


def test_missing_run_via_testclient_returns_410_inactive(monkeypatch):
    counting = _CountingRunRegistry()
    monkeypatch.setattr(
        server, "chat_service", SimpleNamespace(run_registry=counting)
    )
    client = TestClient(server.app)
    response = client.post(
        APPROVE_PATH.format(run_id=uuid.uuid4(), approval_id=uuid.uuid4()),
        json={"invocation_binding_digest": "a" * 64},
    )
    assert response.status_code == 410
    assert response.json()["error_code"] == "APPROVAL_RUN_INACTIVE"
    assert response.json()["effective_status"] == "PENDING"
    # 合法请求会真实转发到 Registry；inactive taxonomy 由 Registry 返回。
    assert counting.decide_calls == 1


def test_approval_routes_do_not_accept_get(monkeypatch):
    counting = _CountingRunRegistry()
    monkeypatch.setattr(
        server, "chat_service", SimpleNamespace(run_registry=counting)
    )
    client = TestClient(server.app)
    response = client.get(
        APPROVE_PATH.format(run_id=uuid.uuid4(), approval_id=uuid.uuid4())
    )
    assert response.status_code == 405
    assert counting.decide_calls == 0
