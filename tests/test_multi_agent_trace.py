from __future__ import annotations

import pytest

from core.runtime import (
    CoordinatedRuntimeFactory,
    InMemoryMetricsRecorder,
    InMemorySpanRecorder,
    RunStatus,
    StopReason,
)
from core.runtime.trace_contract import (
    RUNTIME_FINAL_MEMORY_COMMIT_SPAN,
    RUNTIME_OUTPUT_DELIVERY_SPAN,
    RUNTIME_PLANNING_SPAN,
    RUNTIME_RUN_SPAN,
    RUNTIME_STEP_SPAN,
    RUNTIME_SYNTHESIS_SPAN,
)
from tests._wp3_fixtures import (
    Wp3RecordingRouter,
    make_wp3_services,
    shape3_planning_json,
)


@pytest.mark.asyncio
async def test_shape3_span_topology_parents_and_siblings() -> None:
    recorder = InMemorySpanRecorder()
    metrics = InMemoryMetricsRecorder()
    services = make_wp3_services(
        span_recorder=recorder,
        runtime_metrics_recorder=metrics,
    )
    router = Wp3RecordingRouter(shape3_planning_json())
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        "coordinate two reviews",
    )
    result = await scope.execute()
    assert result.status is RunStatus.SUCCEEDED
    assert result.stop_reason is StopReason.COMPLETED

    records = recorder.snapshot()
    run_spans = [
        record for record in records
        if record.operation == RUNTIME_RUN_SPAN
    ]
    planning_spans = [
        record for record in records
        if record.operation == RUNTIME_PLANNING_SPAN
    ]
    step_spans = [
        record for record in records
        if record.operation == RUNTIME_STEP_SPAN
    ]
    synthesis_spans = [
        record for record in records
        if record.operation == RUNTIME_SYNTHESIS_SPAN
    ]
    delivery_spans = [
        record for record in records
        if record.operation == RUNTIME_OUTPUT_DELIVERY_SPAN
    ]
    memory_spans = [
        record for record in records
        if record.operation == RUNTIME_FINAL_MEMORY_COMMIT_SPAN
    ]

    assert len(run_spans) == 1
    assert len(planning_spans) == 1
    assert len(step_spans) == 3
    assert len(synthesis_spans) == 1
    assert len(delivery_spans) == 1
    assert len(memory_spans) == 1

    root = run_spans[0]
    planner = planning_spans[0]
    assert planner.parent_span_id == root.span_id
    specialist_steps = sorted(
        (record for record in step_spans if record.step_id != "synthesis"),
        key=lambda item: item.step_id,
    )
    synthesis_step = next(
        record for record in step_spans if record.step_id == "synthesis"
    )
    assert len(specialist_steps) == 2
    # 并行 specialist span 是 sibling：同一 parent，互不嵌套。
    assert specialist_steps[0].parent_span_id == root.span_id
    assert specialist_steps[1].parent_span_id == root.span_id
    assert specialist_steps[0].span_id != specialist_steps[1].span_id

    synthesis_span = synthesis_spans[0]
    assert synthesis_span.parent_span_id == synthesis_step.span_id
    # synthesis span 在依赖 specialist span 结束后开始。
    assert synthesis_span.started_at >= max(
        item.completed_at for item in specialist_steps
    )

    delivery_span = delivery_spans[0]
    assert delivery_span.parent_span_id == synthesis_step.span_id
    assert delivery_span.attributes["delivery_status"] == "DELIVERED"
    memory_span = memory_spans[0]
    assert memory_span.parent_span_id == synthesis_step.span_id
    assert memory_span.attributes["user_write_status"] == "WRITTEN"
    assert memory_span.attributes["assistant_write_status"] == "WRITTEN"
    assert memory_span.attributes["transaction_used"] is True

    rendered = repr(records)
    for forbidden in (
        "coordinate two reviews",
        "result-code_expert",
        "result-knowledge_expert",
        "result-synthesis_agent",
    ):
        assert forbidden not in rendered
    await scope.close()


@pytest.mark.asyncio
async def test_span_termination_is_unique_for_every_span() -> None:
    recorder = InMemorySpanRecorder()
    services = make_wp3_services(span_recorder=recorder)
    router = Wp3RecordingRouter(shape3_planning_json())
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("core_router", "termination")
    result = await scope.execute()
    assert result.status is RunStatus.SUCCEEDED
    for record in recorder.snapshot():
        assert record.completed_at is not None
        assert record.duration_ms is not None
    assert recorder.health_snapshot().active_span_count == 0
    await scope.close()
