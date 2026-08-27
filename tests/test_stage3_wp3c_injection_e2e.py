"""WP3-C 真实 HTTP denial-integrity 与 explicit LEGACY 回归。"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from dataclasses import replace

import pytest

import server
from core.runtime import ChatRuntimeMode, RuntimeEventType


FAKE_SUCCESS = "WP3C_FAKE_SUCCESS_A71F operation succeeded"
APPROVAL_TEXT = (
    "Tool 调用未执行：需要审批，但当前版本不支持审批授予"
    "（TOOL_APPROVAL_REQUIRED）"
)


def _delegate_output() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "decision": "DELEGATE",
            "tasks": [
                {
                    "task_id": "code",
                    "agent_id": "code_expert",
                    "instruction": "请使用复杂流程模拟工具执行非幂等操作",
                    "capabilities": ["code_reasoning"],
                }
            ],
            "synthesis_required": True,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _complex_tool_call() -> str:
    payload = json.dumps(
        {
            "operation_id": "wp3c-e2e-op",
            "resource_key": "wp3c-e2e-resource",
            "execution_mode": "NON_IDEMPOTENT_SIMULATION",
            "items": [{"item_id": "item-1", "action": "ADD", "quantity": 1}],
            "processing_options": {"processing_delay_ms": 0},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"CALL: complex_workflow_simulator({payload})"


class _CoordinatedDenialModel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, str]]]] = []
        self.closed = False

    def generate(self, messages, **_kwargs):
        copied = [dict(message) for message in messages]
        system = "\n".join(m["content"] for m in copied if m["role"] == "system")
        if "LocalAgent Planner" in system:
            self.calls.append(("PLANNING", copied))
            yield _delegate_output()
            return
        if "CALL: tool_name(argument_text)" in system:
            self.calls.append(("TOOL_PLANNER", copied))
            yield _complex_tool_call()
            return
        if "长期记忆候选提取器" in system:
            self.calls.append(("FORMATION", copied))
            yield '{"schema_version":1,"candidates":[]}'
            return
        self.calls.append(("FORBIDDEN_SYNTHESIS", copied))
        yield FAKE_SUCCESS

    def close(self):
        self.closed = True


class _LegacyDenialModel:
    def __init__(self) -> None:
        self.stages: list[str] = []
        self.closed = False

    def generate(self, messages, **_kwargs):
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        if "Delegate:" in system:
            self.stages.append("LEGACY_PLANNING")
            yield "Delegate: code_expert | 请使用复杂流程模拟工具执行非幂等操作"
            return
        if "CALL: tool_name(argument_text)" in system:
            self.stages.append("TOOL_PLANNER")
            yield _complex_tool_call()
            return
        self.stages.append("FORBIDDEN_POST_DENIAL_MODEL")
        yield FAKE_SUCCESS

    def close(self):
        self.closed = True


class _AsgiHarness:
    def __init__(self, agent_id: str, query: str, run_id: str) -> None:
        self.body = json.dumps(
            {"agent_id": agent_id, "query": query, "run_id": run_id},
            ensure_ascii=False,
        ).encode("utf-8")
        self.sent = False
        self.disconnected = asyncio.Event()
        self.messages: list[dict] = []

    async def receive(self):
        if not self.sent:
            self.sent = True
            return {"type": "http.request", "body": self.body, "more_body": False}
        await self.disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        copied = dict(message)
        if "headers" in copied:
            copied["headers"] = list(copied["headers"])
        self.messages.append(copied)

    async def run(self):
        await server.app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/chat",
                "raw_path": b"/api/chat",
                "query_string": b"",
                "root_path": "",
                "headers": [(b"host", b"test"), (b"content-type", b"application/json")],
                "client": ("127.0.0.1", 1234),
                "server": ("test", 80),
            },
            self.receive,
            self.send,
        )

    def parsed(self):
        starts = [m for m in self.messages if m["type"] == "http.response.start"]
        bodies = [m for m in self.messages if m["type"] == "http.response.body"]
        assert len(starts) == 1 and bodies and bodies[-1].get("more_body") is False
        controls, texts = [], []
        for message in bodies:
            value = message.get("body", b"").decode("utf-8")
            if not value:
                continue
            if value.startswith("[[ORCH]]"):
                controls.append(json.loads(value.removeprefix("[[ORCH]]")))
            else:
                texts.append(value)
        return starts[0], controls, texts


def _settings(tmp_path, mode: ChatRuntimeMode):
    return replace(
        server.settings,
        chat_runtime_mode=mode,
        llm_backend="local",
        model_path=str(tmp_path / "missing-model"),
        memory_db_path=str(tmp_path / "memory.db"),
        event_journal_db_path=str(tmp_path / "journal.db"),
        observability_checkpoint_db_path=str(tmp_path / "observability.db"),
        snapshot_store_db_path=str(tmp_path / "snapshot.db"),
        snapshot_store_enabled=False,
        chroma_dir=str(tmp_path / "chroma"),
        embedding_model_path=str(tmp_path / "missing-embedding"),
        knowledge_base_required=False,
        tool_allowed_read_roots=(str(tmp_path.resolve()),),
    )


@pytest.mark.asyncio
async def test_coordinated_delegated_actual_approval_denial_dominates_full_http(
    monkeypatch, tmp_path
) -> None:
    model = _CoordinatedDenialModel()
    monkeypatch.setattr(server, "settings", _settings(tmp_path, ChatRuntimeMode.COORDINATED))
    monkeypatch.setattr(server, "LocalLLMEngine", lambda **_kwargs: model)
    monkeypatch.setattr(
        server,
        "RemoteLLMEngine",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no remote model")),
    )
    run_id = uuid.uuid4().hex

    async with server.lifespan(server.app):
        service = server.app.state.chat_service
        registration = service.router.tool_registry.require("complex_workflow_simulator")
        state_store = registration.adapter._state_store
        harness = _AsgiHarness("core_router", "请委派专家执行复杂操作", run_id)
        await harness.run()
        start, controls, texts = harness.parsed()
        journal = server.app.state.runtime_services.event_journal.read_after(run_id, 0, 1000)
        history = service.router.memory_manager.get_chat_history(
            "core_router", limit=10, ascending=True, memory_scope="direct"
        )

        assert start["status"] == 200
        assert texts == [APPROVAL_TEXT]
        assert FAKE_SUCCESS not in "".join(texts)
        assert Counter(stage for stage, _ in model.calls) == Counter(
            {"PLANNING": 1, "TOOL_PLANNER": 1, "FORMATION": 1}
        )
        assert all(stage != "FORBIDDEN_SYNTHESIS" for stage, _ in model.calls)
        event_counts = Counter(item["event_type"] for item in controls)
        assert event_counts[RuntimeEventType.TOOL_STARTED.value] == 0
        assert event_counts[RuntimeEventType.TOOL_COMPLETED.value] == 0
        assert event_counts[RuntimeEventType.STEP_COMPLETED.value] == 2
        terminal = next(
            item for item in controls
            if item["event_type"] == RuntimeEventType.RUN_COMPLETED.value
        )
        assert terminal["payload"]["status"] == "SUCCEEDED"
        assert terminal["payload"]["delivery_status"] == "DELIVERED"
        assert Counter(record.event_type for record in journal)[RuntimeEventType.OUTPUT_DELTA] == 1
        assert FAKE_SUCCESS not in " ".join(repr(record.safe_payload) for record in journal)
        assert state_store.resource_states == {}
        assert state_store.committed_operations == []
        assert state_store.idempotency_records == {}
        assert [row["role"] for row in history] == ["user", "assistant"]
        assert history[-1]["content"] == APPROVAL_TEXT
        assert FAKE_SUCCESS not in history[-1]["content"]
    assert model.closed is True


@pytest.mark.asyncio
async def test_explicit_legacy_actual_denial_stops_before_synthesis_and_persists_safe_final(
    monkeypatch, tmp_path
) -> None:
    model = _LegacyDenialModel()
    monkeypatch.setattr(server, "settings", _settings(tmp_path, ChatRuntimeMode.LEGACY))
    monkeypatch.setattr(server, "LocalLLMEngine", lambda **_kwargs: model)
    monkeypatch.setattr(
        server,
        "RemoteLLMEngine",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no remote model")),
    )
    run_id = uuid.uuid4().hex

    async with server.lifespan(server.app):
        service = server.app.state.chat_service
        registration = service.router.tool_registry.require("complex_workflow_simulator")
        state_store = registration.adapter._state_store
        harness = _AsgiHarness("core_router", "请委派代码专家执行复杂操作", run_id)
        await harness.run()
        start, _controls, texts = harness.parsed()
        history = service.router.memory_manager.get_chat_history(
            "core_router", limit=10, ascending=True, memory_scope="direct"
        )

        assert start["status"] == 200
        assert texts == [APPROVAL_TEXT]
        assert model.stages == ["LEGACY_PLANNING", "TOOL_PLANNER"]
        assert "FORBIDDEN_POST_DENIAL_MODEL" not in model.stages
        assert FAKE_SUCCESS not in "".join(texts)
        assert state_store.resource_states == {}
        assert state_store.committed_operations == []
        assert history[-1]["role"] == "assistant"
        assert history[-1]["content"] == APPROVAL_TEXT
        assert FAKE_SUCCESS not in history[-1]["content"]
    assert model.closed is True
