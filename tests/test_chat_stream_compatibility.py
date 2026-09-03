from __future__ import annotations

import json

import pytest

import main
from core.runtime import (
    BudgetExhaustedPayload,
    CancellationPayload,
    ChatStreamChunkKind,
    ChatStreamCompatibilityAdapter,
    ChatStreamProtocolError,
    ErrorPayload,
    OutputDeltaPayload,
    RunCompletedPayload,
    RunStartedPayload,
    RuntimeEventChannel,
    RuntimeEventDraft,
    RuntimeEventType,
    StepCompletedPayload,
    TimeoutPayload,
    ToolCompletedPayload,
)


async def _event(event_type, payload, *, sequence_run="run-a", step=False):
    channel = RuntimeEventChannel(4, run_id=sequence_run)
    return await channel.publish(
        RuntimeEventDraft(
            sequence_run,
            "trace-secret",
            event_type,
            "component",
            payload,
            "answer" if step else None,
            1 if step else None,
            span_id="span-secret",
        )
    )


@pytest.mark.asyncio
async def test_output_delta_is_the_only_plain_text_source_and_empty_is_ignored():
    adapter = ChatStreamCompatibilityAdapter()
    first = adapter.adapt(
        await _event(
            RuntimeEventType.OUTPUT_DELTA,
            OutputDeltaPayload("one"),
            step=True,
        )
    )
    empty = adapter.adapt(
        await _event(
            RuntimeEventType.OUTPUT_DELTA,
            OutputDeltaPayload(""),
            step=True,
        )
    )
    second = adapter.adapt(
        await _event(
            RuntimeEventType.OUTPUT_DELTA,
            OutputDeltaPayload(" two"),
            step=True,
        )
    )

    assert first is not None and first.kind is ChatStreamChunkKind.TEXT
    assert second is not None and second.kind is ChatStreamChunkKind.TEXT
    assert first.text + second.text == "one two"
    assert empty is None


@pytest.mark.asyncio
async def test_control_projection_omits_trace_span_event_identity_and_evidence():
    adapter = ChatStreamCompatibilityAdapter()
    event = await _event(
        RuntimeEventType.TOOL_COMPLETED,
        ToolCompletedPayload(
            "calculator",
            True,
            invocation_id="sensitive-invocation",
            attempt_id="sensitive-attempt",
            resource_key_digest="sensitive-digest",
        ),
        step=True,
    )

    chunk = adapter.adapt(event)
    assert chunk is not None and chunk.kind is ChatStreamChunkKind.CONTROL
    projection = json.loads(chunk.text.removeprefix("[[ORCH]]"))
    assert projection["event_type"] == "TOOL_COMPLETED"
    assert projection["payload"]["tool_name"] == "calculator"
    rendered = chunk.text.lower()
    for forbidden in (
        "trace-secret",
        "span-secret",
        "sensitive-invocation",
        "sensitive-attempt",
        "sensitive-digest",
        "final_output",
        "prompt",
    ):
        assert forbidden not in rendered


@pytest.mark.asyncio
async def test_runtime_error_maps_to_one_fixed_safe_error_without_raw_message():
    adapter = ChatStreamCompatibilityAdapter()
    chunk = adapter.adapt(
        await _event(
            RuntimeEventType.ERROR,
            ErrorPayload(
                "PROVIDER_SECRET_ERROR",
                "C:/secret token=abc provider exploded",
                "model",
                True,
            ),
        )
    )

    assert chunk is not None
    assert chunk.kind is ChatStreamChunkKind.SAFE_ERROR
    assert chunk.text == "[runtime-error] RUNTIME_EXECUTION_FAILED\n"
    assert "secret" not in chunk.text.lower()


@pytest.mark.asyncio
async def test_rejected_tool_approval_has_a_clear_safe_user_message():
    chunk = ChatStreamCompatibilityAdapter().adapt(
        await _event(
            RuntimeEventType.ERROR,
            ErrorPayload(
                "TOOL_APPROVAL_REJECTED",
                "Tool 调用已被拒绝审批（TOOL_APPROVAL_REJECTED）",
                "run_coordinator",
                True,
            ),
        )
    )

    assert chunk is not None
    assert chunk.kind is ChatStreamChunkKind.TEXT
    assert chunk.text == "已拒绝本次工具调用，未执行任何操作。\n"


@pytest.mark.asyncio
async def test_rejected_step_maps_its_generic_run_failure_to_safe_user_message():
    adapter = ChatStreamCompatibilityAdapter()
    step = adapter.adapt(
        await _event(
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload(
                "FAILED", safe_error_code="TOOL_APPROVAL_REJECTED"
            ),
            step=True,
        )
    )
    error = adapter.adapt(
        await _event(
            RuntimeEventType.ERROR,
            ErrorPayload(
                "AGENT_STEP_FAILED",
                "一个或多个步骤执行失败",
                "run_coordinator",
                True,
            ),
        )
    )

    assert step is not None and step.kind is ChatStreamChunkKind.CONTROL
    assert error is not None and error.kind is ChatStreamChunkKind.TEXT
    assert error.text == "已拒绝本次工具调用，未执行任何操作。\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            RuntimeEventType.CANCELLATION,
            CancellationPayload("USER_CANCELLED", "run"),
        ),
        (RuntimeEventType.TIMEOUT, TimeoutPayload("run")),
        (
            RuntimeEventType.BUDGET_EXHAUSTED,
            BudgetExhaustedPayload("run", "tokens"),
        ),
    ],
)
async def test_safe_terminal_related_events_remain_control_chunks(
    event_type,
    payload,
):
    chunk = ChatStreamCompatibilityAdapter().adapt(
        await _event(event_type, payload)
    )
    assert chunk is not None
    assert chunk.kind is ChatStreamChunkKind.CONTROL
    assert json.loads(chunk.text.removeprefix("[[ORCH]]"))[
        "event_type"
    ] == event_type.value


@pytest.mark.asyncio
async def test_run_completed_is_control_only_and_does_not_repeat_final_output():
    adapter = ChatStreamCompatibilityAdapter()
    chunk = adapter.adapt(
        await _event(
            RuntimeEventType.RUN_COMPLETED,
            RunCompletedPayload("SUCCEEDED", "COMPLETED"),
        )
    )

    assert chunk is not None and chunk.kind is ChatStreamChunkKind.CONTROL
    assert "final_output" not in chunk.text
    assert adapter.terminal_seen is True
    assert adapter.finish() is None


@pytest.mark.asyncio
async def test_missing_terminal_returns_transport_error_without_fabricating_terminal():
    adapter = ChatStreamCompatibilityAdapter()
    adapter.adapt(
        await _event(
            RuntimeEventType.RUN_STARTED,
            RunStartedPayload("RUNNING"),
        )
    )

    chunk = adapter.finish()
    assert chunk is not None
    assert chunk.kind is ChatStreamChunkKind.SAFE_ERROR
    assert chunk.text == "[runtime-error] RUNTIME_TERMINAL_MISSING\n"
    assert "RUN_COMPLETED" not in chunk.text


@pytest.mark.asyncio
async def test_duplicate_terminal_and_business_event_after_terminal_are_rejected():
    terminal = await _event(
        RuntimeEventType.RUN_COMPLETED,
        RunCompletedPayload("SUCCEEDED", "COMPLETED"),
    )
    output = await _event(
        RuntimeEventType.OUTPUT_DELTA,
        OutputDeltaPayload("late"),
        step=True,
    )

    first_adapter = ChatStreamCompatibilityAdapter()
    first_adapter.adapt(terminal)
    with pytest.raises(
        ChatStreamProtocolError,
        match="RUNTIME_STREAM_ENCODING_FAILED",
    ):
        first_adapter.adapt(terminal)

    second_adapter = ChatStreamCompatibilityAdapter()
    second_adapter.adapt(terminal)
    with pytest.raises(
        ChatStreamProtocolError,
        match="RUNTIME_STREAM_ENCODING_FAILED",
    ):
        second_adapter.adapt(output)


@pytest.mark.asyncio
async def test_encoding_failure_is_projected_to_a_safe_protocol_error(monkeypatch):
    adapter = ChatStreamCompatibilityAdapter()
    event = await _event(
        RuntimeEventType.RUN_STARTED,
        RunStartedPayload("RUNNING"),
    )
    monkeypatch.setattr(
        "core.runtime.stream_adapter.json.dumps",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            TypeError("C:/secret")
        ),
    )

    with pytest.raises(
        ChatStreamProtocolError,
        match="RUNTIME_STREAM_ENCODING_FAILED",
    ):
        adapter.adapt(event)


@pytest.mark.asyncio
async def test_desktop_parser_handles_control_chunk_split_at_every_boundary():
    adapter = ChatStreamCompatibilityAdapter()
    chunk = adapter.adapt(
        await _event(
            RuntimeEventType.RUN_STARTED,
            RunStartedPayload("RUNNING"),
        )
    )
    assert chunk is not None

    for boundary in range(1, len(chunk.text)):
        worker = main.ApiWorker("http://test/api/chat")
        plain: list[str] = []
        statuses: list[dict] = []
        worker.chunk_signal.connect(plain.append)
        worker.status_signal.connect(statuses.append)
        worker._emit_stream_payload(chunk.text[:boundary])
        worker._emit_stream_payload(chunk.text[boundary:])
        assert plain == []
        assert [item["event_type"] for item in statuses] == ["RUN_STARTED"]
