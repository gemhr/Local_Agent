from core.runtime.tracing import InMemorySpanRecorder, activate_span

def test_parent_child_hierarchy():
    r=InMemorySpanRecorder(); root=r.start_span(trace_id='trace',run_id='run',component='runtime',operation='run')
    with activate_span(root):
        child=r.start_span(trace_id='trace',run_id='run',component='planner',operation='plan'); child.end_ok()
    records=r.snapshot(); assert records[0].parent_span_id == root.context.span_id
