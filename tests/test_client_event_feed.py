import pytest

from core.runtime.client_event_feed import InMemoryClientEventFeed
from core.runtime.event_channel import RuntimeEventChannel
from core.runtime.event_emitter import RunEventEmitter
from core.runtime.event_journal_store import InMemoryRunEventJournal
from core.runtime.events import OutputDeltaPayload, RuntimeEventType


@pytest.mark.asyncio
async def test_client_feed_persists_output_and_replays_after_cursor():
    feed = InMemoryClientEventFeed()
    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(8, run_id="run-a", journal=journal, client_event_feed=feed)
    emitter = RunEventEmitter(run_id="run-a", trace_id="trace-a", channel=channel)
    await emitter.emit(RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload("hello"), component="test")
    await emitter.emit(RuntimeEventType.OUTPUT_DELTA, OutputDeltaPayload(" world"), component="test")

    replay = await feed.read_after("run-a", 1)
    assert [(item.cursor, item.event_type, item.payload) for item in replay] == [
        (2, "output.delta", {"text": " world"})
    ]
    assert journal.read_after("run-a", 0, 10)[0].safe_payload["text_digest"]
