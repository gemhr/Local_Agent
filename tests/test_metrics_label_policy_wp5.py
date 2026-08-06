from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from core.runtime import (
    InMemoryMetricsRecorder,
    JournalRecord,
    MetricDescriptor,
    MetricLabelPolicy,
    MetricType,
    PlanCreatedPayload,
    RunCompletedPayload,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeMetricsProjector,
    StepCompletedPayload,
    StepStartedPayload,
)


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def journal_record(event_type, payload, sequence, *, step_id=None):
    return JournalRecord.from_event(
        RuntimeEvent(
            schema_version=2,
            event_id=uuid4().hex,
            run_id="run-a",
            trace_id="trace-a",
            sequence=sequence,
            event_type=event_type,
            emitted_at=NOW,
            component="test",
            payload=payload,
            step_id=step_id,
            step_sequence=1 if step_id else None,
        )
    )


def test_delivery_and_memory_metric_descriptors_are_bounded():
    recorder = InMemoryMetricsRecorder()
    delivery = recorder.descriptors["runtime_output_delivery_total"]
    assert delivery.required_labels == frozenset({"status", "error_code"})
    assert {
        "OK",
        "FINAL_OUTPUT_DELIVERY_FAILED",
        "FINAL_OUTPUT_DELIVERY_UNKNOWN",
        "OUTPUT_GATE_DUPLICATE_ATTEMPT",
    } <= delivery.bounded_values["error_code"]

    memory = recorder.descriptors["runtime_final_memory_commit_total"]
    assert memory.bounded_values["status"] == {
        "SUCCEEDED",
        "FAILED",
        "NOT_ATTEMPTED",
        "unknown",
    }


def test_high_cardinality_labels_are_rejected():
    recorder = InMemoryMetricsRecorder()
    with pytest.raises(ValueError):
        recorder.increment_counter(
            "runtime_step_total",
            labels={
                "execution_kind": "AGENT",
                "output_policy": "INTERNAL",
                "status": "SUCCEEDED",
                "step_id": "step-1",
            },
        )
    with pytest.raises(ValueError):
        recorder.increment_counter(
            "runtime_multi_agent_runs_total",
            labels={"shape": "3", "status": "SUCCEEDED", "run_id": "r"},
        )


def test_agent_id_is_controlled_label_with_allowlist():
    policy = MetricLabelPolicy(
        agent_id_allowlist=frozenset(
            {"core_router", "knowledge_expert", "code_expert"}
        )
    )
    descriptor = MetricDescriptor(
        name="runtime_step_total",
        type=MetricType.COUNTER,
        description="step",
        unit="steps",
        allowed_labels=frozenset(
            {"execution_kind", "output_policy", "status", "agent_id"}
        ),
        required_labels=frozenset(
            {"execution_kind", "output_policy", "status", "agent_id"}
        ),
    )
    assert policy.normalize(
        {
            "execution_kind": "AGENT",
            "output_policy": "INTERNAL",
            "status": "SUCCEEDED",
            "agent_id": "code_expert",
        },
        descriptor,
    ) == (
        ("agent_id", "code_expert"),
        ("execution_kind", "AGENT"),
        ("output_policy", "INTERNAL"),
        ("status", "SUCCEEDED"),
    )
    # 未在固定 Registry allowlist 的 agent 聚合为 other，保持低基数。
    assert policy.normalize(
        {
            "execution_kind": "AGENT",
            "output_policy": "INTERNAL",
            "status": "SUCCEEDED",
            "agent_id": "injected-random-agent",
        },
        descriptor,
    )[0] == ("agent_id", "other")


def test_step_and_shape_metrics_projected_from_journal():
    recorder = InMemoryMetricsRecorder()
    projector = RuntimeMetricsProjector(recorder)
    projector.project(
        journal_record(
            RuntimeEventType.PLAN_CREATED,
            PlanCreatedPayload("p", 1, "a" * 64, 3, "MODEL", shape="3"),
            1,
        )
    )
    projector.project(
        journal_record(
            RuntimeEventType.STEP_STARTED,
            StepStartedPayload(
                "RUNNING",
                agent_id="code_expert",
                execution_kind="AGENT",
                output_policy="INTERNAL",
                dependency_count=0,
            ),
            2,
            step_id="code",
        )
    )
    projector.project(
        journal_record(
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload(
                "SUCCEEDED",
                duration_ms=100,
                result_char_count=10,
                delivery_status="DELIVERED",
                delivery_duration_ms=5,
                execution_kind="AGENT",
                output_policy="INTERNAL",
            ),
            3,
            step_id="code",
        )
    )
    projector.project(
        journal_record(
            RuntimeEventType.STEP_STARTED,
            StepStartedPayload(
                "RUNNING",
                agent_id="synthesis_agent",
                execution_kind="SYNTHESIS",
                output_policy="FINAL_SYNTHESIS",
                dependency_count=1,
            ),
            4,
            step_id="synthesis",
        )
    )
    projector.project(
        journal_record(
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload(
                "SUCCEEDED",
                duration_ms=50,
                result_char_count=20,
                delivery_status="DELIVERED",
                delivery_duration_ms=3,
                execution_kind="SYNTHESIS",
                output_policy="FINAL_SYNTHESIS",
            ),
            5,
            step_id="synthesis",
        )
    )
    projector.project(
        journal_record(
            RuntimeEventType.RUN_COMPLETED,
            RunCompletedPayload(
                "SUCCEEDED",
                "COMPLETED",
                duration_ms=200,
                delivery_status="DELIVERED",
                final_step_status="SUCCEEDED",
                memory_commit_status="SUCCEEDED",
                shape="3",
            ),
            6,
        )
    )
    snap = recorder.snapshot()
    assert snap.counter(
        "runtime_step_total",
        {
            "execution_kind": "AGENT",
            "output_policy": "INTERNAL",
            "status": "SUCCEEDED",
        },
    ) == 1
    assert snap.counter(
        "runtime_synthesis_total", {"status": "SUCCEEDED"}
    ) == 1
    assert snap.histogram(
        "runtime_step_duration_seconds",
        {
            "execution_kind": "SYNTHESIS",
            "output_policy": "FINAL_SYNTHESIS",
            "status": "SUCCEEDED",
        },
    ) == (0.05,)
    assert snap.counter(
        "runtime_multi_agent_runs_total",
        {"shape": "3", "status": "SUCCEEDED"},
    ) == 1
    assert snap.histogram(
        "runtime_specialist_count", {"shape": "3"}
    ) == (2.0,)


def test_legacy_step_event_maps_to_unknown_labels():
    recorder = InMemoryMetricsRecorder()
    projector = RuntimeMetricsProjector(recorder)
    projector.project(
        journal_record(
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload("SUCCEEDED", duration_ms=7),
            1,
            step_id="answer",
        )
    )
    snap = recorder.snapshot()
    assert snap.counter(
        "runtime_step_total",
        {
            "execution_kind": "unknown",
            "output_policy": "unknown",
            "status": "SUCCEEDED",
        },
    ) == 1


def test_executor_p2_metrics_remain_available():
    recorder = InMemoryMetricsRecorder()
    recorder.set_gauge("runtime_blocking_executor_pending", 2.0)
    recorder.observe_histogram(
        "runtime_blocking_executor_wait_seconds", 0.25
    )
    snap = recorder.snapshot()
    assert snap.gauge("runtime_blocking_executor_pending") == 2.0
    assert snap.histogram("runtime_blocking_executor_wait_seconds") == (
        0.25,
    )
