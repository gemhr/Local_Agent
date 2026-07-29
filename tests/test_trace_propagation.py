import asyncio
from core.runtime.tracing import InMemorySpanRecorder, activate_span, current_trace_context

def test_context_reset():
    r=InMemorySpanRecorder(); h=r.start_span(trace_id='trace',run_id='run',component='runtime',operation='run')
    with activate_span(h): assert current_trace_context() == h.context
    assert current_trace_context() is None

def test_async_tasks_are_isolated():
    async def main():
        r=InMemorySpanRecorder(); root=r.start_span(trace_id='trace',run_id='run',component='runtime',operation='run')
        async def one(n):
            h=r.start_span(trace_id='trace',run_id='run',component='step',operation='execute',step_id=n,parent_context=root.context)
            with activate_span(h):
                await asyncio.sleep(0); return current_trace_context().span_id
        assert len(set(await asyncio.gather(one('a'),one('b')))) == 2
    asyncio.run(main())
