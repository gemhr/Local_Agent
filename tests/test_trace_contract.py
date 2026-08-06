import pytest
from core.runtime import InMemoryMetricsRecorder
from core.runtime.trace_contract import (
    DELIVERY_ATTRIBUTE_KEYS,
    MEMORY_ATTRIBUTE_KEYS,
    PLANNING_ATTRIBUTE_KEYS,
    RUN_ATTRIBUTE_KEYS,
    RUNTIME_FINAL_MEMORY_COMMIT_SPAN,
    RUNTIME_OUTPUT_DELIVERY_SPAN,
    RUNTIME_PLANNING_SPAN,
    RUNTIME_RUN_SPAN,
    RUNTIME_STEP_SPAN,
    RUNTIME_SYNTHESIS_SPAN,
    RUNTIME_TRACE_CONTRACT_VERSION,
    STEP_ATTRIBUTE_KEYS,
)
from core.runtime.tracing import InMemorySpanRecorder, SpanStatus, TraceContext

def test_contract_and_attribute_policy():
    recorder=InMemorySpanRecorder(); handle=recorder.start_span(trace_id='a'*32,run_id='run',component='runtime',operation='run')
    handle.set_safe_attribute('input_count', 2)
    with pytest.raises(ValueError): handle.set_safe_attribute('prompt','secret')
    first=handle.end_ok(); second=handle.end_error()
    assert first is second and first.status is SpanStatus.OK and first.duration_ms >= 0


def test_wp5_contract_span_names_and_safe_attribute_sets():
    assert RUNTIME_TRACE_CONTRACT_VERSION == 1
    assert RUNTIME_RUN_SPAN == "runtime.run"
    assert RUNTIME_PLANNING_SPAN == "runtime.planning"
    assert RUNTIME_STEP_SPAN == "runtime.step"
    assert RUNTIME_SYNTHESIS_SPAN == "runtime.synthesis"
    assert RUNTIME_OUTPUT_DELIVERY_SPAN == "runtime.output_delivery"
    assert (
        RUNTIME_FINAL_MEMORY_COMMIT_SPAN
        == "runtime.final_memory_commit"
    )
    assert {"plan_id", "plan_fingerprint", "step_count"} <= RUN_ATTRIBUTE_KEYS
    assert {"compiled_shape", "specialist_count"} <= PLANNING_ATTRIBUTE_KEYS
    assert {"execution_kind", "output_policy"} <= STEP_ATTRIBUTE_KEYS
    assert {"delivery_status", "gate_terminal_state"} <= DELIVERY_ATTRIBUTE_KEYS
    assert {"user_write_status", "transaction_used"} <= MEMORY_ATTRIBUTE_KEYS


def test_wp5_span_operations_accept_dotted_contract_names():
    recorder = InMemorySpanRecorder()
    handle = recorder.start_span(
        trace_id="trace",
        run_id="run",
        component="runtime",
        operation=RUNTIME_RUN_SPAN,
    )
    handle.set_safe_attribute("plan_id", "plan-1")
    handle.set_safe_attribute("plan_fingerprint", "a" * 64)
    handle.set_safe_attribute("step_count", 3)
    handle.end_ok()
    record = recorder.snapshot()[0]
    assert record.operation == "runtime.run"
    assert record.attributes["plan_id"] == "plan-1"


def test_wp5_delivery_and_memory_attributes_are_safe():
    recorder = InMemorySpanRecorder()
    handle = recorder.start_span(
        trace_id="trace",
        run_id="run",
        component="output_gate",
        operation=RUNTIME_OUTPUT_DELIVERY_SPAN,
        step_id="answer",
    )
    handle.set_safe_attribute("delivery_status", "DELIVERED")
    handle.set_safe_attribute("gate_terminal_state", "PUBLISHED")
    handle.set_safe_attribute("publish_attempt_count", 1)
    handle.set_safe_attribute("partially_persisted", False)
    handle.set_safe_attribute("output_char_count", 12)
    handle.end_ok()

    memory_handle = recorder.start_span(
        trace_id="trace",
        run_id="run",
        component="final_memory",
        operation=RUNTIME_FINAL_MEMORY_COMMIT_SPAN,
        step_id="answer",
    )
    memory_handle.set_safe_attribute("persist_enabled", True)
    memory_handle.set_safe_attribute("entry_agent_id", "core_router")
    memory_handle.set_safe_attribute("transaction_used", True)
    memory_handle.end_ok()
    assert len(recorder.snapshot()) == 2

def test_context_validation():
    with pytest.raises(ValueError): TraceContext('', 'span', None, 'run')

def test_health_snapshot_and_dropped_span_metric():
    metrics = InMemoryMetricsRecorder()
    recorder = InMemorySpanRecorder(metrics_recorder=metrics)
    handle = recorder.start_span(
        trace_id="trace", run_id="run", component="model_attempt", operation="attempt"
    )
    assert recorder.health_snapshot().active_span_count == 1
    handle.end_ok()
    handle.end_error()
    assert recorder.health_snapshot().active_span_count == 0
    assert recorder.health_snapshot().completed_span_count == 1
    recorder.close()
    dropped = recorder.start_span(
        trace_id="trace", run_id="run", component="model_attempt", operation="attempt"
    )
    assert dropped.context is None
    assert recorder.health_snapshot().dropped_span_count == 1
    assert metrics.snapshot().counter(
        "runtime_trace_dropped_spans_total",
        {"component": "model_attempt", "reason": "recorder_start_failed"},
    ) == 1

def test_close_ends_all_active_spans_and_local_health_survives_metrics_failure():
    class BrokenMetrics:
        def increment_counter(self, *args, **kwargs):
            raise RuntimeError("metrics unavailable")

    recorder = InMemorySpanRecorder(metrics_recorder=BrokenMetrics())
    recorder.start_span(
        trace_id="trace", run_id="run", component="tool_attempt", operation="attempt"
    )
    recorder.close()
    health = recorder.health_snapshot()
    assert health.active_span_count == 0
    assert health.dropped_span_count == 1
