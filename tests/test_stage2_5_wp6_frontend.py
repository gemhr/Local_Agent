"""WP6 frontend/projection supplement: terminal before control events."""

from __future__ import annotations

from datetime import UTC, datetime

from core.runtime import RuntimeEventType
from core.runtime.events import RunCompletedPayload, RunStartedPayload
from core.runtime.multi_agent_status import format_frontend_status
from core.runtime.runtime_projection import (
    PlanningStatus,
    RuntimeProjectionBuilder,
)


NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _event(
    sequence: int,
    event_type: RuntimeEventType,
    payload,
    *,
    event_id: str,
):
    from core.runtime import RuntimeEvent

    return RuntimeEvent(
        schema_version=2,
        event_id=event_id,
        run_id="run-f",
        trace_id="trace",
        sequence=sequence,
        event_type=event_type,
        emitted_at=NOW,
        component="test",
        payload=payload,
    )


def test_run_completed_without_prior_control_events() -> None:
    """RUN_COMPLETED 前缺失 PLANNING_STARTED/PLAN_CREATED/STEP_STARTED：
    投影仍给出终态分层事实，前端文案可安全展示，不崩溃、不虚构。"""
    builder = RuntimeProjectionBuilder()
    builder.apply(
        _event(
            1,
            RuntimeEventType.RUN_STARTED,
            RunStartedPayload("RUNNING"),
            event_id="e-1",
        )
    )
    projection = builder.apply(
        _event(
            2,
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
            event_id="e-2",
        )
    )

    assert projection.terminal is True
    assert projection.run_status == "FAILED"
    assert projection.stop_reason == "UNHANDLED_ERROR"
    assert projection.delivery_status == "OUTCOME_UNKNOWN"
    assert projection.planning_status is PlanningStatus.NOT_STARTED
    assert projection.active_steps == ()
    assert projection.safe_error_code == "FINAL_OUTPUT_DELIVERY_UNKNOWN"
    text = format_frontend_status(
        {
            "event_type": "RUN_COMPLETED",
            "payload": {
                "status": projection.run_status,
                "delivery_status": projection.delivery_status,
                "memory_commit_status": projection.memory_commit_status,
                "safe_error_code": projection.safe_error_code,
            },
        }
    )
    assert "避免重复执行" in (text or "")


def test_frontend_status_never_carries_specialist_raw() -> None:
    """前端状态文案与 projection 不携带 specialist raw / synthesis input。"""
    raw = "SECRET_SPECIALIST_RAW_DO_NOT_DISPLAY"
    builder = RuntimeProjectionBuilder()
    # STEP_COMPLETED 只带安全字段；任何 raw 不得进入 projection。
    from core.runtime import StepCompletedPayload

    builder.apply(
        _event(
            1,
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload(
                "SUCCEEDED",
                duration_ms=1,
                result_char_count=len(raw),
            ),
            event_id="e-1",
        )
    )
    assert raw not in repr(builder.projection)
    assert "content" not in repr(builder.projection)
