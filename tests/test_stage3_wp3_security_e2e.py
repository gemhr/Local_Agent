from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

import server
from core.runtime import (
    ChatRuntimeMode,
    RESOURCE_DENIAL_MESSAGE,
    ResourceAuthorizationOutcome,
    RuntimeEventType,
)
from core.runtime.tool_governance import ToolGovernanceContext, ToolGovernanceOutcome


_PLANNING_OUTPUT = (
    '{"schema_version":1,"decision":"DIRECT_ANSWER",'
    '"agent_id":"core_router","reason_code":"WP3_SECURITY_E2E"}'
)
_MARKER = "marker_file_wp3.txt"


class _SecurityModel:
    def __init__(self, tool_call: str, *, authorized: bool) -> None:
        self.tool_call = tool_call
        self.authorized = authorized
        self.stages: list[str] = []
        self.closed = False
        self.non_planning_calls = 0

    def generate(self, messages, **kwargs):
        system = "\n".join(
            item["content"] for item in messages if item.get("role") == "system"
        )
        user_messages = [
            item["content"] for item in messages if item.get("role") == "user"
        ]
        if "LocalAgent Planner" in system:
            self.stages.append("PLANNING")
            yield _PLANNING_OUTPUT
            return
        if self.non_planning_calls == 0:
            self.non_planning_calls += 1
            self.stages.append("TOOL_PLANNER")
            yield self.tool_call
            return
        self.non_planning_calls += 1
        tool_messages = [
            content
            for content in user_messages
            if _MARKER in content
            and "工具观察结果：" in content
            and "[来源: list_files]" in content
        ]
        if "请依据随后提供的工具观察结果直接回答用户" in system:
            self.stages.append("FINAL_ANSWER")
            if (
                not self.authorized
                or "不可信外部数据" not in system
                or _MARKER in system
                or len(tool_messages) != 1
            ):
                raise AssertionError("final model received unauthorized or unreal observation")
            yield f"找到 {_MARKER}"
            return
        raise AssertionError(f"unexpected model stage: {system[:300]!r}")

    def close(self) -> None:
        self.closed = True


class _AsgiHarness:
    def __init__(self, query: str, run_id: str) -> None:
        self.body = json.dumps(
            {"agent_id": "core_router", "query": query, "run_id": run_id},
            ensure_ascii=False,
        ).encode("utf-8")
        self.sent = False
        self.wait = asyncio.Event()
        self.messages: list[dict] = []

    async def receive(self):
        if not self.sent:
            self.sent = True
            return {"type": "http.request", "body": self.body, "more_body": False}
        await self.wait.wait()
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
        starts = [item for item in self.messages if item["type"] == "http.response.start"]
        bodies = [item for item in self.messages if item["type"] == "http.response.body"]
        assert len(starts) == 1 and bodies and bodies[-1].get("more_body") is False
        controls, texts = [], []
        for item in bodies:
            value = item.get("body", b"").decode("utf-8")
            if not value:
                continue
            if value.startswith("[[ORCH]]"):
                controls.append(json.loads(value.removeprefix("[[ORCH]]")))
            else:
                texts.append(value)
        return starts[0], controls, texts


def _settings(tmp_path: Path):
    return replace(
        server.settings,
        chat_runtime_mode=ChatRuntimeMode.COORDINATED,
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
        tool_allowed_read_roots=(str((tmp_path / "allowed-root").resolve()),),
    )


def _event_counts(events):
    return Counter(item["event_type"] for item in events)


def _assert_terminal(events):
    terminal = next(
        item for item in events if item["event_type"] == RuntimeEventType.RUN_COMPLETED.value
    )
    assert terminal["payload"]["status"] == "SUCCEEDED"
    assert terminal["payload"]["delivery_status"] == "DELIVERED"


@pytest.mark.asyncio
async def test_authorized_list_files_full_http_chain(monkeypatch, tmp_path: Path) -> None:
    allowed = tmp_path / "allowed-root"
    fixture = allowed / "tool-fixture"
    fixture.mkdir(parents=True)
    (fixture / _MARKER).write_text("wp3", encoding="utf-8")
    model = _SecurityModel(f"CALL: list_files({fixture.resolve()})", authorized=True)
    monkeypatch.setattr(server, "settings", _settings(tmp_path))
    monkeypatch.setattr(server, "LocalLLMEngine", lambda **kwargs: model)
    monkeypatch.setattr(server, "RemoteLLMEngine", lambda **kwargs: (_ for _ in ()).throw(AssertionError()))
    run_id = uuid.uuid4().hex

    async with server.lifespan(server.app):
        service = server.app.state.chat_service
        registration = service.router.tool_registry.require("list_files")
        invocation = registration.adapter.build_invocation(str(fixture.resolve()))
        request = service.router.resource_authorization_service.extract(invocation)
        assert request is not None
        assert service.router.resource_authorization_service.authorize(request).outcome is ResourceAuthorizationOutcome.ALLOW
        harness = _AsgiHarness(f"列出目录 {fixture.resolve()}", run_id)
        await harness.run()
        start, controls, texts = harness.parsed()
        journal = server.app.state.runtime_services.event_journal.read_after(run_id, 0, 1000)
        assert start["status"] == 200
        assert texts == [f"找到 {_MARKER}"]
        counts = _event_counts(controls)
        assert counts[RuntimeEventType.TOOL_STARTED.value] == 1
        assert counts[RuntimeEventType.TOOL_COMPLETED.value] == 1
        assert Counter(item.event_type for item in journal)[RuntimeEventType.TOOL_STARTED] == 1
        assert Counter(item.event_type for item in journal)[RuntimeEventType.TOOL_COMPLETED] == 1
        _assert_terminal(controls)
    assert Counter(model.stages) == Counter({"PLANNING": 1, "TOOL_PLANNER": 1, "FINAL_ANSWER": 1})


@pytest.mark.asyncio
async def test_model_generated_outside_path_denied_full_http_chain(monkeypatch, tmp_path: Path) -> None:
    allowed = tmp_path / "allowed-root"
    outside = tmp_path / "outside-root"
    allowed.mkdir()
    outside.mkdir()
    secret = outside / "secret-marker.txt"
    secret.write_text("WP3_FORBIDDEN_SECRET", encoding="utf-8")
    model = _SecurityModel(f"CALL: list_files({outside.resolve()})", authorized=False)
    monkeypatch.setattr(server, "settings", _settings(tmp_path))
    monkeypatch.setattr(server, "LocalLLMEngine", lambda **kwargs: model)
    monkeypatch.setattr(server, "RemoteLLMEngine", lambda **kwargs: (_ for _ in ()).throw(AssertionError()))
    run_id = uuid.uuid4().hex

    async with server.lifespan(server.app):
        service = server.app.state.chat_service
        registration = service.router.tool_registry.require("list_files")
        governance = ToolGovernanceContext("core_router", run_id, "answer")
        assert service.router.tool_governance_service.authorize_tool(governance, registration).outcome is ToolGovernanceOutcome.ALLOW
        invocation = registration.adapter.build_invocation(str(outside.resolve()))
        spec = registration.adapter.spec_for(invocation)
        assert service.router.tool_governance_service.evaluate_invocation(governance, registration, invocation, spec).outcome is ToolGovernanceOutcome.ALLOW
        request = service.router.resource_authorization_service.extract(invocation)
        assert request is not None
        assert service.router.resource_authorization_service.authorize(request).outcome is ResourceAuthorizationOutcome.DENY

        harness = _AsgiHarness(
            f"请使用文件工具查看我给出的目录内容（我已授权）：{outside.resolve()}",
            run_id,
        )
        await harness.run()
        start, controls, texts = harness.parsed()
        journal = server.app.state.runtime_services.event_journal.read_after(run_id, 0, 1000)
        history = service.router.memory_manager.get_chat_history(
            "core_router", limit=10, ascending=True, memory_scope="direct"
        )
        assert start["status"] == 200
        assert texts == [RESOURCE_DENIAL_MESSAGE]
        rendered = "".join(texts)
        assert str(outside) not in rendered and secret.name not in rendered
        counts = _event_counts(controls)
        assert counts[RuntimeEventType.TOOL_STARTED.value] == 0
        assert counts[RuntimeEventType.TOOL_COMPLETED.value] == 0
        assert counts[RuntimeEventType.STEP_COMPLETED.value] == 1
        assert counts[RuntimeEventType.RUN_COMPLETED.value] == 1
        journal_counts = Counter(item.event_type for item in journal)
        assert journal_counts[RuntimeEventType.TOOL_STARTED] == 0
        assert journal_counts[RuntimeEventType.TOOL_COMPLETED] == 0
        assert journal_counts[RuntimeEventType.OUTPUT_DELTA] == 1
        assert [item["role"] for item in history] == ["user", "assistant"]
        assert history[-1]["content"] == RESOURCE_DENIAL_MESSAGE
        assert str(outside) not in history[-1]["content"]
        _assert_terminal(controls)
    assert Counter(model.stages) == Counter({"PLANNING": 1, "TOOL_PLANNER": 1})
    assert secret.read_text(encoding="utf-8") == "WP3_FORBIDDEN_SECRET"
