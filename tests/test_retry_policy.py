import unittest

from core.runtime import (
    JitterMode, ModelFailureCategory, OperationIdempotency,
    RateLimitRecoveryMode, RetryExecutor, RetryPolicy, RetryableOperationKind,
    retry_allowed_by_idempotency,
)


class FixedRandom:
    def uniform(self, lower, upper):
        return upper


class RetryPolicyTests(unittest.TestCase):
    def test_backoff_jitter_and_cap_are_deterministic(self):
        policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=4, backoff_multiplier=2)
        self.assertEqual(policy.raw_delay(1), 1)
        self.assertEqual(policy.raw_delay(99), 4)
        self.assertEqual(RetryPolicy(base_delay_seconds=2, max_delay_seconds=2, jitter_mode=JitterMode.FULL).delay(1, FixedRandom()), 2)
        self.assertEqual(RetryPolicy(base_delay_seconds=2, max_delay_seconds=2, jitter_mode=JitterMode.EQUAL).delay(1, FixedRandom()), 2)

    def test_idempotency_contract(self):
        self.assertTrue(retry_allowed_by_idempotency(RetryableOperationKind.TOOL, OperationIdempotency.READ_ONLY))
        self.assertTrue(retry_allowed_by_idempotency(RetryableOperationKind.RAG, OperationIdempotency.IDEMPOTENT_WITH_KEY, idempotency_key="stable"))
        self.assertFalse(retry_allowed_by_idempotency(RetryableOperationKind.TOOL, OperationIdempotency.IDEMPOTENT_WITH_KEY))
        self.assertFalse(retry_allowed_by_idempotency(RetryableOperationKind.TOOL, OperationIdempotency.NON_IDEMPOTENT))

    def test_rate_limit_fallback_first_does_not_wait(self):
        decision = RetryExecutor().decide(category=ModelFailureCategory.RATE_LIMITED, retry_index=1, output_started=False, remaining_seconds=None, has_fallback=True)
        self.assertFalse(decision.should_retry)
        self.assertEqual(decision.reason_code, "RETRY_NOT_ALLOWED")


class RetryBudgetAtomicityTests(unittest.TestCase):
    def test_two_concurrent_retry_reservations_allow_only_one(self):
        from concurrent.futures import ThreadPoolExecutor
        from core.runtime import BudgetExceededError, BudgetLedger, BudgetUsage, RunBudget
        ledger = BudgetLedger(RunBudget(max_retries=1, max_model_calls=2))
        usage = BudgetUsage(model_calls=1, retries=1)
        def reserve():
            try:
                return ledger.reserve(usage, reservation_type="retry")
            except BudgetExceededError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            reservations = list(pool.map(lambda _: reserve(), range(2)))
        self.assertEqual(sum(item is not None for item in reservations), 1)
        for item in reservations:
            if item is not None:
                ledger.release(item)
