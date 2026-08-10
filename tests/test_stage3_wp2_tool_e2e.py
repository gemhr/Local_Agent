from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

import server
from core.runtime import ChatRuntimeMode, RuntimeEventType
from core.runtime.tool_governance import (
    ToolGovernanceContext,
    ToolGovernanceOutcome,
    ToolRiskLevel,
)


_PLANNING_OUTPUT = (
    '{"schema_version":1,"decision":"DIRECT_ANSWER",'
    '"agent_id":"core_router","reason_code":"WP2_C_TOOL_E2E"}'
)
_MARKER = "marker_file_7f3a.txt"
_FINAL_TEXT = f"找到 {_MARKER}"
_APPROVAL_TEXT = (
    "Tool 调用未执行：需要审批，但当前版本不支持审批授予"
    "（TOOL_APPROVAL_REQUIRED）"
)


class _ScenarioModel:
    """仅替代外部模型输出，并按真实 prompt 语义 fail closed。"""

    def __init__(self, *, tool_call: str, allow_final_answer: bool) -> None:
        self.tool_call = tool_call
        self.allow_final_answer = allow_final_answer
        self.semantic_stages: list[str] = []
        self.messages_by_stage: dict[str, list[list[dict[str, str]]]] = {}
        self.closed = False

    def _record(self, stage: str, messages: list[dict[str, str]]) -> None:
        self.semantic_stages.append(stage)
        self.messages_by_stage.setdefault(stage, []).append(
            [dict(message) for message in messages]
        )

    def generate(self, messages, **kwargs):
        system_messages = [
            message["content"]
            for message in messages
            if message.get("role") == "system"
        ]
        system = "\n".join(system_messages)
        if "LocalAgent Planner" in system:
            self._record("PLANNING", messages)
            yield _PLANNING_OUTPUT
            return
        if all(
            marker in system
            for marker in ("只能输出一行", "CALL: tool_name(argument_text)", "可用工具")
        ):
            self._record("TOOL_PLANNER", messages)
            yield self.tool_call
            return
        if "已使用工具：" in system and "工具观察结果：" in system:
            self._record("FINAL_ANSWER", messages)
            if not self.allow_final_answer:
                raise AssertionError("governance denial must not invoke final-answer model")
            if "已使用工具：list_files" not in system or _MARKER not in system:
                raise AssertionError(
                    "final-answer model did not receive real list_files observation"
                )
            yield _FINAL_TEXT
            return
        raise AssertionError(f"unknown model message shape: {system[:160]!r}")

    def close(self) -> None:
        self.closed = True


class _AsgiHarness:
    """通过真实 FastAPI ASGI callable 发送并完整消费一个请求。"""

    def __init__(self, *, query: str, run_id: str) -> None:
        self._request_body = json.dumps(
            {"agent_id": "core_router", "query": query, "run_id": run_id},
            ensure_ascii=False,
        ).encode("utf-8")
        self._request_sent = False
        self._disconnected = asyncio.Event()
        self.messages: list[dict] = []

    async def receive(self) -> dict:
        if not self._request_sent:
            self._request_sent = True
            return {
                "type": "http.request",
                "body": self._request_body,
                "more_body": False,
            }
        await self._disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: dict) -> None:
        copied = dict(message)
        if "headers" in copied:
            copied["headers"] = list(copied["headers"])
        self.messages.append(copied)

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

    def parsed_response(self) -> tuple[dict, list[bytes], list[dict], list[str]]:
        starts = [item for item in self.messages if item["type"] == "http.response.start"]
        assert len(starts) == 1
        bodies = [item for item in self.messages if item["type"] == "http.response.body"]
        assert bodies
        assert bodies[-1].get("more_body") is False
        assert all(item.get("more_body") is True for item in bodies[:-1])

        raw_chunks = [item.get("body", b"") for item in bodies]
        controls: list[dict] = []
        texts: list[str] = []
        for body in raw_chunks:
            if not body:
                continue
            decoded = body.decode("utf-8")
            if decoded.startswith("[[ORCH]]"):
                controls.append(json.loads(decoded.removeprefix("[[ORCH]]")))
            else:
                texts.append(decoded)
        return starts[0], raw_chunks, controls, texts


def _isolated_settings(tmp_path: Path):
    return replace(
        server.settings,
        chat_runtime_mode=ChatRuntimeMode.COORDINATED,
        llm_backend="local",
        model_path=str(tmp_path / "missing-local-model"),
        memory_db_path=str(tmp_path / "memory.db"),
        event_journal_db_path=str(tmp_path / "journal.db"),
        observability_checkpoint_db_path=str(tmp_path / "observability.db"),
        snapshot_store_db_path=str(tmp_path / "snapshot.db"),
        snapshot_store_enabled=False,
        chroma_dir=str(tmp_path / "chroma"),
        embedding_model_path=str(tmp_path / "missing-embedding-model"),
        knowledge_base_required=False,
    )


def _fail_remote_model(*args, **kwargs):
    raise AssertionError("remote model constructor must not be called")


def _event_counts(items) -> Counter:
    return Counter(item["event_type"] for item in items)


def _events_of(items, event_type: str) -> list[dict]:
    return [item for item in items if item["event_type"] == event_type]


def _assert_http_response(start: dict, run_id: str) -> None:
    assert start["status"] == 200
    headers = {name.lower(): value for name, value in start["headers"]}
    assert headers[b"content-type"].startswith(b"text/plain")
    assert headers[b"x-run-id"] == run_id.encode("ascii")


def _assert_common_terminal(control_events: list[dict]) -> None:
    counts = _event_counts(control_events)
    assert counts[RuntimeEventType.RUN_COMPLETED.value] == 1
    terminal = _events_of(control_events, RuntimeEventType.RUN_COMPLETED.value)[0]
    assert terminal["payload"]["status"] == "SUCCEEDED"
    assert terminal["payload"]["delivery_status"] == "DELIVERED"
    assert terminal["payload"].get("safe_error_code") is None
    for error_type in (
        RuntimeEventType.ERROR,
        RuntimeEventType.CANCELLATION,
        RuntimeEventType.TIMEOUT,
        RuntimeEventType.BUDGET_EXHAUSTED,
    ):
        assert counts[error_type.value] == 0


@pytest.mark.asyncio
async def test_default_coordinated_list_files_full_e2e(monkeypatch, tmp_path):
    monkeypatch.delenv("CHAT_RUNTIME_MODE", raising=False)
    fixture = tmp_path / "tool-fixture"
    fixture.mkdir()
    (fixture / _MARKER).write_text("wp2-c marker", encoding="utf-8")
    query = f"请使用文件工具查看我给出的目录内容：{fixture.resolve()}"
    model = _ScenarioModel(
        tool_call=f"CALL: list_files({fixture.resolve()})",
        allow_final_answer=True,
    )
    monkeypatch.setattr(server, "settings", _isolated_settings(tmp_path))
    monkeypatch.setattr(server, "LocalLLMEngine", lambda **kwargs: model)
    monkeypatch.setattr(server, "RemoteLLMEngine", _fail_remote_model)
    run_id = uuid.uuid4().hex

    async with server.lifespan(server.app):
        service = server.app.state.chat_service
        services = server.app.state.runtime_services
        assert service.selected_runtime_mode() is ChatRuntimeMode.COORDINATED

        registration = service.router.tool_registry.require("list_files")
        context = ToolGovernanceContext("core_router", run_id, "answer")
        static_decision = service.router.tool_governance_service.authorize_tool(
            context, registration
        )
        invocation = registration.adapter.build_invocation(str(fixture.resolve()))
        spec = registration.adapter.spec_for(invocation)
        invocation_decision = (
            service.router.tool_governance_service.evaluate_invocation(
                context, registration, invocation, spec
            )
        )
        assert static_decision.outcome is ToolGovernanceOutcome.ALLOW
        assert invocation_decision.outcome is ToolGovernanceOutcome.ALLOW
        assert invocation_decision.risk_level is ToolRiskLevel.MEDIUM

        harness = _AsgiHarness(query=query, run_id=run_id)
        await harness.run()
        start, _raw_chunks, controls, texts = harness.parsed_response()
        journal = services.event_journal.read_after(run_id, 0, 1000)
        history = service.router.memory_manager.get_chat_history(
            "core_router", limit=10, ascending=True, memory_scope="direct"
        )

        _assert_http_response(start, run_id)
        assert texts == [_FINAL_TEXT]
        assert all("[[ORCH]]" not in text for text in texts)
        assert all("CALL:" not in text and "可用工具" not in text for text in texts)
        assert all("TOOL_STARTED" not in text and "TOOL_COMPLETED" not in text for text in texts)

        expected_counts = {
            RuntimeEventType.RUN_STARTED.value: 1,
            RuntimeEventType.PLANNING_STARTED.value: 1,
            RuntimeEventType.PLAN_CREATED.value: 1,
            RuntimeEventType.STEP_STARTED.value: 1,
            RuntimeEventType.TOOL_STARTED.value: 1,
            RuntimeEventType.TOOL_COMPLETED.value: 1,
            RuntimeEventType.STEP_COMPLETED.value: 1,
            RuntimeEventType.RUN_COMPLETED.value: 1,
        }
        counts = _event_counts(controls)
        for event_type, count in expected_counts.items():
            assert counts[event_type] == count
        _assert_common_terminal(controls)

        plan = _events_of(controls, RuntimeEventType.PLAN_CREATED.value)[0]
        assert plan["payload"]["planning_source"] == "MODEL"
        assert plan["payload"]["step_count"] == 1
        step_started = _events_of(controls, RuntimeEventType.STEP_STARTED.value)[0]
        assert step_started["payload"]["agent_id"] == "core_router"
        tool_started = _events_of(controls, RuntimeEventType.TOOL_STARTED.value)[0]
        tool_completed = _events_of(controls, RuntimeEventType.TOOL_COMPLETED.value)[0]
        assert tool_started["payload"]["tool_name"] == "list_files"
        assert tool_completed["payload"]["tool_name"] == "list_files"
        assert tool_completed["payload"]["succeeded"] is True
        step_completed = _events_of(controls, RuntimeEventType.STEP_COMPLETED.value)[0]
        assert step_completed["payload"]["status"] == "SUCCEEDED"
        assert step_completed["payload"]["delivery_status"] == "DELIVERED"

        order = [record.event_type.value for record in journal]
        assert order.index("RUN_STARTED") < order.index("PLANNING_STARTED") < order.index("PLAN_CREATED") < order.index("STEP_STARTED")
        assert order.index("STEP_STARTED") < order.index("TOOL_STARTED") < order.index("TOOL_COMPLETED")
        assert order.index("TOOL_COMPLETED") < order.index("OUTPUT_DELTA") < order.index("STEP_COMPLETED") < order.index("RUN_COMPLETED")

        journal_counts = Counter(record.event_type for record in journal)
        assert journal_counts[RuntimeEventType.TOOL_STARTED] == 1
        assert journal_counts[RuntimeEventType.TOOL_COMPLETED] == 1
        assert journal_counts[RuntimeEventType.OUTPUT_DELTA] == 1
        journal_tool_completed = next(
            record for record in journal
            if record.event_type is RuntimeEventType.TOOL_COMPLETED
        )
        assert journal_tool_completed.safe_payload["tool_name"] == "list_files"
        assert journal_tool_completed.safe_payload["succeeded"] is True
        terminal_record = next(
            record for record in journal
            if record.event_type is RuntimeEventType.RUN_COMPLETED
        )
        assert terminal_record.safe_payload["memory_commit_status"] == "SUCCEEDED"

        assert len(history) == 2
        assert [row["role"] for row in history] == ["user", "assistant"]
        assert _MARKER in history[1]["content"]
        assert "工具观察结果：" not in history[1]["content"]

    assert model.closed is True
    assert Counter(model.semantic_stages) == Counter(
        {"PLANNING": 1, "TOOL_PLANNER": 1, "FINAL_ANSWER": 1}
    )
    final_system = "\n".join(
        message["content"]
        for message in model.messages_by_stage["FINAL_ANSWER"][0]
        if message.get("role") == "system"
    )
    assert "已使用工具：list_files" in final_system
    assert "工具观察结果：" in final_system
    assert _MARKER in final_system


@pytest.mark.asyncio
async def test_default_coordinated_approval_required_full_e2e(monkeypatch, tmp_path):
    monkeypatch.delenv("CHAT_RUNTIME_MODE", raising=False)
    tool_args = json.dumps(
        {
            "operation_id": "wp2c-op-1",
            "resource_key": "wp2c-resource-1",
            "execution_mode": "NON_IDEMPOTENT_SIMULATION",
            "items": [{"item_id": "item-1", "action": "ADD", "quantity": 1}],
            "processing_options": {"processing_delay_ms": 0},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    model = _ScenarioModel(
        tool_call=f"CALL: complex_workflow_simulator({tool_args})",
        allow_final_answer=False,
    )
    monkeypatch.setattr(server, "settings", _isolated_settings(tmp_path))
    monkeypatch.setattr(server, "LocalLLMEngine", lambda **kwargs: model)
    monkeypatch.setattr(server, "RemoteLLMEngine", _fail_remote_model)
    run_id = uuid.uuid4().hex

    async with server.lifespan(server.app):
        service = server.app.state.chat_service
        services = server.app.state.runtime_services
        assert service.selected_runtime_mode() is ChatRuntimeMode.COORDINATED

        registration = service.router.tool_registry.require(
            "complex_workflow_simulator"
        )
        store = registration.adapter._state_store
        context = ToolGovernanceContext("core_router", run_id, "answer")
        static_decision = service.router.tool_governance_service.authorize_tool(
            context, registration
        )
        invocation = registration.adapter.build_invocation(tool_args)
        spec = registration.adapter.spec_for(invocation)
        invocation_decision = (
            service.router.tool_governance_service.evaluate_invocation(
                context, registration, invocation, spec
            )
        )
        assert static_decision.outcome is ToolGovernanceOutcome.ALLOW
        assert invocation_decision.risk_level is ToolRiskLevel.HIGH
        assert invocation_decision.outcome is ToolGovernanceOutcome.APPROVAL_REQUIRED
        assert store.resource_states == {}
        assert store.committed_operations == []
        assert store.idempotency_records == {}

        query = "请使用复杂流程模拟工具执行一次非幂等本地状态变更模拟"
        harness = _AsgiHarness(query=query, run_id=run_id)
        await harness.run()
        start, _raw_chunks, controls, texts = harness.parsed_response()
        journal = services.event_journal.read_after(run_id, 0, 1000)

        _assert_http_response(start, run_id)
        assert texts == [_APPROVAL_TEXT]
        _assert_common_terminal(controls)
        counts = _event_counts(controls)
        assert counts[RuntimeEventType.RUN_STARTED.value] == 1
        assert counts[RuntimeEventType.PLANNING_STARTED.value] == 1
        assert counts[RuntimeEventType.PLAN_CREATED.value] == 1
        assert counts[RuntimeEventType.STEP_STARTED.value] == 1
        assert counts[RuntimeEventType.TOOL_STARTED.value] == 0
        assert counts[RuntimeEventType.TOOL_COMPLETED.value] == 0
        assert counts[RuntimeEventType.STEP_COMPLETED.value] == 1
        plan = _events_of(controls, RuntimeEventType.PLAN_CREATED.value)[0]
        assert plan["payload"]["planning_source"] == "MODEL"
        assert plan["payload"]["step_count"] == 1
        step = _events_of(controls, RuntimeEventType.STEP_COMPLETED.value)[0]
        assert step["payload"]["status"] == "SUCCEEDED"
        assert step["payload"]["delivery_status"] == "DELIVERED"

        journal_counts = Counter(record.event_type for record in journal)
        assert journal_counts[RuntimeEventType.TOOL_STARTED] == 0
        assert journal_counts[RuntimeEventType.TOOL_COMPLETED] == 0
        assert journal_counts[RuntimeEventType.OUTPUT_DELTA] == 1
        assert journal_counts[RuntimeEventType.RUN_COMPLETED] == 1
        assert store.resource_states == {}
        assert store.committed_operations == []
        assert store.idempotency_records == {}

    assert model.closed is True
    assert Counter(model.semantic_stages) == Counter(
        {"PLANNING": 1, "TOOL_PLANNER": 1}
    )
    assert "FINAL_ANSWER" not in model.messages_by_stage
