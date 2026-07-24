import asyncio
import unittest

from core.runtime import (
    CancellationReason,
    CancellationSource,
    EventChannelClosedError,
    EventChannelState,
    RunCompletedPayload,
    RunStartedPayload,
    RuntimeEventChannel,
    RuntimeEventDraft,
    RuntimeEventType,
)
from core.runtime.cancellation import RunCancelledError


def draft(index: int = 0) -> RuntimeEventDraft:
    return RuntimeEventDraft(
        "run-a",
        "trace-a",
        RuntimeEventType.RUN_STARTED,
        "test",
        RunStartedPayload(f"RUNNING-{index}"),
    )


class RuntimeEventChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_is_positive_bounded_and_rejects_bool(self):
        for value in (0, -1, True):
            with self.assertRaises(ValueError):
                RuntimeEventChannel(value, run_id="run-a")
        channel = RuntimeEventChannel(2, run_id="run-a")
        await channel.publish(draft(1))
        await channel.publish(draft(2))
        self.assertEqual(channel.buffered_count, 2)

    async def test_full_queue_blocks_and_consumer_releases_producer(self):
        channel = RuntimeEventChannel(1, run_id="run-a")
        first = await channel.publish(draft(1))
        blocked = asyncio.create_task(channel.publish(draft(2)))
        await asyncio.sleep(0)
        self.assertFalse(blocked.done())
        self.assertEqual(channel.buffered_count, 1)
        iterator = channel.__aiter__()
        self.assertEqual((await anext(iterator)).event_id, first.event_id)
        second = await asyncio.wait_for(blocked, 0.2)
        self.assertEqual(second.sequence, 2)
        self.assertEqual((await anext(iterator)).event_id, second.event_id)
        await channel.close()
        with self.assertRaises(StopAsyncIteration):
            await anext(iterator)

    async def test_no_event_drop_with_slow_consumer(self):
        channel = RuntimeEventChannel(2, run_id="run-a")

        async def produce():
            for index in range(8):
                await channel.publish(draft(index))
            await channel.close()

        producer = asyncio.create_task(produce())
        sequences = []
        async for event in channel:
            sequences.append(event.sequence)
            await asyncio.sleep(0)
        await producer
        self.assertEqual(sequences, list(range(1, 9)))

    async def test_close_is_idempotent_drains_and_rejects_publish(self):
        channel = RuntimeEventChannel(2, run_id="run-a")
        event = await channel.publish(draft())
        close_task = asyncio.create_task(channel.close())
        iterator = channel.__aiter__()
        self.assertEqual((await anext(iterator)).event_id, event.event_id)
        await close_task
        await channel.close()
        with self.assertRaises(StopAsyncIteration):
            await anext(iterator)
        self.assertEqual(channel.state, EventChannelState.CLOSED)
        with self.assertRaises(EventChannelClosedError):
            await channel.publish(draft(2))

    async def test_terminal_event_precedes_end_sentinel(self):
        channel = RuntimeEventChannel(2, run_id="run-a")
        terminal = await channel.publish(
            RuntimeEventDraft(
                "run-a",
                "trace-a",
                RuntimeEventType.RUN_COMPLETED,
                "test",
                RunCompletedPayload("SUCCEEDED", "COMPLETED"),
            )
        )
        await channel.close()
        consumed = [event async for event in channel]
        self.assertEqual(consumed, [terminal])

    async def test_abort_discards_buffer_and_unblocks_publisher(self):
        channel = RuntimeEventChannel(1, run_id="run-a")
        await channel.publish(draft(1))
        blocked = asyncio.create_task(channel.publish(draft(2)))
        await asyncio.sleep(0)
        await channel.abort()
        with self.assertRaises(EventChannelClosedError):
            await blocked
        self.assertEqual(channel.state, EventChannelState.ABORTED)
        self.assertEqual(channel.buffered_count, 0)

    async def test_task_cancellation_unblocks_publisher(self):
        channel = RuntimeEventChannel(1, run_id="run-a")
        await channel.publish(draft(1))
        blocked = asyncio.create_task(channel.publish(draft(2)))
        await asyncio.sleep(0)
        blocked.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await blocked
        self.assertEqual(channel.buffered_count, 1)
        iterator = channel.__aiter__()
        await anext(iterator)
        await asyncio.sleep(0)
        self.assertEqual(channel.buffered_count, 0)
        await channel.close()

    async def test_run_cancellation_unblocks_publisher(self):
        source = CancellationSource()
        channel = RuntimeEventChannel(
            1, run_id="run-a", cancellation_token=source.token
        )
        await channel.publish(draft(1))
        blocked = asyncio.create_task(channel.publish(draft(2)))
        await asyncio.sleep(0)
        source.cancel(CancellationReason.USER_CANCELLED)
        with self.assertRaises(RunCancelledError):
            await asyncio.wait_for(blocked, 0.3)
        self.assertEqual(channel.buffered_count, 1)

    async def test_close_waits_for_accepted_blocked_publisher_before_sentinel(self):
        channel = RuntimeEventChannel(1, run_id="run-a")
        first = await channel.publish(draft(1))
        second_task = asyncio.create_task(channel.publish(draft(2)))
        await asyncio.sleep(0)
        self.assertFalse(second_task.done())

        close_task = asyncio.create_task(channel.close())
        await asyncio.sleep(0)
        self.assertEqual(channel.state, EventChannelState.CLOSING)
        with self.assertRaises(EventChannelClosedError):
            await channel.publish(draft(3))

        iterator = channel.__aiter__()
        self.assertEqual((await anext(iterator)).event_id, first.event_id)
        second = await asyncio.wait_for(second_task, 0.2)
        self.assertEqual((await anext(iterator)).event_id, second.event_id)
        await asyncio.wait_for(close_task, 0.2)
        # Sentinel 已入队，但不是用户可见 buffered event。
        self.assertEqual(channel.buffered_count, 0)
        with self.assertRaises(StopAsyncIteration):
            await anext(iterator)
        self.assertEqual(channel.buffered_count, 0)

    async def test_cancelled_blocked_publisher_releases_close_barrier(self):
        channel = RuntimeEventChannel(1, run_id="run-a")
        first = await channel.publish(draft(1))
        blocked = asyncio.create_task(channel.publish(draft(2)))
        await asyncio.sleep(0)
        close_task = asyncio.create_task(channel.close())
        await asyncio.sleep(0)

        blocked.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await blocked
        iterator = channel.__aiter__()
        self.assertEqual((await anext(iterator)).event_id, first.event_id)
        await asyncio.wait_for(close_task, 0.2)
        with self.assertRaises(StopAsyncIteration):
            await anext(iterator)

    async def test_abort_while_close_waits_releases_every_task(self):
        channel = RuntimeEventChannel(1, run_id="run-a")
        await channel.publish(draft(1))
        blocked = asyncio.create_task(channel.publish(draft(2)))
        await asyncio.sleep(0)
        close_task = asyncio.create_task(channel.close())
        await asyncio.sleep(0)

        await channel.abort()
        with self.assertRaises(EventChannelClosedError):
            await asyncio.wait_for(blocked, 0.2)
        await asyncio.wait_for(close_task, 0.2)
        self.assertEqual(channel.state, EventChannelState.ABORTED)
        self.assertEqual(channel.buffered_count, 0)

    async def test_concurrent_close_is_idempotent_and_sentinel_is_invisible(self):
        channel = RuntimeEventChannel(1, run_id="run-a")
        await asyncio.gather(*(channel.close() for _ in range(8)))
        self.assertEqual(channel.state, EventChannelState.CLOSED)
        self.assertEqual(channel.buffered_count, 0)
        iterator = channel.__aiter__()
        with self.assertRaises(StopAsyncIteration):
            await anext(iterator)
        self.assertEqual(channel.buffered_count, 0)

    async def test_closed_state_wins_over_late_abort(self):
        channel = RuntimeEventChannel(1, run_id="run-a")
        await channel.close()
        await channel.abort()
        self.assertEqual(channel.state, EventChannelState.CLOSED)
