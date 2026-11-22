from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from unittest.mock import Mock
from uuid import uuid4

import pytest

from core.agent_platform.application import AgentApplicationError, AgentApplicationService, ExecutionRequest
from core.agent_platform.contracts import AgentDefinition, AgentRegistration
from core.agent_platform.registry import AgentRegistrationBundle, compile_agent_catalog
from core.agent_platform.runtime_adapter import ExecutionResult, RuntimeAdapterError, RuntimeRequest, RuntimeUpdate
from core.runtime.cancellation import CancellationReason, CancellationSource
from core.runtime.run_registry import ActiveRunControlHandle, RunRegistry
from core.runtime.state import RunStatus, StopReason


class _FakeRuntime:
    output = '{"ok":true}'

    def __init__(self, outcome: RunStatus | None = RunStatus.SUCCEEDED, *, wait_for_cancel: bool = False) -> None:
        self.outcome = outcome
        self.wait_for_cancel = wait_for_cancel
        self.registry = RunRegistry()
        self.requests: list[RuntimeRequest] = []
        self.execution_ids: dict[str, str] = {}

    async def execute(self, request: RuntimeRequest) -> ExecutionResult:
        async with aclosing(self.stream(request)) as events:
            async for event in events:
                if isinstance(event, ExecutionResult):
                    return event
        raise RuntimeAdapterError("RUNTIME_RESULT_MISSING", "Runtime 未返回结构化结果")

    async def stream(self, request: RuntimeRequest) -> AsyncGenerator[RuntimeUpdate | ExecutionResult, None]:
        self.requests.append(request)
        run_id = request.run_id or uuid4().hex
        trace_id = f"fake-trace-{run_id}"
        self.execution_ids[run_id] = f"fake-execution-{uuid4().hex}"
        cancellation = CancellationSource()
        token = cancellation.token
        handle = self.registry.register(ActiveRunControlHandle(
            run_id=run_id, runtime_mode="FAKE", owner="test", cancellation_source=cancellation,
        ))
        try:
            yield RuntimeUpdate("started", run_id, trace_id)
            if self.wait_for_cancel:
                await token.wait_cancelled()
            else:
                if not token.is_cancelled():
                    yield RuntimeUpdate("control_delta", run_id, trace_id, "模拟执行中")
                if self.outcome is RunStatus.SUCCEEDED and not token.is_cancelled():
                    yield RuntimeUpdate("output_delta", run_id, trace_id, self.output)

            status = RunStatus.CANCELLED if token.is_cancelled() else self.outcome
            if status is None:
                return
            stop_reason, code, message = {
                RunStatus.SUCCEEDED: (StopReason.COMPLETED, None, ""),
                RunStatus.FAILED: (StopReason.UNHANDLED_ERROR, "FAKE_EXECUTION_FAILED", "模拟执行失败"),
                RunStatus.CANCELLED: (StopReason.USER_CANCELLED, None, "运行已取消"),
            }[status]
            result = ExecutionResult(
                run_id=run_id, trace_id=trace_id, session_id=request.session_id,
                agent_id=request.agent_id, agent_version=request.agent_version,
                workflow_id=request.workflow_id, workflow_version=request.workflow_version,
                status=status, stop_reason=stop_reason, error_code=code, safe_message=message,
                output=self.output if status is RunStatus.SUCCEEDED else None,
            )
        except GeneratorExit:
            handle.request_cancel(CancellationReason.CLIENT_DISCONNECTED)
            raise
        finally:
            self.registry.unregister(run_id)
        yield result


@pytest.fixture
def fake_application():
    definition = AgentDefinition(
        agent_id="business_agent", agent_version="v1", display_name="Business Agent",
        role="处理业务输入", instructions="执行已注册的任务。",
        input_schema={"type": "object", "required": ["question"]},
        output_schema={"type": "object", "required": ["ok"]},
    )
    registry = compile_agent_catalog(AgentRegistrationBundle(
        registrations=(AgentRegistration(definition),),
    )).agent_registry
    chat = Mock(spec_set=[])

    def build(runtime: _FakeRuntime) -> AgentApplicationService:
        return AgentApplicationService(chat, registry, runtime_adapter=runtime)

    yield build
    assert not chat.mock_calls


@pytest.mark.asyncio
async def test_fake_execute_validates_input_and_projects_result(fake_application):
    runtime = _FakeRuntime()
    application = fake_application(runtime)
    with pytest.raises(AgentApplicationError, match="BUSINESS_INPUT_INVALID"):
        await application.execute(ExecutionRequest("business_agent", "{}"))
    assert runtime.requests == []

    request = ExecutionRequest(
        "business_agent", '{"question":"hello"}', session_id="conversation", expected_agent_version="v1",
    )
    result = await application.execute(request)

    assert runtime.requests == [RuntimeRequest(
        agent_id=request.agent_id, input=request.input, agent_version="v1", session_id=request.session_id,
    )]
    assert result.run_id in runtime.execution_ids
    assert runtime.execution_ids[result.run_id] not in {result.run_id, result.session_id}
    assert result.session_id == request.session_id
    assert result.status is RunStatus.SUCCEEDED
    assert result.stop_reason is StopReason.COMPLETED
    assert result.output == runtime.output
    assert result.business_output_valid is True
    assert result.output_disposition == "ACCEPTED"
    assert runtime.registry.active_handles() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [RunStatus.SUCCEEDED, RunStatus.FAILED])
async def test_fake_stream_projects_progress_and_one_terminal(fake_application, status):
    runtime = _FakeRuntime(status)
    request = ExecutionRequest("business_agent", '{"question":"hello"}')
    events = [event async for event in fake_application(runtime).stream(request)]

    expected = ["started", "control_delta", "terminal"]
    if status is RunStatus.SUCCEEDED:
        expected.insert(2, "output_delta")
    assert [event.kind for event in events] == expected
    terminal = events[-1]
    assert len(runtime.requests) == 1
    assert set(runtime.execution_ids) == {terminal.run_id}
    assert {event.run_id for event in events} == {terminal.run_id}
    assert events[1].control_delta == "模拟执行中"
    assert terminal.status is status
    if status is RunStatus.SUCCEEDED:
        assert events[2].output_delta == runtime.output
        assert terminal.business_output_valid is True
    else:
        assert terminal.stop_reason is StopReason.UNHANDLED_ERROR
        assert terminal.error_code == "FAKE_EXECUTION_FAILED"
        assert terminal.safe_message == "模拟执行失败"
        assert terminal.business_output_valid is None
    assert runtime.registry.active_handles() == ()


@pytest.mark.asyncio
async def test_fake_run_is_cancelled_by_platform_run_id(fake_application):
    runtime = _FakeRuntime(wait_for_cancel=True)
    run_id = uuid4().hex
    request = ExecutionRequest("business_agent", '{"question":"hello"}', run_id=run_id)
    started = asyncio.Event()

    async def collect():
        events = []
        async with aclosing(fake_application(runtime).stream(request)) as stream:
            async for event in stream:
                events.append(event)
                if event.kind == "started":
                    started.set()
        return events

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        consumer = tasks.create_task(collect())
        await started.wait()
        handle = runtime.registry.get(run_id)
        assert handle is not None
        assert runtime.registry.cancel(runtime.execution_ids[run_id], CancellationReason.REQUEST_CANCELLED) is None
        assert runtime.registry.cancel(run_id, CancellationReason.REQUEST_CANCELLED) is True
        assert not handle.is_completed
        assert not consumer.done()

    events = consumer.result()
    assert [event.kind for event in events] == ["started", "terminal"]
    assert {event.run_id for event in events} == {run_id}
    assert events[-1].status is RunStatus.CANCELLED
    assert events[-1].stop_reason is StopReason.USER_CANCELLED
    assert events[-1].output_delta is None
    assert handle.cancellation_source.token.reason is CancellationReason.REQUEST_CANCELLED
    assert handle.is_completed
    assert runtime.registry.active_handles() == ()


@pytest.mark.asyncio
async def test_closing_fake_application_stream_unregisters_active_run(fake_application):
    runtime = _FakeRuntime()
    request = ExecutionRequest("business_agent", '{"question":"hello"}')
    stream = fake_application(runtime).stream(request)

    async with aclosing(stream):
        first = await anext(stream)
        assert first.kind == "started"
        handle = runtime.registry.get(first.run_id)
        assert handle is not None
        assert not handle.is_completed

    assert handle.cancellation_source.token.reason is CancellationReason.CLIENT_DISCONNECTED
    assert handle.is_completed
    assert runtime.registry.active_handles() == ()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_fake_stream_eof_without_result_does_not_project_terminal(fake_application):
    runtime = _FakeRuntime(outcome=None)
    request = ExecutionRequest("business_agent", '{"question":"hello"}')
    kinds = []
    with pytest.raises(AgentApplicationError, match="RUNTIME_RESULT_MISSING"):
        async for event in fake_application(runtime).stream(request):
            kinds.append(event.kind)
    assert kinds == ["started", "control_delta"]
    assert runtime.registry.active_handles() == ()
