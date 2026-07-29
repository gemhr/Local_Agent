import asyncio
from datetime import UTC, datetime

import pytest

from core.runtime.event_channel import JournalWatermarkError, RuntimeEventChannel
from core.runtime.event_journal import JournalAppendStatus
from core.runtime.events import (
    OutputDeltaPayload,
    RuntimeEventDraft,
    RuntimeEventType,
)


class _Journal:
    def __init__(self, sequence=0):
        self.sequence = sequence

    def append(self, event):
        self.sequence = event.sequence
        return JournalAppendStatus.APPENDED

    def last_sequence(self, run_id):
        return self.sequence or None


def _draft(text: str) -> RuntimeEventDraft:
    return RuntimeEventDraft(
        run_id="run",
        trace_id="trace",
        event_type=RuntimeEventType.OUTPUT_DELTA,
        component="test",
        payload=OutputDeltaPayload(text),
    )


@pytest.mark.asyncio
async def test_watermark_zero_normal_sequence_gap_and_mismatch():
    journal = _Journal()
    channel = RuntimeEventChannel(4, run_id="run", journal=journal)
    assert await channel.capture_journal_watermark() == 0
    await channel.publish(_draft("one"))
    assert await channel.capture_journal_watermark() == 1
    await channel.abort()
    assert await channel.capture_journal_watermark() == 1

    gap_journal = _Journal(7)
    gap_channel = RuntimeEventChannel(4, run_id="run", journal=gap_journal)
    assert await gap_channel.capture_journal_watermark() == 7

    journal.sequence = 9
    with pytest.raises(JournalWatermarkError):
        await channel.capture_journal_watermark()


@pytest.mark.asyncio
async def test_watermark_waits_for_in_flight_publication():
    journal = _Journal()
    channel = RuntimeEventChannel(1, run_id="run", journal=journal)
    await channel.publish(_draft("one"))
    blocked = asyncio.create_task(channel.publish(_draft("two")))
    await asyncio.sleep(0)
    assert channel.publications_in_flight == 1
    capture = asyncio.create_task(channel.capture_journal_watermark())
    await asyncio.sleep(0)
    assert not capture.done()
    iterator = channel.__aiter__()
    assert (await anext(iterator)).sequence == 1
    await blocked
    assert await capture == 2
