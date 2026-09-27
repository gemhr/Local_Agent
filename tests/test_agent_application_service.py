from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.agent_platform.application import (
    AgentApplicationError,
    AgentApplicationService,
    ExecutionRequest,
)
from core.agent_platform.contracts import AgentDefinition
from core.runtime.events import OutputDeltaPayload
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
    service = AgentApplicationService(chat, _Registry())

    import asyncio
    result = asyncio.run(service.execute(ExecutionRequest(
        "business_agent", "question", expected_agent_version="v1", run_id="requested-run"
    )))

    assert result.run_id == "requested-run"
    assert result.trace_id == "runtime-trace"
    assert result.agent_version == "v1"
    assert result.output == "answer"
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
        yield SimpleNamespace(payload=OutputDeltaPayload("answer"))


def test_stream_projects_one_execution_as_safe_business_events():
    import asyncio

    async def collect():
        return [event async for event in AgentApplicationService(
            _StreamingChatService(), _Registry()
        ).stream(ExecutionRequest("business_agent", "question"))]

    events = asyncio.run(collect())
    assert [event.kind for event in events] == ["output_delta", "terminal"]
    assert events[0].output_delta == "answer"
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
    assert [event.kind for event in events] == ["terminal"]
    assert events[0].status is RunStatus.SUCCEEDED
    assert events[0].business_output_valid is False
    assert events[0].business_error_code == "BUSINESS_OUTPUT_INVALID"
    assert events[0].output_disposition == "REJECTED"
