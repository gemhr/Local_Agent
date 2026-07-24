import asyncio
import unittest

from core.runtime import (
    BudgetLedger, BudgetUsage, CancellationReason, CancellationSource,
    CancellableRetrySleeper, ModelFailureCategory, RetryExecutor, RetryPolicy,
    RunBudget, RunDeadlineExceededError,
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


class BoundCancellableSleeper:
    def __init__(self, sleeper, source):
        self._sleeper = CancellableRetrySleeper(sleeper)
        self._source = source

    async def sleep(self, seconds):
        await self._sleeper.sleep(
            seconds,
            cancellation_token=self._source.token,
            remaining_seconds=lambda: None,
        )


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

    async def test_backoff_cancellation_starts_no_adapter_or_retry_budget(self):
        source = CancellationSource()
        sleeper = BlockingSleeper()
        executor = RetryExecutor(
            RetryPolicy(
                max_attempts=2,
                base_delay_seconds=10,
                max_delay_seconds=10,
            ),
            sleeper=BoundCancellableSleeper(sleeper, source),
        )
        ledger = BudgetLedger(RunBudget(max_model_calls=2, max_retries=1))
        adapter_calls = []

        async def attempt(retry_index):
            usage = BudgetUsage(
                model_calls=1,
                retries=1 if retry_index > 0 else 0,
            )
            reservation = ledger.reserve(usage, reservation_type="retry_test")
            adapter_calls.append(retry_index)
            ledger.commit(reservation)
            raise RuntimeError("transient")

        task = asyncio.create_task(
            executor.execute_async(
                attempt,
                category_of=lambda _: (
                    ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE
                ),
                should_retry=lambda category, retry_index: executor.decide(
                    category=category,
                    retry_index=retry_index,
                    output_started=False,
                    remaining_seconds=None,
                ),
                raise_if_cancelled=source.token.raise_if_cancelled,
            )
        )
        await sleeper.started.wait()
        source.cancel(CancellationReason.USER_CANCELLED)
        with self.assertRaisesRegex(Exception, "USER_CANCELLED"):
            await task

        snapshot = ledger.snapshot()
        self.assertEqual(adapter_calls, [0])
        self.assertEqual(snapshot.committed_usage.model_calls, 1)
        self.assertEqual(snapshot.committed_usage.retries, 0)
        self.assertEqual(snapshot.active_reservation_count, 0)
