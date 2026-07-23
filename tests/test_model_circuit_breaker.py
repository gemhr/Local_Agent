import threading
import unittest

from core.runtime import (
    CircuitOpenError,
    CircuitPermitStateError,
    ModelCircuitBreaker,
    ModelCircuitBreakerConfig,
    ModelCircuitBreakerRegistry,
    ModelCircuitState,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class ModelCircuitBreakerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.breaker = ModelCircuitBreaker(
            "provider-a",
            ModelCircuitBreakerConfig(
                failure_threshold=2,
                recovery_timeout_seconds=10,
                half_open_max_calls=1,
            ),
            clock=self.clock,
        )

    def test_closed_threshold_open_half_open_success_cycle(self) -> None:
        self.assertEqual(self.breaker.snapshot().state, ModelCircuitState.CLOSED)
        first = self.breaker.acquire_permission()
        first.record_failure()
        self.assertEqual(self.breaker.snapshot().state, ModelCircuitState.CLOSED)
        second = self.breaker.acquire_permission()
        second.record_failure()
        self.assertEqual(self.breaker.snapshot().state, ModelCircuitState.OPEN)
        with self.assertRaises(CircuitOpenError):
            self.breaker.acquire_permission()
        self.clock.advance(10)
        probe = self.breaker.acquire_permission()
        self.assertTrue(probe.half_open_probe)
        self.assertEqual(self.breaker.snapshot().state, ModelCircuitState.HALF_OPEN)
        probe.record_success()
        self.assertEqual(self.breaker.snapshot().state, ModelCircuitState.CLOSED)

    def test_half_open_probe_failure_reopens(self) -> None:
        for _ in range(2):
            self.breaker.acquire_permission().record_failure()
        self.clock.advance(10)
        self.breaker.acquire_permission().record_failure()
        self.assertEqual(self.breaker.snapshot().state, ModelCircuitState.OPEN)

    def test_abandon_releases_probe_without_provider_failure(self) -> None:
        for _ in range(2):
            self.breaker.acquire_permission().record_failure()
        self.clock.advance(10)
        probe = self.breaker.acquire_permission()
        probe.abandon()
        self.assertEqual(self.breaker.snapshot().state, ModelCircuitState.HALF_OPEN)
        replacement = self.breaker.acquire_permission()
        self.assertTrue(replacement.half_open_probe)

    def test_permit_cannot_be_completed_twice(self) -> None:
        permit = self.breaker.acquire_permission()
        permit.record_success()
        with self.assertRaises(CircuitPermitStateError):
            permit.abandon()

    def test_indeterminate_completion_is_not_abandon_or_provider_failure(
        self,
    ) -> None:
        self.breaker.acquire_permission().record_failure()
        permit = self.breaker.acquire_permission()
        permit.record_indeterminate()
        snapshot = self.breaker.snapshot()
        self.assertEqual(snapshot.state, ModelCircuitState.CLOSED)
        self.assertEqual(snapshot.consecutive_failures, 1)
        with self.assertRaises(CircuitPermitStateError):
            permit.record_success()

    def test_half_open_indeterminate_completion_reopens_without_probe_leak(
        self,
    ) -> None:
        for _ in range(2):
            self.breaker.acquire_permission().record_failure()
        self.clock.advance(10)
        self.breaker.acquire_permission().record_indeterminate()
        snapshot = self.breaker.snapshot()
        self.assertEqual(snapshot.state, ModelCircuitState.OPEN)
        self.assertEqual(snapshot.half_open_active_calls, 0)

    def test_half_open_probe_limit_is_thread_safe(self) -> None:
        for _ in range(2):
            self.breaker.acquire_permission().record_failure()
        self.clock.advance(10)
        barrier = threading.Barrier(8)
        accepted = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            try:
                permit = self.breaker.acquire_permission()
            except CircuitOpenError:
                return
            with lock:
                accepted.append(permit)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(accepted), 1)
        accepted[0].abandon()

    def test_registry_returns_same_breaker_across_runs(self) -> None:
        registry = ModelCircuitBreakerRegistry(clock=self.clock)
        self.assertIs(registry.get("shared"), registry.get("shared"))
        self.assertIsNot(registry.get("shared"), registry.get("other"))


if __name__ == "__main__":
    unittest.main()
