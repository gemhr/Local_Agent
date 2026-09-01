from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from core.runtime import (
    BudgetExhaustedPayload,
    CancellationPayload,
    InMemoryMetricsRecorder,
    JournalRecord,
    MetricDescriptor,
    MetricLabelPolicy,
    MetricType,
    ModelCompletedPayload,
    ModelStartedPayload,
    RetrievalBudgetPayload,
    RetrievalCompletedPayload,
    RunCompletedPayload,
    RunStartedPayload,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeMetricsProjector,
    StepCompletedPayload,
    StepStartedPayload,
    ToolStartedPayload,
    ToolCompletedPayload,
    TimeoutPayload,
)


def journal_record(
    event_type,
    payload,
    sequence,
    *,
    when=None,
    event_id=None,
    step_id=None,
):
    return JournalRecord.from_event(
        RuntimeEvent(
            schema_version=1,
            event_id=event_id or f"event-{sequence}",
            run_id="run-a",
            trace_id="trace-a",
            sequence=sequence,
            event_type=event_type,
            emitted_at=when or datetime.now(UTC),
            component="test",
            payload=payload,
            step_id=step_id,
            step_sequence=sequence if step_id else None,
        )
    )


def test_descriptor_naming_and_registered_metrics():
    with pytest.raises(ValueError):
        MetricDescriptor("runs_total", MetricType.COUNTER, "bad", "runs")
    with pytest.raises(ValueError):
        MetricDescriptor("runtime_runs", MetricType.COUNTER, "bad", "runs")
    with pytest.raises(ValueError):
        MetricDescriptor("runtime_active_total", MetricType.GAUGE, "bad", "runs")
    with pytest.raises(ValueError):
        MetricDescriptor("runtime_duration", MetricType.HISTOGRAM, "bad", "seconds")
    recorder = InMemoryMetricsRecorder()
    with pytest.raises(ValueError, match="未注册"):
        recorder.increment_counter("runtime_unknown_total")


def test_type_number_counter_and_label_validation():
    recorder = InMemoryMetricsRecorder(
        label_policy=MetricLabelPolicy(tool_name_allowlist=frozenset({"known"}))
    )
    with pytest.raises(ValueError, match="类型"):
        recorder.set_gauge("runtime_runs_total", 1)
    with pytest.raises(ValueError, match="负数"):
        recorder.increment_counter("runtime_runs_started_total", -1)
    with pytest.raises(ValueError, match="有限"):
        recorder.set_gauge("runtime_active_runs", float("nan"))
    with pytest.raises(ValueError, match="高基数"):
        recorder.increment_counter(
            "runtime_runs_total",
            labels={"run_id": "run-a", "status": "SUCCEEDED"},
        )
    with pytest.raises(ValueError, match="未被 descriptor"):
        recorder.increment_counter(
            "runtime_runs_total", labels={"component": "coordinator"}
        )


def test_tool_name_allowlist_maps_unknown_to_other():
    recorder = InMemoryMetricsRecorder(
        label_policy=MetricLabelPolicy(tool_name_allowlist=frozenset({"known"}))
    )
    recorder.increment_counter(
        "runtime_tool_attempts_total", labels={"tool_name": "dynamic_tool"}
    )
    assert (
        recorder.snapshot().counter(
            "runtime_tool_attempts_total", {"tool_name": "other"}
        )
        == 1
    )


def test_counter_gauge_histogram_and_seconds_conversion():
    recorder = InMemoryMetricsRecorder()
    recorder.increment_counter("runtime_runs_started_total", 2)
    recorder.set_gauge("runtime_active_runs", 3)
    recorder.observe_histogram("runtime_run_duration_seconds", 0.25, labels={"status": "SUCCEEDED"})
    snap = recorder.snapshot()
    assert snap.counter("runtime_runs_started_total") == 2
    assert snap.gauge("runtime_active_runs") == 3
    assert snap.histogram(
        "runtime_run_duration_seconds", {"status": "SUCCEEDED"}
    ) == (0.25,)


def test_event_projection_counts_attempts_retries_terminal_results_and_duration():
    recorder = InMemoryMetricsRecorder()
    projector = RuntimeMetricsProjector(recorder)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    records = [
        journal_record(RuntimeEventType.RUN_STARTED, RunStartedPayload("RUNNING"), 1, when=start),
        journal_record(RuntimeEventType.STEP_STARTED, StepStartedPayload("RUNNING"), 2, when=start, step_id="step"),
        journal_record(
            RuntimeEventType.MODEL_STARTED,
            ModelStartedPayload("local", 0, 1, "NONE", "breaker"),
            3,
            when=start,
            step_id="step",
        ),
        journal_record(
            RuntimeEventType.MODEL_COMPLETED,
            ModelCompletedPayload("local", 0, 1, True, duration_ms=500),
            4,
            when=start + timedelta(milliseconds=500),
            step_id="step",
        ),
        journal_record(
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload("SUCCEEDED", duration_ms=1000),
            5,
            when=start + timedelta(seconds=1),
            step_id="step",
        ),
        journal_record(
            RuntimeEventType.RUN_COMPLETED,
            RunCompletedPayload("SUCCEEDED", "COMPLETED", duration_ms=2000),
            6,
            when=start + timedelta(seconds=2),
        ),
    ]
    for value in records:
        projector.project(value)
    snap = recorder.snapshot()
    assert snap.counter("runtime_runs_started_total") == 1
    assert snap.counter("runtime_runs_total", {"status": "SUCCEEDED"}) == 1
    assert snap.counter("runtime_steps_total", {"status": "SUCCEEDED"}) == 1
    assert snap.counter(
        "runtime_model_attempts_total", {"model_profile": "local"}
    ) == 1
    assert snap.counter("runtime_retries_total", {"component": "model"}) == 1
    assert snap.histogram(
        "runtime_model_duration_seconds",
        {"model_profile": "local", "status": "SUCCEEDED"},
    ) == (0.5,)
    assert snap.histogram(
        "runtime_run_duration_seconds", {"status": "SUCCEEDED"}
    ) == (2.0,)


def test_budget_and_cancellation_are_separate_metrics():
    recorder = InMemoryMetricsRecorder()
    projector = RuntimeMetricsProjector(recorder)
    projector.project(
        journal_record(
            RuntimeEventType.BUDGET_EXHAUSTED,
            BudgetExhaustedPayload("run", "model_calls"),
            1,
        )
    )
    projector.project(
        journal_record(
            RuntimeEventType.CANCELLATION,
            CancellationPayload("USER_REQUESTED", "run"),
            2,
        )
    )
    snap = recorder.snapshot()
    assert snap.counter(
        "runtime_budget_exhaustions_total",
        {
            "component": "run",
            "budget_dimension": "model_calls",
            "status": "EXHAUSTED",
        },
    ) == 1
    assert snap.counter(
        "runtime_cancellations_total",
        {
            "component": "run",
            "cancellation_reason": "USER_REQUESTED",
            "status": "CANCELLED",
        },
    ) == 1


def test_completed_only_replay_restores_every_duration_histogram():
    recorder = InMemoryMetricsRecorder(
        label_policy=MetricLabelPolicy(
            tool_name_allowlist=frozenset({"known"})
        )
    )
    projector = RuntimeMetricsProjector(recorder)
    completed = [
        journal_record(
            RuntimeEventType.MODEL_COMPLETED,
            ModelCompletedPayload(
                "local", 0, 0, True, duration_ms=125
            ),
            1,
            step_id="step",
        ),
        journal_record(
            RuntimeEventType.TOOL_COMPLETED,
            ToolCompletedPayload(
                "known",
                True,
                duration_ms=250,
                status="SUCCEEDED",
            ),
            2,
            step_id="step",
        ),
        journal_record(
            RuntimeEventType.RETRIEVAL_COMPLETED,
            RetrievalCompletedPayload(
                "retrieval-a",
                "SUCCEEDED",
                375,
                0,
                0,
                False,
                RetrievalBudgetPayload(),
            ),
            3,
            step_id="step",
        ),
        journal_record(
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload("SUCCEEDED", duration_ms=500),
            4,
            step_id="step",
        ),
        journal_record(
            RuntimeEventType.RUN_COMPLETED,
            RunCompletedPayload(
                "SUCCEEDED", "COMPLETED", duration_ms=625
            ),
            5,
        ),
    ]
    for item in completed:
        projector.project(item)
    snap = recorder.snapshot()
    assert snap.histogram(
        "runtime_model_duration_seconds",
        {"model_profile": "local", "status": "SUCCEEDED"},
    ) == (0.125,)
    assert snap.histogram(
        "runtime_tool_duration_seconds",
        {"status": "SUCCEEDED", "tool_name": "known"},
    ) == (0.25,)
    assert snap.histogram(
        "runtime_retrieval_duration_seconds",
        {"status": "SUCCEEDED", "retrieval_strategy": "BASELINE"},
    ) == (0.375,)
    assert snap.histogram(
        "runtime_step_duration_seconds",
        {
            "execution_kind": "unknown",
            "output_policy": "unknown",
            "status": "SUCCEEDED",
        },
    ) == (0.5,)
    assert snap.histogram(
        "runtime_run_duration_seconds", {"status": "SUCCEEDED"}
    ) == (0.625,)
    assert projector.correlation_state_size == 0


def test_component_outcome_and_run_signal_have_distinct_unique_owners():
    recorder = InMemoryMetricsRecorder()
    projector = RuntimeMetricsProjector(recorder)
    events = [
        journal_record(
            RuntimeEventType.MODEL_COMPLETED,
            ModelCompletedPayload(
                "local",
                0,
                0,
                False,
                "MODEL_PROVIDER_TIMEOUT",
                duration_ms=10,
            ),
            1,
        ),
        journal_record(
            RuntimeEventType.TOOL_COMPLETED,
            ToolCompletedPayload(
                "dynamic",
                False,
                "TOOL_TIMEOUT",
                duration_ms=20,
                status="TIMED_OUT",
            ),
            2,
        ),
        journal_record(
            RuntimeEventType.RETRIEVAL_COMPLETED,
            RetrievalCompletedPayload(
                "retrieval-a",
                "TIMED_OUT",
                30,
                0,
                0,
                True,
                RetrievalBudgetPayload(),
                safe_error_code="RETRIEVAL_TIMEOUT",
            ),
            3,
        ),
        journal_record(
            RuntimeEventType.TIMEOUT, TimeoutPayload("run"), 4
        ),
        # 方案 B：非 Run 专用信号不拥有组件 Counter。
        journal_record(
            RuntimeEventType.TIMEOUT, TimeoutPayload("model"), 5
        ),
    ]
    for item in events:
        projector.project(item)
    snap = recorder.snapshot()
    for component, status in (
        ("model", "FAILED"),
        ("tool", "TIMED_OUT"),
        ("retrieval", "TIMED_OUT"),
        ("run", "TIMED_OUT"),
    ):
        assert snap.counter(
            "runtime_timeouts_total",
            {"component": component, "status": status},
        ) == 1
    assert sum(snap.counters.values()) == 4


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_completed_duration_rejects_invalid_values(value):
    with pytest.raises((TypeError, ValueError)):
        ModelCompletedPayload(
            "local", 0, 0, True, duration_ms=value
        )


def test_gauge_collect_failure_does_not_hide_accumulated_metrics():
    class BrokenGaugeProvider:
        def snapshot(self):
            raise RuntimeError("gauge unavailable")

    recorder = InMemoryMetricsRecorder()
    recorder.increment_counter("runtime_runs_started_total")
    snapshot = recorder.snapshot(gauge_provider=BrokenGaugeProvider())
    assert snapshot.counter("runtime_runs_started_total") == 1


def test_budget_and_cancellation_component_completed_ownership():
    recorder = InMemoryMetricsRecorder()
    projector = RuntimeMetricsProjector(recorder)
    records = [
        journal_record(
            RuntimeEventType.MODEL_COMPLETED,
            ModelCompletedPayload(
                "local",
                0,
                0,
                False,
                "BUDGET_EXHAUSTED",
                duration_ms=1,
            ),
            1,
        ),
        # 方案 B 下相同组件的专用事件不再增加 Counter。
        journal_record(
            RuntimeEventType.BUDGET_EXHAUSTED,
            BudgetExhaustedPayload("model", "model_calls"),
            2,
        ),
        journal_record(
            RuntimeEventType.TOOL_COMPLETED,
            ToolCompletedPayload(
                "dynamic",
                False,
                "TOOL_CANCELLED",
                duration_ms=1,
                status="CANCELLED",
            ),
            3,
        ),
        journal_record(
            RuntimeEventType.CANCELLATION,
            CancellationPayload("TOOL_CANCELLED", "tool"),
            4,
        ),
    ]
    for item in records:
        projector.project(item)
    snapshot = recorder.snapshot()
    assert snapshot.counter(
        "runtime_budget_exhaustions_total",
        {
            "component": "model",
            "budget_dimension": "unknown",
            "status": "FAILED",
        },
    ) == 1
    assert snapshot.counter(
        "runtime_cancellations_total",
        {
            "component": "tool",
            "cancellation_reason": "TOOL_CANCELLED",
            "status": "CANCELLED",
        },
    ) == 1
