import asyncio
import unittest

from core.runtime import (
    CancellationReason, CancellationSource, CancellableRetrySleeper,
    RunDeadlineExceededError,
)


class BlockingSleeper:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    async def sleep(self, seconds):
        self.calls.append(seconds)
        self.started.set()
        await self.release.wait()


class RetrySleeperTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_cancellation_interrupts_backoff(self):
        source = CancellationSource()
        sleeper = BlockingSleeper()
        task = asyncio.create_task(
            CancellableRetrySleeper(sleeper).sleep(
                10, cancellation_token=source.token, remaining_seconds=lambda: None
            )
        )
        await sleeper.started.wait()
        source.cancel(CancellationReason.USER_CANCELLED)
        with self.assertRaisesRegex(Exception, "USER_CANCELLED"):
            await task
        self.assertEqual(sleeper.calls, [10])

    async def test_deadline_wins_when_delay_exceeds_remaining(self):
        source = CancellationSource()
        with self.assertRaises(RunDeadlineExceededError):
            await CancellableRetrySleeper().sleep(
                1, cancellation_token=source.token, remaining_seconds=lambda: 0
            )

    async def test_task_cancellation_propagates(self):
        source = CancellationSource()
        sleeper = BlockingSleeper()
        task = asyncio.create_task(
            CancellableRetrySleeper(sleeper).sleep(
                10, cancellation_token=source.token, remaining_seconds=lambda: None
            )
        )
        await sleeper.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
