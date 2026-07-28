from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from core.runtime import (
    CancellationPayload,
    AgentState,
    BudgetLedger,
    ErrorPayload,
    InMemoryStructuredRuntimeLogger,
    JsonStructuredRuntimeLogger,
    JournalRecord,
    OutputDeltaPayload,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeLogLevel,
    RunBudget,
    RunStatus,
    StepState,
    StepStatus,
    StopReason,
    StructuredLogProjector,
    ToolCompletedPayload,
    create_run_context,
)
from core.runtime.agent_loop import _log_safe_agent_state
from server import _close_model_engines


def record(
    event_type,
    payload,
    *,
    event_id="event-1",
    emitted_at=None,
    journaled_at=None,
):
    return JournalRecord.from_event(
        RuntimeEvent(
            schema_version=1,
            event_id=event_id,
            run_id="run-secret-id",
            trace_id="trace-secret-id",
            sequence=1,
            event_type=event_type,
            emitted_at=emitted_at or datetime.now(UTC),
            component="test_component",
            payload=payload,
            step_id="step-secret-id",
            step_sequence=1,
        ),
        journaled_at=journaled_at,
    )


def test_identity_and_json_line_are_explicit_and_utc():
    stream = io.StringIO()
    projector = StructuredLogProjector(JsonStructuredRuntimeLogger(stream))
    source = record(
        RuntimeEventType.TOOL_COMPLETED,
        ToolCompletedPayload("calculator", True, invocation_id="hidden-call"),
    )
    projected = projector.project(source)
    line = stream.getvalue()
    assert line.count("\n") == 1
    value = json.loads(line)
    assert value["event_id"] == source.event_id
    assert value["run_id"] == source.run_id
    assert value["trace_id"] == source.trace_id
    assert value["timestamp"].endswith("+00:00")
    assert value["journaled_at"].endswith("+00:00")
    assert projected.timestamp.utcoffset().total_seconds() == 0


@pytest.mark.parametrize(
    ("event_type", "payload", "expected"),
    [
        (
            RuntimeEventType.CANCELLATION,
            CancellationPayload("USER_REQUESTED", "run_coordinator"),
            RuntimeLogLevel.INFO,
        ),
        (
            RuntimeEventType.CANCELLATION,
            CancellationPayload("SYSTEM_SHUTDOWN", "run_coordinator"),
            RuntimeLogLevel.WARNING,
        ),
        (
            RuntimeEventType.ERROR,
            ErrorPayload("INTERNAL_ERROR", "safe", "coordinator", True),
            RuntimeLogLevel.ERROR,
        ),
    ],
)
def test_fixed_log_level_policy(event_type, payload, expected):
    logger = InMemoryStructuredRuntimeLogger()
    StructuredLogProjector(logger).project(record(event_type, payload))
    assert logger.records[0].level is expected


def test_safe_projection_excludes_output_identifiers_and_raw_content():
    secret = "PROMPT TOOL_OUTPUT RAG_CHUNK MEMORY SECRET=abc"
    stream = io.StringIO()
    StructuredLogProjector(JsonStructuredRuntimeLogger(stream)).project(
        record(RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload(secret))
    )
    value = stream.getvalue()
    assert secret not in value
    assert "text_digest" not in value
    assert "text_length" in value


def test_error_safe_message_is_not_written():
    raw_like = "password=raw-exception-message"
    stream = io.StringIO()
    StructuredLogProjector(JsonStructuredRuntimeLogger(stream)).project(
        record(
            RuntimeEventType.ERROR,
            ErrorPayload("INTERNAL_ERROR", raw_like, "coordinator", True),
        )
    )
    assert raw_like not in stream.getvalue()
    assert "INTERNAL_ERROR" in stream.getvalue()


def test_event_timestamp_and_journaled_timestamp_survive_projection():
    emitted_at = datetime(2026, 7, 29, 1, 2, 3, tzinfo=UTC)
    journaled_at = emitted_at + timedelta(milliseconds=125)
    stream = io.StringIO()
    source = record(
        RuntimeEventType.TOOL_COMPLETED,
        ToolCompletedPayload("calculator", True, duration_ms=7),
        emitted_at=emitted_at,
        journaled_at=journaled_at,
    )
    projected = StructuredLogProjector(
        JsonStructuredRuntimeLogger(stream)
    ).project(source)
    value = json.loads(stream.getvalue())
    assert projected.timestamp == source.emitted_at == emitted_at
    assert projected.journaled_at == source.journaled_at == journaled_at
    assert value["timestamp"] == emitted_at.isoformat()
    assert value["journaled_at"] == journaled_at.isoformat()
    assert value["journal_latency_ms"] == 125


def test_standard_and_structured_logs_never_emit_sensitive_markers(caplog):
    markers = (
        "SECRET_PROMPT_TEXT",
        "TOOL_OUTPUT_SECRET",
        "RAG_CHUNK_SECRET",
        "MEMORY_SECRET",
        r"C:\Users\private-user\kb",
        "provider-secret-error",
    )
    run_context, _ = create_run_context(entry_agent_id="test")
    run_context.attach_budget_ledger(BudgetLedger(RunBudget()))
    now = datetime.now(UTC)
    state = AgentState.for_run_context(run_context.run_id)
    state.status = RunStatus.FAILED
    state.stop_reason = StopReason.UNHANDLED_ERROR
    state.final_output = " ".join(markers[:2])
    state.error_message = markers[5]
    state.steps["step-a"] = StepState(
        "step-a",
        markers[2],
        status=StepStatus.FAILED,
        created_at=now,
        started_at=now,
        ended_at=now,
        error_code="SAFE_ERROR",
        error_message=markers[3],
    )

    class BrokenEngine:
        def close(self):
            raise RuntimeError(f"{markers[4]} {markers[5]}")

    caplog.set_level(logging.INFO)
    _log_safe_agent_state(state, run_context)
    assert _close_model_engines({"local": BrokenEngine()}) == (
        "MODEL_ENGINE_CLOSE_FAILED",
    )

    stream = io.StringIO()
    StructuredLogProjector(JsonStructuredRuntimeLogger(stream)).project(
        record(
            RuntimeEventType.OUTPUT_DELTA,
            OutputDeltaPayload(" ".join(markers)),
        )
    )
    standard_log_projection = "\n".join(
        f"{item.getMessage()} {item.__dict__}" for item in caplog.records
    )
    all_logs = standard_log_projection + stream.getvalue()
    for marker in markers:
        assert marker not in all_logs
    assert all("agent_state" not in item.__dict__ for item in caplog.records)
