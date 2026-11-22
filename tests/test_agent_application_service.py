from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.agent_platform.application import (
    AgentApplicationError,
    AgentApplicationService,
    ExecutionRequest,
)
from core.agent_platform.contracts import AgentDefinition
from core.agent_platform.runtime_adapter import NativeRuntimeAdapter
from core.runtime.events import (
    OutputDeltaPayload,
    RunCompletedPayload,
    RunStartedPayload,
    RuntimeEvent,
    RuntimeEventDraft,
    RuntimeEventType,
)
from core.runtime.state import RunStatus, StopReason


def _registration():
    definition = AgentDefinition(
        agent_id="business_agent",
        agent_version="v1",
        display_name="Business Agent",
        role="Review business input",
        instructions="Follow the registered task instructions.",
    )
    return SimpleNamespace(definition=definition, workflow=None, toolset_identity="a" * 64)


class _Registry:
    def __init__(self, registration=None):
        self.registration = registration or _registration()

    def require_entry(self, agent_id):
        if self.registration.definition.agent_id != agent_id:
            raise LookupError("unknown")
        return self.registration


class _ChatService:
    def __init__(self, *, status=RunStatus.SUCCEEDED):
        self.calls = []
        self.status = status

    async def run_coordinated_agent(self, agent_id, query, **kwargs):
        self.calls.append((agent_id, query, kwargs))
        result = SimpleNamespace(
            run_id=kwargs["run_id"] or "runtime-run",
            trace_id="runtime-trace",
            session_id=kwargs["session_id"] or "default",
            agent_id=agent_id,
            agent_version=kwargs["agent_version"],
            workflow_id=kwargs["workflow_id"],
            workflow_version=kwargs["workflow_version"],
            status=self.status,
            stop_reason=StopReason.COMPLETED if self.status is RunStatus.SUCCEEDED else StopReason.UNHANDLED_ERROR,
            error_code=None if self.status is RunStatus.SUCCEEDED else "RUNTIME_FAILED",
            safe_message="" if self.status is RunStatus.SUCCEEDED else "safe failure",
        )
        return ("answer" if self.status is RunStatus.SUCCEEDED else None), result


def test_execute_resolves_version_and_projects_runtime_identity():
    chat = _ChatService()
    service = AgentApplicationService(chat, _Registry(), runtime_adapter=NativeRuntimeAdapter(chat))

    import asyncio
    result = asyncio.run(service.execute(ExecutionRequest(
        "business_agent", "question", expected_agent_version="v1"
    )))

    assert result.run_id == "runtime-run"
    assert result.trace_id == "runtime-trace"
    assert result.agent_version == "v1"
    assert result.output == "answer"
    assert len(chat.calls) == 1
    assert chat.calls[0][2]["agent_version"] == "v1"
    assert result.toolset_identity == "a" * 64
    assert result.resolved_model_profile_id == "default"
    assert result.resolved_retrieval_profile_id == "NONE"
    assert result.resolved_memory_profile_id == "NONE"


def test_expected_version_mismatch_and_unknown_agent_fail_before_runtime():
    chat = _ChatService()
    service = AgentApplicationService(chat, _Registry())
    import asyncio

    with pytest.raises(AgentApplicationError, match="AGENT_VERSION_MISMATCH"):
        asyncio.run(service.execute(ExecutionRequest(
            "business_agent", "question", expected_agent_version="v2"
        )))
    with pytest.raises(AgentApplicationError, match="UNKNOWN_AGENT"):
        asyncio.run(service.execute(ExecutionRequest("missing_agent", "question")))
    assert chat.calls == []


def test_runtime_failure_is_not_projected_as_business_success():
    chat = _ChatService(status=RunStatus.FAILED)
    service = AgentApplicationService(chat, _Registry())
    import asyncio

    result = asyncio.run(service.execute(ExecutionRequest("business_agent", "question")))
    assert result.status is RunStatus.FAILED
    assert result.error_code == "RUNTIME_FAILED"
    assert result.output is None


@pytest.mark.asyncio
async def test_native_single_call_budget_comes_from_registration():
    registration = _registration()
    registration.definition = replace(registration.definition, business_options={"stage13_single_call": True})
    chat = _ChatService()
    application = AgentApplicationService(chat, _Registry(registration))

    await application.execute(ExecutionRequest("business_agent", "question", timeout_seconds=15))

    _, _, kwargs = chat.calls[0]
    assert kwargs["persist"] is False
    assert kwargs["budget"].max_model_calls == kwargs["budget"].max_remote_model_calls == 1
    assert kwargs["budget"].max_retries == 0
    assert kwargs["budget"].max_elapsed_seconds == 15


def test_business_output_validation_does_not_rewrite_runtime_terminal():
    definition = AgentDefinition(
        agent_id="business_agent",
        agent_version="v1",
        display_name="Business Agent",
        role="Review business input",
        instructions="Follow the registered task instructions.",
        output_schema={"type": "object", "required": ["ok"]},
    )
    service = AgentApplicationService(_ChatService(), _Registry(SimpleNamespace(
        definition=definition, workflow=None
    )))
    import asyncio

    result = asyncio.run(service.execute(ExecutionRequest("business_agent", "question")))
    assert result.status is RunStatus.SUCCEEDED
    assert result.business_output_valid is False
    assert result.error_code == "BUSINESS_OUTPUT_INVALID"
    assert result.output is None
    assert result.rejected_output == "answer"
    assert result.business_error_code == "BUSINESS_OUTPUT_INVALID"
    assert result.output_disposition == "REJECTED"


class _StreamingChatService:
    async def stream_coordinated_agent_events(self, agent_id, query, **kwargs):
        kwargs["_result_out"].append(SimpleNamespace(
            run_id="run", trace_id="trace", session_id="default", agent_id=agent_id,
            agent_version=kwargs["agent_version"], workflow_id=None, workflow_version=None,
            status=RunStatus.SUCCEEDED, stop_reason=StopReason.COMPLETED,
            error_code=None, safe_message="",
        ))
        for sequence, (event_type, payload) in enumerate((
            (RuntimeEventType.RUN_STARTED, RunStartedPayload("RUNNING")),
            (RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload("ans")),
            (RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload("wer")),
            (RuntimeEventType.RUN_COMPLETED, RunCompletedPayload("SUCCEEDED", "COMPLETED")),
        ), 1):
            yield RuntimeEvent.from_draft(RuntimeEventDraft(
                run_id="run", trace_id="trace", event_type=event_type,
                component="test", payload=payload,
            ), sequence)


def test_stream_projects_one_execution_as_safe_business_events():
    import asyncio

    async def collect():
        return [event async for event in AgentApplicationService(
            _StreamingChatService(), _Registry()
        ).stream(ExecutionRequest("business_agent", "question"))]

    events = asyncio.run(collect())
    assert [event.kind for event in events] == [
        "started", "control_delta", "output_delta", "output_delta", "control_delta", "terminal",
    ]
    assert {event.run_id for event in events} == {"run"}
    assert [event.output_delta for event in events if event.kind == "output_delta"] == ["ans", "wer"]
    assert events[-1].status is RunStatus.SUCCEEDED


def test_stream_suppresses_invalid_structured_body_and_keeps_runtime_success():
    import asyncio

    definition = AgentDefinition(
        agent_id="business_agent", agent_version="v1", display_name="Business Agent",
        role="Review business input", instructions="Follow the registered task instructions.",
        output_schema={"type": "object", "required": ["ok"]},
    )

    async def collect():
        return [event async for event in AgentApplicationService(
            _StreamingChatService(), _Registry(SimpleNamespace(definition=definition, workflow=None))
        ).stream(ExecutionRequest("business_agent", "question"))]

    events = asyncio.run(collect())
    assert [event.kind for event in events] == ["started", "control_delta", "control_delta", "terminal"]
    assert events[-1].status is RunStatus.SUCCEEDED
    assert events[-1].business_output_valid is False
    assert events[-1].business_error_code == "BUSINESS_OUTPUT_INVALID"
    assert events[-1].output_disposition == "REJECTED"
