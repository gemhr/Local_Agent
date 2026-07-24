import unittest

from core.runtime import (
    BudgetLedger, ModelAdapterInvocationError, ModelAdapterResolver,
    ModelAdapterResponse, ModelCircuitBreakerRegistry, ModelCostProfile,
    ModelFailureCategory, ModelInvocationChainError, ModelInvocationRouter,
    ModelProfile, ModelProfileId, ModelRoutingCandidate, ModelRoutingDecision,
    RetryExecutor, RetryPolicy, RunBudget, RoutingAdjustment, create_run_context,
)


def profile(profile_id, remote=False):
    return ModelProfile(profile_id, 8192, 64, True, True, True, True, 1, 1,
        ModelCostProfile(profile_id, remote, 1, 1, 1, 10), remote, f"breaker:{profile_id.value}")


def decision(*profiles):
    return ModelRoutingDecision(None, profiles[0].profile_id, tuple(
        ModelRoutingCandidate(p, p.effective_breaker_key, RoutingAdjustment.NONE, "TEST") for p in profiles
    ), 1)


class Adapter:
    def __init__(self, outcomes): self.outcomes=list(outcomes); self.calls=0
    def invoke(self, messages, *, max_tokens):
        self.calls += 1
        item=self.outcomes.pop(0)
        if isinstance(item, BaseException): raise item
        return ModelAdapterResponse(item)


def fail(category):
    return ModelAdapterInvocationError(category, provider_started=True, provider_responded=True)


class ModelRetryIntegrationTests(unittest.TestCase):
    def invoke(self, router, profiles, adapters, budget=RunBudget()):
        context, _ = create_run_context(entry_agent_id="test")
        ledger=BudgetLedger(budget, deadline_remaining=context.remaining_seconds)
        return router.invoke(run_context=context, budget_ledger=ledger, routing_decision=decision(*profiles),
            messages=(), adapter_resolver=ModelAdapterResolver(adapters), circuit_breaker_registry=ModelCircuitBreakerRegistry(), token_estimate=1, max_tokens=1), ledger

    def test_zero_delay_retry_success_and_stable_indices(self):
        local=profile(ModelProfileId.LOCAL_FAST); adapter=Adapter([fail(ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE), "ok"])
        result, ledger=self.invoke(ModelInvocationRouter(retry_executor=RetryExecutor(RetryPolicy(base_delay_seconds=0, max_delay_seconds=0))), (local,), {local.profile_id:adapter})
        self.assertEqual(adapter.calls, 2); self.assertEqual(result.output, "ok")
        self.assertEqual([(a.candidate_index, a.retry_index) for a in result.attempts], [(0,0),(0,1)])
        self.assertEqual(ledger.snapshot().committed_usage.retries, 1)

    def test_nonzero_delay_never_calls_adapter_twice(self):
        local=profile(ModelProfileId.LOCAL_FAST); adapter=Adapter([fail(ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE)])
        with self.assertRaises(ModelInvocationChainError) as raised:
            self.invoke(ModelInvocationRouter(retry_executor=RetryExecutor(RetryPolicy(base_delay_seconds=1, max_delay_seconds=1))), (local,), {local.profile_id:adapter})
        self.assertEqual(adapter.calls, 1); self.assertEqual(raised.exception.error_code, "SYNC_RETRY_DELAY_UNSUPPORTED")

    def test_rate_limit_modes(self):
        local=profile(ModelProfileId.LOCAL_FAST); remote=profile(ModelProfileId.REMOTE_ADVANCED, True)
        # FALLBACK_FIRST directly invokes the next profile.
        a, b=Adapter([fail(ModelFailureCategory.RATE_LIMITED)]), Adapter(["fallback"])
        result, ledger=self.invoke(ModelInvocationRouter(retry_executor=RetryExecutor(RetryPolicy(base_delay_seconds=0,max_delay_seconds=0))), (local,remote), {local.profile_id:a,remote.profile_id:b})
        self.assertEqual((a.calls,b.calls,ledger.snapshot().committed_usage.retries), (1,1,0))
        self.assertEqual(result.attempts[-1].retry_index, 0)

    def test_rate_limit_without_fallback_retries_current(self):
        local=profile(ModelProfileId.LOCAL_FAST); a=Adapter([fail(ModelFailureCategory.RATE_LIMITED), "ok"])
        result, ledger=self.invoke(ModelInvocationRouter(retry_executor=RetryExecutor(RetryPolicy(base_delay_seconds=0,max_delay_seconds=0))), (local,), {local.profile_id:a})
        self.assertEqual((a.calls, ledger.snapshot().committed_usage.retries), (2,1)); self.assertEqual(result.output, "ok")

    def test_rate_limit_retry_current_first_and_stop(self):
        from core.runtime import RateLimitRecoveryMode
        local=profile(ModelProfileId.LOCAL_FAST); remote=profile(ModelProfileId.REMOTE_ADVANCED, True)
        a,b=Adapter([fail(ModelFailureCategory.RATE_LIMITED), "ok"]),Adapter(["fallback"])
        result,_=self.invoke(ModelInvocationRouter(retry_executor=RetryExecutor(RetryPolicy(base_delay_seconds=0,max_delay_seconds=0,rate_limit_recovery_mode=RateLimitRecoveryMode.RETRY_CURRENT_FIRST))), (local,remote), {local.profile_id:a,remote.profile_id:b})
        self.assertEqual((result.output,a.calls,b.calls), ("ok",2,0))
        a,b=Adapter([fail(ModelFailureCategory.RATE_LIMITED)]),Adapter(["fallback"])
        with self.assertRaises(ModelInvocationChainError):
            self.invoke(ModelInvocationRouter(retry_executor=RetryExecutor(RetryPolicy(rate_limit_recovery_mode=RateLimitRecoveryMode.STOP))), (local,remote), {local.profile_id:a,remote.profile_id:b})
        self.assertEqual((a.calls,b.calls), (1,0))

    def test_duplicate_candidate_chain_is_rejected(self):
        local=profile(ModelProfileId.LOCAL_FAST); a=Adapter(["unused"])
        with self.assertRaisesRegex(RuntimeError, "重复"):
            self.invoke(ModelInvocationRouter(), (local,local), {local.profile_id:a})
