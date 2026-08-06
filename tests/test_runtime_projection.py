from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from core.runtime.events import (
    ErrorPayload,
    ModelStartedPayload,
    OutputDeltaPayload,
    PlanCreatedPayload,
    PlanningStartedPayload,
    RunCompletedPayload,
    RunStartedPayload,
    RuntimeEvent,
    RuntimeEventType,
    StepCompletedPayload,
    StepStartedPayload,
    ToolCompletedPayload,
)
from core.runtime.runtime_projection import (
    PlanStatus,
    PlanningStatus,
    ProjectionSequenceError,
    RunProjection,
    RuntimeProjectionBuilder,
    SynthesisStatus,
)


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def event(
    sequence: int,
    event_type: RuntimeEventType,
    payload,
    *,
    step_id: str | None = None,
    event_id: str | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        schema_version=2,
        event_id=event_id or uuid4().hex,
        run_id="run",
        trace_id="trace",
        sequence=sequence,
        event_type=event_type,
        emitted_at=NOW,
        component="test",
        payload=payload,
        step_id=step_id,
        step_sequence=1 if step_id else None,
    )


def test_planning_specialists_synthesis_delivery_completed_sequence():
    builder = RuntimeProjectionBuilder()
    builder.apply(
        event(1, RuntimeEventType.RUN_STARTED, RunStartedPayload("RUNNING"))
    )
    builder.apply(
        event(
            2,
            RuntimeEventType.PLANNING_STARTED,
            PlanningStartedPayload(1, 15000),
        )
    )
    projection = builder.projection
    assert projection.planning_status is PlanningStatus.PLANNING

    builder.apply(
        event(
            3,
            RuntimeEventType.PLAN_CREATED,
            PlanCreatedPayload("plan", 1, "a" * 64, 3, "MODEL", shape="3"),
        )
    )
    assert builder.projection.planning_status is PlanningStatus.COMPLETED
    assert builder.projection.plan_status is PlanStatus.CREATED
    assert builder.projection.plan_shape == "3"

    builder.apply(
        event(
            4,
            RuntimeEventType.STEP_STARTED,
            StepStartedPayload(
                "RUNNING",
                agent_id="code_expert",
                execution_kind="AGENT",
                output_policy="INTERNAL",
                dependency_count=0,
            ),
            step_id="code",
        )
    )
    builder.apply(
        event(
            5,
            RuntimeEventType.STEP_STARTED,
            StepStartedPayload(
                "RUNNING",
                agent_id="knowledge_expert",
                execution_kind="AGENT",
                output_policy="INTERNAL",
                dependency_count=0,
            ),
            step_id="knowledge",
        )
    )
    assert builder.projection.active_steps == ("code", "knowledge")

    builder.apply(
        event(
            6,
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload(
                "SUCCEEDED",
                duration_ms=10,
                result_char_count=12,
            ),
            step_id="code",
        )
    )
    builder.apply(
        event(
            7,
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload(
                "SUCCEEDED",
                duration_ms=12,
                result_char_count=14,
            ),
            step_id="knowledge",
        )
    )
    builder.apply(
        event(
            8,
            RuntimeEventType.STEP_STARTED,
            StepStartedPayload(
                "RUNNING",
                agent_id="synthesis_agent",
                execution_kind="SYNTHESIS",
                output_policy="FINAL_SYNTHESIS",
                dependency_count=2,
            ),
            step_id="synthesis",
        )
    )
    assert builder.projection.synthesis_status is SynthesisStatus.RUNNING
    builder.apply(
        event(
            9,
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload(
                "SUCCEEDED",
                duration_ms=20,
                result_char_count=30,
                delivery_status="DELIVERED",
                delivery_duration_ms=3,
                execution_kind="SYNTHESIS",
                output_policy="FINAL_SYNTHESIS",
            ),
            step_id="synthesis",
        )
    )
    assert builder.projection.synthesis_status is SynthesisStatus.COMPLETED
    assert builder.projection.completed_steps == (
        "code",
        "knowledge",
        "synthesis",
    )
    assert builder.projection.delivery_status == "DELIVERED"

    builder.apply(
        event(10, RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload("answer"))
    )
    assert builder.projection.output_journaled
    builder.apply(
        event(
            11,
            RuntimeEventType.RUN_COMPLETED,
            RunCompletedPayload(
                "SUCCEEDED",
                "COMPLETED",
                duration_ms=100,
                delivery_status="DELIVERED",
                final_step_status="SUCCEEDED",
                memory_commit_status="SUCCEEDED",
                shape="3",
            ),
        )
    )
    projection = builder.projection
    assert projection.run_status == "SUCCEEDED"
    assert projection.stop_reason == "COMPLETED"
    assert projection.memory_commit_status == "SUCCEEDED"
    assert projection.terminal


def test_delivery_failed_and_memory_failed_are_layered():
    builder = RuntimeProjectionBuilder()
    builder.apply(
        event(1, RuntimeEventType.RUN_STARTED, RunStartedPayload("RUNNING"))
    )
    builder.apply(
        event(
            2,
            RuntimeEventType.PLAN_CREATED,
            PlanCreatedPayload("plan", 1, "a" * 64, 1, "MODEL", shape="1"),
        )
    )
    builder.apply(
        event(
            3,
            RuntimeEventType.STEP_STARTED,
            StepStartedPayload(
                "RUNNING",
                agent_id="knowledge_expert",
                execution_kind="AGENT",
                output_policy="FINAL_PASSTHROUGH",
            ),
            step_id="answer",
        )
    )
    builder.apply(
        event(
            4,
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload(
                "SUCCEEDED",
                duration_ms=5,
                result_char_count=8,
                delivery_status="DELIVERED",
            ),
            step_id="answer",
        )
    )
    builder.apply(
        event(
            5,
            RuntimeEventType.ERROR,
            ErrorPayload(
                "FINAL_OUTPUT_MEMORY_COMMIT_FAILED",
                "memory failed",
                "step_completion",
                True,
                delivery_status="DELIVERED",
                final_step_status="SUCCEEDED",
                memory_commit_status="FAILED",
            ),
        )
    )
    builder.apply(
        event(
            6,
            RuntimeEventType.RUN_COMPLETED,
            RunCompletedPayload(
                "FAILED",
                "UNHANDLED_ERROR",
                duration_ms=50,
                safe_error_code="FINAL_OUTPUT_MEMORY_COMMIT_FAILED",
                delivery_status="DELIVERED",
                final_step_status="SUCCEEDED",
                memory_commit_status="FAILED",
            ),
        )
    )
    projection = builder.projection
    assert projection.run_status == "FAILED"
    assert projection.delivery_status == "DELIVERED"
    assert projection.memory_commit_status == "FAILED"
    assert (
        projection.safe_error_code == "FINAL_OUTPUT_MEMORY_COMMIT_FAILED"
    )
    # Step 执行成功与 Delivery 成功都必须可区分，避免前端只显示“运行失败”。
    assert projection.completed_steps == ("answer",)


def test_delivery_unknown_projection():
    builder = RuntimeProjectionBuilder()
    builder.apply(
        event(1, RuntimeEventType.RUN_STARTED, RunStartedPayload("RUNNING"))
    )
    builder.apply(
        event(
            2,
            RuntimeEventType.ERROR,
            ErrorPayload(
                "FINAL_OUTPUT_DELIVERY_UNKNOWN",
                "unknown",
                "output_gate",
                True,
                delivery_status="OUTCOME_UNKNOWN",
                final_step_status="SUCCEEDED",
            ),
        )
    )
    builder.apply(
        event(
            3,
            RuntimeEventType.RUN_COMPLETED,
            RunCompletedPayload(
                "FAILED",
                "UNHANDLED_ERROR",
                duration_ms=10,
                safe_error_code="FINAL_OUTPUT_DELIVERY_UNKNOWN",
                delivery_status="OUTCOME_UNKNOWN",
                final_step_status="SUCCEEDED",
                memory_commit_status="NOT_ATTEMPTED",
            ),
        )
    )
    assert builder.projection.delivery_status == "OUTCOME_UNKNOWN"
    assert builder.projection.memory_commit_status == "NOT_ATTEMPTED"


def test_duplicate_event_is_idempotent_and_regression_is_rejected():
    builder = RuntimeProjectionBuilder()
    first = event(
        1,
        RuntimeEventType.PLANNING_STARTED,
        PlanningStartedPayload(1, 15000),
    )
    builder.apply(first)
    builder.apply(first)
    assert builder.projection.planning_status is PlanningStatus.PLANNING

    with pytest.raises(ProjectionSequenceError):
        builder.apply(
            event(
                1,
                RuntimeEventType.RUN_STARTED,
                RunStartedPayload("RUNNING"),
            )
        )
    with pytest.raises(ProjectionSequenceError):
        builder.apply(
            event(
                1,
                RuntimeEventType.PLANNING_STARTED,
                PlanningStartedPayload(1, 15000),
                event_id="different-id",
            )
        )
    class FakeEvent:
        event_type = RuntimeEventType.RUN_STARTED
        event_id = "zero"
        sequence = 0
        payload = RunStartedPayload("RUNNING")

    with pytest.raises(ProjectionSequenceError):
        builder.apply(FakeEvent())


def test_unknown_and_observation_events_are_safely_ignored():
    builder = RuntimeProjectionBuilder()
    builder.apply(
        event(1, RuntimeEventType.RUN_STARTED, RunStartedPayload("RUNNING"))
    )
    # 未识别 event_type 视为未知 control event：安全忽略且推进 sequence。
    builder.apply(
        event(
            2,
            RuntimeEventType.MODEL_STARTED,
            ModelStartedPayload("p", 0, 0, "NONE", "key"),
        )
    )
    builder.apply(
        event(
            3,
            RuntimeEventType.TOOL_COMPLETED,
            ToolCompletedPayload("calc", True),
        )
    )
    projection = builder.projection
    assert projection.run_status == "RUNNING"
    assert projection.last_sequence == 3
    assert projection.output_journaled is False


def test_projection_never_holds_raw_content():
    builder = RuntimeProjectionBuilder()
    builder.apply(
        event(1, RuntimeEventType.RUN_STARTED, RunStartedPayload("RUNNING"))
    )
    builder.apply(
        event(2, RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload("SECRET"))
    )
    data = builder.projection
    rendered = repr(data)
    assert "SECRET" not in rendered
    assert isinstance(data, RunProjection)
