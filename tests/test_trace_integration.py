from core.runtime.events import RuntimeEventDraft, RuntimeEventType, RunStartedPayload, RuntimeEvent
from core.runtime.event_journal import JournalRecord
from core.runtime.structured_logging import InMemoryStructuredRuntimeLogger, StructuredLogProjector
from core.runtime.tracing import OpenTelemetryCompatibleSpanAdapter

def test_event_journal_log_correlation():
    event=RuntimeEvent.from_draft(RuntimeEventDraft(run_id='run',trace_id='trace',event_type=RuntimeEventType.RUN_STARTED,component='runtime',payload=RunStartedPayload('RUNNING'),span_id='span'),1)
    journal=JournalRecord.from_event(event); logger=InMemoryStructuredRuntimeLogger(); log=StructuredLogProjector(logger).project(journal)
    assert event.span_id == journal.span_id == log.span_id == 'span'

def test_otel_mapping():
    r=OpenTelemetryCompatibleSpanAdapter(); h=r.start_span(trace_id='trace',run_id='run',component='runtime',operation='run'); h.end_ok()
    assert r.export_snapshot()[0]['name'] == 'run'
