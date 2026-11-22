import asyncio
from contextlib import aclosing

import pytest

from core.agent_platform.application import AgentApplicationError, AgentApplicationService, ExecutionRequest
from core.agent_platform.runtime_adapter import NativeRuntimeAdapter
from core.chat_service import ChatRuntimeTransportError, ChatService
from core.persistence.memory import PostgresMemoryManager, PostgresMemoryManagerBridge
from core.persistence.sync_bridge import SyncPersistenceBridge
from core.runtime.events import RuntimeEventType
from core.runtime.model_invocation import ModelAdapterResponse
from core.runtime.state import RunStatus, StopReason
from tests._runtime_assembly_fixtures import FakeRouter
from tests.test_agent_loop_runtime import assembly
from tests.test_coordinated_stream_lifecycle import _RecordingFactory
from tests.test_scoped_memory_lifecycle import actor


async def _native_application(database, tmp_path, responses, *, capacity=32):
    memory = PostgresMemoryManagerBridge(
        PostgresMemoryManager(database), SyncPersistenceBridge(asyncio.get_running_loop()),
    )
    factory, repository, model, _ = await assembly(database, tmp_path, responses, memory_manager=memory)
    router = factory._router
    router.complete_planning_decision = FakeRouter().complete_planning_decision
    services = factory.services
    registry = factory._agent_registry
    factory = _RecordingFactory(
        router, services, agent_registry=registry, event_channel_capacity=capacity,
        execution_repository=repository,
    )
    chat = ChatService(router, coordinated_runtime_factory=factory, run_registry=services.run_registry)
    application = AgentApplicationService(chat, registry, runtime_adapter=NativeRuntimeAdapter(chat))
    return application, chat, factory, model, await actor(database)


@pytest.mark.asyncio
@pytest.mark.parametrize(("streaming", "fail"), [(False, False), (False, True), (True, False)])
async def test_native_run_projects_result_and_keeps_single_journal_terminal(clean_database, tmp_path, streaming, fail):
    def provider(_messages):
        if fail:
            raise RuntimeError("private provider error")
        return ModelAdapterResponse("assembled-output", finish_reason="stop", tool_call_count=0)

    application, _, factory, model, access = await _native_application(clean_database, tmp_path, [provider])
    request = ExecutionRequest("core_router", "question")
    if streaming:
        events = [event async for event in application.stream(request, memory_access_context=access)]
        assert [event.kind for event in events].count("started") == 1
        assert [event.kind for event in events].count("terminal") == 1
        result = events[-1]
        assert result.kind == "terminal"
        assert {event.run_id for event in events} == {result.run_id}
        output = "".join(event.output_delta for event in events if event.output_delta is not None)
    else:
        result = await application.execute(request, memory_access_context=access)
        output = result.output

    assert len(model.requests) == factory.create_count == 1
    scope = factory.scopes[0]
    assert result.run_id == scope.run_id
    assert scope.is_closed
    services = factory.services
    assert services.run_registry.observability_snapshot()["active_runs"] == 0
    records = await services.event_journal.read_after(result.run_id, 0, 100)
    assert sum(record.event_type is RuntimeEventType.RUN_COMPLETED for record in records) == 1
    feed = await services.client_event_feed.read_after(result.run_id, 0, 100)
    assert sum(event.event_type in {"run.completed", "run.failed"} for event in feed) == 1
    assert scope.budget_ledger.snapshot().active_reservation_count == 0
    if fail:
        assert result.status is RunStatus.FAILED
        assert result.error_code == "AGENT_STEP_FAILED"
        assert "private provider error" not in result.safe_message
        assert output is None
    else:
        assert result.status is RunStatus.SUCCEEDED, (
            result.error_code, result.safe_message,
            [(record.event_type, record.safe_payload) for record in records],
        )
        assert output == "assembled-output"


@pytest.mark.asyncio
async def test_closing_application_stream_drains_native_scope(clean_database, tmp_path):
    application, _, factory, _, access = await _native_application(clean_database, tmp_path, [], capacity=1)
    stream = application.stream(ExecutionRequest("core_router", "question"), memory_access_context=access)

    async with aclosing(stream):
        first = await anext(stream)
        assert first.kind == "started"

    assert factory.create_count == 1
    assert factory.scopes[0].is_closed
    services = factory.services
    assert services.run_registry.observability_snapshot()["active_runs"] == 0
    records = await services.event_journal.read_after(first.run_id, 0, 100)
    terminals = [record for record in records if record.event_type is RuntimeEventType.RUN_COMPLETED]
    assert len(terminals) == 1
    assert terminals[0].safe_payload["status"] == RunStatus.CANCELLED.value
    assert terminals[0].safe_payload["stop_reason"] == StopReason.CLIENT_DISCONNECTED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_native_eof_without_terminal_does_not_report_success(clean_database, tmp_path, monkeypatch, streaming):
    application, chat, factory, _, access = await _native_application(
        clean_database, tmp_path, [ModelAdapterResponse("assembled-output", finish_reason="stop", tool_call_count=0)],
    )
    native_stream = chat.stream_coordinated_agent_events

    async def without_terminal(*args, **kwargs):
        async with aclosing(native_stream(*args, **kwargs)) as events:
            async for event in events:
                if event.event_type is not RuntimeEventType.RUN_COMPLETED:
                    yield event

    monkeypatch.setattr(chat, "stream_coordinated_agent_events", without_terminal)
    request = ExecutionRequest("core_router", "question")

    if streaming:
        with pytest.raises(AgentApplicationError, match="RUNTIME_TERMINAL_MISSING"):
            async for event in application.stream(request, memory_access_context=access):
                assert event.kind != "terminal"
                assert '"event_type":"RUN_COMPLETED"' not in (event.control_delta or "")
    else:
        with pytest.raises(ChatRuntimeTransportError, match="RUNTIME_TERMINAL_MISSING"):
            await application.execute(request, memory_access_context=access)
    assert factory.create_count == 1
    assert factory.scopes[0].is_closed
