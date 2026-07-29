import pytest
from core.runtime import InMemoryMetricsRecorder
from core.runtime.tracing import InMemorySpanRecorder, SpanStatus, TraceContext

def test_contract_and_attribute_policy():
    recorder=InMemorySpanRecorder(); handle=recorder.start_span(trace_id='a'*32,run_id='run',component='runtime',operation='run')
    handle.set_safe_attribute('input_count', 2)
    with pytest.raises(ValueError): handle.set_safe_attribute('prompt','secret')
    first=handle.end_ok(); second=handle.end_error()
    assert first is second and first.status is SpanStatus.OK and first.duration_ms >= 0

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
