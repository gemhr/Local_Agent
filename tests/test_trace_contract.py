import pytest
from core.runtime.tracing import InMemorySpanRecorder, SpanStatus, TraceContext

def test_contract_and_attribute_policy():
    recorder=InMemorySpanRecorder(); handle=recorder.start_span(trace_id='a'*32,run_id='run',component='runtime',operation='run')
    handle.set_safe_attribute('input_count', 2)
    with pytest.raises(ValueError): handle.set_safe_attribute('prompt','secret')
    first=handle.end_ok(); second=handle.end_error()
    assert first is second and first.status is SpanStatus.OK and first.duration_ms >= 0

def test_context_validation():
    with pytest.raises(ValueError): TraceContext('', 'span', None, 'run')
