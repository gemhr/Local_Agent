import tempfile
import threading
import unittest
from pathlib import Path

from core.agent_router import AgentRouter
from core.chat_service import ChatService
from core.llm_engine import RemoteLLMEngine
from core.memory_manager import MemoryManager
from core.runtime import (
    BudgetExceededError,
    BudgetLedger,
    BudgetUsage,
    CancellationReason,
    CircuitHealthOutcome,
    GeneratorModelAdapter,
    ModelAdapterInvocationError,
    ModelAdapterResolver,
    ModelAdapterResponse,
    ModelCircuitBreakerConfig,
    ModelCircuitBreakerRegistry,
    ModelContextRequirements,
    ModelCostProfile,
    ModelFailureCategory,
    ModelInvocationChainError,
    ModelInvocationRouter,
    ModelPreference,
    ModelProfile,
    ModelProfileId,
    ModelResolver,
    ModelRoutingCandidate,
    ModelRoutingDecision,
    ModelRoutingPolicy,
    ModelSelectionDecision,
    ModelSelectionObjective,
    ModelSelectionReason,
    RoutingAdjustment,
    RetryExecutor,
    RetryPolicy,
    RunBudget,
    RunCancelledError,
    RunStatus,
    TaskCapabilityRequirements,
    create_run_context,
)


def profile(
    profile_id: ModelProfileId, *, remote: bool, window: int = 8192
) -> ModelProfile:
    return ModelProfile(
        profile_id,
        window,
        64,
        True,
        True,
        True,
        True,
        2 if remote else 1,
        2 if remote else 1,
        ModelCostProfile(profile_id, remote, 1, 1, 1, 10),
        remote,
        f"breaker:{profile_id.value}",
    )


LOCAL = profile(ModelProfileId.LOCAL_FAST, remote=False)
REMOTE = profile(ModelProfileId.REMOTE_ADVANCED, remote=True, window=16384)
REMOTE_BACKUP = profile(
    ModelProfileId.REMOTE_BACKUP, remote=True, window=16384
)


def routing(*profiles: ModelProfile) -> ModelRoutingDecision:
    initial = profiles[0]
    candidates = []
    for index, item in enumerate(profiles):
        if index == 0:
            adjustment = RoutingAdjustment.NONE
        elif not initial.effective_is_remote and item.effective_is_remote:
            adjustment = RoutingAdjustment.ESCALATE_TO_REMOTE
        elif initial.effective_is_remote and not item.effective_is_remote:
            adjustment = RoutingAdjustment.DOWNGRADE_TO_LOCAL
        else:
            adjustment = RoutingAdjustment.SWITCH_SAME_TIER
        candidates.append(
            ModelRoutingCandidate(
                item,
                item.effective_breaker_key,
                adjustment,
                "SAFE_REASON",
            )
        )
    return ModelRoutingDecision(
        ModelProfileId.REMOTE_ADVANCED,
        initial.profile_id,
        tuple(candidates),
        512,
        any(
            item.adjustment == RoutingAdjustment.DOWNGRADE_TO_LOCAL
            for item in candidates
        ),
    )


def policy_routing(
    preference: ModelPreference,
    profiles: tuple[ModelProfile, ...],
    selected: ModelProfileId,
) -> ModelRoutingDecision:
    selection = ModelSelectionDecision(
        selected,
        ModelSelectionReason.LOCAL_SUFFICIENT,
        "安全说明",
        ("rule",),
        False,
        selected,
        selected,
        ModelSelectionObjective.QUALITY_FIRST,
    )
    return ModelRoutingPolicy().route(
        selection_decision=selection,
        capability_requirements=TaskCapabilityRequirements(),
        context_requirements=ModelContextRequirements(
            10, 512, False, False, False, 1, 0, 0, False, False
        ),
        profiles=profiles,
        preference=preference,
        budget_snapshot=BudgetLedger(RunBudget()).snapshot(),
    )


class RecordingAdapter:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def invoke(self, messages, *, max_tokens):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return ModelAdapterResponse(str(outcome))


def provider_error(
    category: ModelFailureCategory,
    *,
    output_started: bool = False,
    provider_started: bool = True,
    provider_responded: bool | None = True,
) -> ModelAdapterInvocationError:
    return ModelAdapterInvocationError(
        category,
        provider_started=provider_started,
        provider_responded=provider_responded,
        output_started=output_started,
    )


class InvocationFixture:
    def __init__(self, adapters, *, budget: RunBudget | None = None) -> None:
        self.context, self.source = create_run_context(entry_agent_id="agent")
        self.ledger = BudgetLedger(
            budget or RunBudget(), deadline_remaining=self.context.remaining_seconds
        )
        self.context.attach_budget_ledger(self.ledger)
        self.registry = ModelCircuitBreakerRegistry(
            ModelCircuitBreakerConfig(failure_threshold=1)
        )
        self.resolver = ModelAdapterResolver(adapters)
        self.router = ModelInvocationRouter()

    def invoke(self, decision: ModelRoutingDecision):
        return self.router.invoke(
            run_context=self.context,
            budget_ledger=self.ledger,
            routing_decision=decision,
            messages=({"role": "user", "content": "redacted"},),
            adapter_resolver=self.resolver,
            circuit_breaker_registry=self.registry,
            token_estimate=10,
            max_tokens=20,
        )


class ModelInvocationTests(unittest.TestCase):
    def test_initial_selection_reaches_resolver_and_adapter_once(self) -> None:
        local = RecordingAdapter(["local"])
        remote = RecordingAdapter(["remote"])
        fixture = InvocationFixture(
            {
                ModelProfileId.LOCAL_FAST: local,
                ModelProfileId.REMOTE_ADVANCED: remote,
            }
        )
        result = fixture.invoke(routing(REMOTE, LOCAL))
        self.assertEqual(result.output, "remote")
        self.assertEqual(result.executed_profile_id, ModelProfileId.REMOTE_ADVANCED)
        self.assertEqual(local.calls, 0)
        self.assertEqual(remote.calls, 1)

    def test_open_circuit_blocks_retry_before_fallback(self) -> None:
        local = RecordingAdapter(
            [provider_error(ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE)]
        )
        remote = RecordingAdapter(["remote"])
        fixture = InvocationFixture(
            {
                ModelProfileId.LOCAL_FAST: local,
                ModelProfileId.REMOTE_ADVANCED: remote,
            }
        )
        result = fixture.invoke(routing(LOCAL, REMOTE))
        self.assertEqual(result.executed_profile_id, ModelProfileId.REMOTE_ADVANCED)
        self.assertEqual((local.calls, remote.calls), (1, 1))
        self.assertEqual(len(result.attempts), 3)
        self.assertTrue(result.attempts[0].started)
        self.assertFalse(result.attempts[1].started)
        self.assertEqual(
            result.attempts[1].failure_category,
            ModelFailureCategory.CIRCUIT_OPEN,
        )
        self.assertEqual(
            [
                (attempt.candidate_index, attempt.retry_index)
                for attempt in result.attempts
            ],
            [(0, 0), (0, 1), (1, 0)],
        )
        self.assertEqual(fixture.ledger.snapshot().committed_usage.model_calls, 2)

    def test_rate_limit_can_downgrade_with_quality_disclosure(self) -> None:
        remote = RecordingAdapter(
            [provider_error(ModelFailureCategory.RATE_LIMITED)]
        )
        local = RecordingAdapter(["local"])
        fixture = InvocationFixture(
            {
                ModelProfileId.LOCAL_FAST: local,
                ModelProfileId.REMOTE_ADVANCED: remote,
            }
        )
        result = fixture.invoke(routing(REMOTE, LOCAL))
        self.assertEqual(result.executed_profile_id, ModelProfileId.LOCAL_FAST)
        self.assertTrue(result.quality_tradeoff_disclosed)

    def test_safety_invalid_and_unknown_do_not_fallback(self) -> None:
        for category in (
            ModelFailureCategory.SAFETY_REFUSAL,
            ModelFailureCategory.INVALID_REQUEST,
            ModelFailureCategory.UNKNOWN_FAILURE,
        ):
            with self.subTest(category=category):
                local = RecordingAdapter([provider_error(category)])
                remote = RecordingAdapter(["must not run"])
                fixture = InvocationFixture(
                    {
                        ModelProfileId.LOCAL_FAST: local,
                        ModelProfileId.REMOTE_ADVANCED: remote,
                    }
                )
                with self.assertRaises(ModelInvocationChainError) as caught:
                    fixture.invoke(routing(LOCAL, REMOTE))
                self.assertIsNone(caught.exception.failure.executed_profile_id)
                self.assertEqual(remote.calls, 0)

    def test_partial_output_forbids_transparent_fallback(self) -> None:
        local = RecordingAdapter(
            [
                provider_error(
                    ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE,
                    output_started=True,
                )
            ]
        )
        remote = RecordingAdapter(["must not run"])
        fixture = InvocationFixture(
            {
                ModelProfileId.LOCAL_FAST: local,
                ModelProfileId.REMOTE_ADVANCED: remote,
            }
        )
        with self.assertRaises(ModelInvocationChainError):
            fixture.invoke(routing(LOCAL, REMOTE))
        self.assertEqual(local.calls, 1)
        self.assertEqual(remote.calls, 0)
        self.assertEqual(fixture.ledger.snapshot().committed_usage.retries, 0)

    def test_open_circuit_skips_without_budget_consumption(self) -> None:
        local = RecordingAdapter(
            [provider_error(ModelFailureCategory.PROVIDER_TIMEOUT)]
        )
        remote = RecordingAdapter(["remote"])
        fixture = InvocationFixture(
            {
                ModelProfileId.LOCAL_FAST: local,
                ModelProfileId.REMOTE_ADVANCED: remote,
            }
        )
        breaker = fixture.registry.get(LOCAL.effective_breaker_key)
        breaker.acquire_permission().record_failure()
        failure_count = breaker.snapshot().consecutive_failures
        result = fixture.invoke(
            policy_routing(
                ModelPreference.AUTO,
                (LOCAL, REMOTE),
                ModelProfileId.LOCAL_FAST,
            )
        )
        self.assertFalse(result.attempts[0].started)
        self.assertEqual(
            result.attempts[0].failure_category, ModelFailureCategory.CIRCUIT_OPEN
        )
        usage = fixture.ledger.snapshot().committed_usage
        self.assertEqual(usage.model_calls, 1)
        self.assertEqual(usage.remote_model_calls, 1)
        self.assertEqual(usage.input_tokens, 10)
        self.assertEqual(usage.output_tokens, 20)
        self.assertEqual(usage.total_tokens, 30)
        self.assertEqual(usage.cost_units, 3)
        self.assertEqual(
            breaker.snapshot().consecutive_failures, failure_count
        )
        self.assertEqual(local.calls, 0)
        self.assertEqual(
            len({attempt.profile_id for attempt in result.attempts}),
            len(result.attempts),
        )

    def test_force_local_open_circuit_cannot_escape_to_remote(self) -> None:
        local = RecordingAdapter(["must not run"])
        remote = RecordingAdapter(["must not run"])
        fixture = InvocationFixture(
            {
                ModelProfileId.LOCAL_FAST: local,
                ModelProfileId.REMOTE_ADVANCED: remote,
            }
        )
        breaker = fixture.registry.get(LOCAL.effective_breaker_key)
        breaker.acquire_permission().record_failure()
        with self.assertRaises(ModelInvocationChainError):
            fixture.invoke(
                policy_routing(
                    ModelPreference.FORCE_LOCAL,
                    (LOCAL, REMOTE),
                    ModelProfileId.LOCAL_FAST,
                )
            )
        usage = fixture.ledger.snapshot().committed_usage
        self.assertEqual(usage, BudgetUsage())
        self.assertEqual((local.calls, remote.calls), (0, 0))
        self.assertEqual(breaker.snapshot().consecutive_failures, 1)

    def test_force_remote_open_primary_can_switch_to_remote_backup(self) -> None:
        primary = RecordingAdapter(["must not run"])
        backup = RecordingAdapter(["backup"])
        fixture = InvocationFixture(
            {
                ModelProfileId.REMOTE_ADVANCED: primary,
                ModelProfileId.REMOTE_BACKUP: backup,
            }
        )
        fixture.registry.get(
            REMOTE.effective_breaker_key
        ).acquire_permission().record_failure()
        result = fixture.invoke(
            policy_routing(
                ModelPreference.FORCE_REMOTE,
                (REMOTE, REMOTE_BACKUP),
                ModelProfileId.REMOTE_ADVANCED,
            )
        )
        self.assertEqual(result.executed_profile_id, ModelProfileId.REMOTE_BACKUP)
        self.assertEqual((primary.calls, backup.calls), (0, 1))
        self.assertEqual(
            [attempt.profile_id for attempt in result.attempts],
            [ModelProfileId.REMOTE_ADVANCED, ModelProfileId.REMOTE_BACKUP],
        )

    def test_healthy_routing_failure_resets_consecutive_provider_failures(
        self,
    ) -> None:
        adapter = RecordingAdapter(
            [
                provider_error(ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE),
                provider_error(ModelFailureCategory.SAFETY_REFUSAL),
                provider_error(ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE),
            ]
        )
        fixture = InvocationFixture({ModelProfileId.LOCAL_FAST: adapter})
        fixture.registry = ModelCircuitBreakerRegistry(
            ModelCircuitBreakerConfig(failure_threshold=2)
        )
        fixture.router = ModelInvocationRouter(
            retry_executor=RetryExecutor(
                RetryPolicy(
                    max_attempts=1,
                    base_delay_seconds=0,
                    max_delay_seconds=0,
                )
            )
        )
        breaker = fixture.registry.get(LOCAL.effective_breaker_key)
        for expected_failures in (1, 0, 1):
            with self.assertRaises(ModelInvocationChainError):
                fixture.invoke(routing(LOCAL))
            self.assertEqual(
                breaker.snapshot().consecutive_failures, expected_failures
            )
            self.assertEqual(breaker.snapshot().state.value, "CLOSED")

    def test_half_open_non_infrastructure_results_close_probe(self) -> None:
        for category in (
            ModelFailureCategory.SAFETY_REFUSAL,
            ModelFailureCategory.BUSINESS_FAILURE,
            ModelFailureCategory.OUTPUT_VALIDATION_FAILED,
            ModelFailureCategory.INVALID_REQUEST,
        ):
            with self.subTest(category=category):
                now = [0.0]
                adapter = RecordingAdapter(
                    [
                        provider_error(
                            ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE
                        ),
                        provider_error(category, provider_responded=True),
                    ]
                )
                fixture = InvocationFixture(
                    {ModelProfileId.LOCAL_FAST: adapter}
                )
                fixture.registry = ModelCircuitBreakerRegistry(
                    ModelCircuitBreakerConfig(
                        failure_threshold=1,
                        recovery_timeout_seconds=10,
                    ),
                    clock=lambda: now[0],
                )
                breaker = fixture.registry.get(LOCAL.effective_breaker_key)
                with self.assertRaises(ModelInvocationChainError):
                    fixture.invoke(routing(LOCAL))
                now[0] = 10.0
                with self.assertRaises(ModelInvocationChainError):
                    fixture.invoke(routing(LOCAL))
                snapshot = breaker.snapshot()
                self.assertEqual(snapshot.state.value, "CLOSED")
                self.assertEqual(snapshot.half_open_active_calls, 0)
                self.assertEqual(snapshot.consecutive_failures, 0)

    def test_adapter_resolve_failure_abandons_half_open_probe(self) -> None:
        now = [0.0]
        fixture = InvocationFixture({})
        fixture.registry = ModelCircuitBreakerRegistry(
            ModelCircuitBreakerConfig(
                failure_threshold=1,
                recovery_timeout_seconds=10,
            ),
            clock=lambda: now[0],
        )
        breaker = fixture.registry.get(LOCAL.effective_breaker_key)
        breaker.acquire_permission().record_failure()
        now[0] = 10.0
        with self.assertRaises(ModelInvocationChainError):
            fixture.invoke(routing(LOCAL))
        snapshot = breaker.snapshot()
        self.assertEqual(snapshot.state.value, "HALF_OPEN")
        self.assertEqual(snapshot.half_open_active_calls, 0)
        self.assertEqual(fixture.ledger.snapshot().committed_usage, BudgetUsage())

    def test_provider_started_paths_never_abandon_permit(self) -> None:
        class SpyPermit:
            def __init__(self) -> None:
                self.success = 0
                self.failure = 0
                self.indeterminate = 0
                self.abandoned = 0

            def record_success(self):
                self.success += 1

            def record_failure(self):
                self.failure += 1

            def record_indeterminate(self):
                self.indeterminate += 1

            def abandon(self):
                self.abandoned += 1

        class SpyBreaker:
            def __init__(self, permit) -> None:
                self.permit = permit

            def acquire_permission(self):
                return self.permit

        class SpyRegistry:
            def __init__(self, permit) -> None:
                self.config = ModelCircuitBreakerConfig()
                self.breaker = SpyBreaker(permit)

            def get(self, _key):
                return self.breaker

        for category, responded, expected in (
            (ModelFailureCategory.SAFETY_REFUSAL, True, "success"),
            (ModelFailureCategory.BUSINESS_FAILURE, True, "success"),
            (ModelFailureCategory.OUTPUT_VALIDATION_FAILED, True, "success"),
            (ModelFailureCategory.INVALID_REQUEST, True, "success"),
            (ModelFailureCategory.INVALID_REQUEST, False, "indeterminate"),
        ):
            with self.subTest(category=category, responded=responded):
                adapter = RecordingAdapter(
                    [provider_error(category, provider_responded=responded)]
                )
                fixture = InvocationFixture(
                    {ModelProfileId.LOCAL_FAST: adapter}
                )
                permit = SpyPermit()
                fixture.registry = SpyRegistry(permit)
                with self.assertRaises(ModelInvocationChainError):
                    fixture.invoke(routing(LOCAL))
                self.assertEqual(permit.abandoned, 0)
                self.assertEqual(getattr(permit, expected), 1)

    def test_routing_failure_and_circuit_health_are_independent(self) -> None:
        fixture = InvocationFixture(
            {
                ModelProfileId.LOCAL_FAST: RecordingAdapter(
                    [provider_error(ModelFailureCategory.SAFETY_REFUSAL)]
                )
            }
        )
        with self.assertRaises(ModelInvocationChainError):
            fixture.invoke(routing(LOCAL))
        snapshot = fixture.registry.get(
            LOCAL.effective_breaker_key
        ).snapshot()
        self.assertEqual(snapshot.state.value, "CLOSED")
        self.assertEqual(snapshot.consecutive_failures, 0)
        self.assertEqual(
            fixture.router._circuit_health_outcome(
                category=ModelFailureCategory.SAFETY_REFUSAL,
                provider_started=True,
                provider_responded=True,
                registry=fixture.registry,
            ),
            CircuitHealthOutcome.HEALTHY_COMPLETION,
        )

    def test_budget_failure_abandons_half_open_permit(self) -> None:
        adapter = RecordingAdapter(["unused"])
        fixture = InvocationFixture(
            {ModelProfileId.LOCAL_FAST: adapter},
            budget=RunBudget(max_model_calls=0),
        )
        now = [0.0]
        fixture.registry = ModelCircuitBreakerRegistry(
            ModelCircuitBreakerConfig(
                failure_threshold=1,
                recovery_timeout_seconds=10,
                half_open_max_calls=1,
            ),
            clock=lambda: now[0],
        )
        breaker = fixture.registry.get(LOCAL.effective_breaker_key)
        breaker.acquire_permission().record_failure()
        now[0] = 10.0
        with self.assertRaises(BudgetExceededError):
            fixture.invoke(routing(LOCAL))
        self.assertEqual(breaker.snapshot().half_open_active_calls, 0)
        self.assertEqual(adapter.calls, 0)

    def test_cancel_terminates_chain_without_adapter_call(self) -> None:
        adapter = RecordingAdapter(["unused"])
        fixture = InvocationFixture({ModelProfileId.LOCAL_FAST: adapter})
        fixture.source.cancel(CancellationReason.USER_CANCELLED)
        with self.assertRaises(RunCancelledError):
            fixture.invoke(routing(LOCAL))
        self.assertEqual(adapter.calls, 0)


class RemoteSessionInvocationConcurrencyTests(unittest.TestCase):
    def test_two_runs_share_remote_engine_without_concurrent_session_access(
        self,
    ) -> None:
        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": "ok"}}]}

        class ControlledSession:
            def __init__(self) -> None:
                self.lock = threading.Lock()
                self.first_entered = threading.Event()
                self.concurrent_access = threading.Event()
                self.release = threading.Event()
                self.active = 0
                self.max_active = 0
                self.calls = 0

            def mount(self, *_args) -> None:
                pass

            def post(self, *_args, **_kwargs):
                with self.lock:
                    self.calls += 1
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    if self.active > 1:
                        self.concurrent_access.set()
                    if self.calls == 1:
                        self.first_entered.set()
                self.release.wait(2)
                with self.lock:
                    self.active -= 1
                return Response()

            def close(self) -> None:
                pass

        session = ControlledSession()
        engine = RemoteLLMEngine(
            "https://example.test",
            "model",
            session=session,
        )
        resolver = ModelAdapterResolver(
            {ModelProfileId.REMOTE_ADVANCED: GeneratorModelAdapter(engine)}
        )
        registry = ModelCircuitBreakerRegistry()
        invocation_router = ModelInvocationRouter()
        results = []
        ledgers = []
        errors = []
        result_lock = threading.Lock()

        def run_once() -> None:
            context, _source = create_run_context(entry_agent_id="agent")
            ledger = BudgetLedger(RunBudget())
            context.attach_budget_ledger(ledger)
            try:
                result = invocation_router.invoke(
                    run_context=context,
                    budget_ledger=ledger,
                    routing_decision=routing(REMOTE),
                    messages=({"role": "user", "content": "redacted"},),
                    adapter_resolver=resolver,
                    circuit_breaker_registry=registry,
                    token_estimate=10,
                    max_tokens=20,
                )
            except Exception as exc:
                with result_lock:
                    errors.append(exc)
                return
            with result_lock:
                results.append(result)
                ledgers.append(ledger)

        first = threading.Thread(target=run_once)
        second = threading.Thread(target=run_once)
        first.start()
        self.assertTrue(session.first_entered.wait(1))
        second.start()
        self.assertFalse(session.concurrent_access.wait(0.1))
        session.release.set()
        first.join(2)
        second.join(2)

        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(session.calls, 2)
        self.assertEqual(session.max_active, 1)
        self.assertTrue(
            all(
                ledger.snapshot().committed_usage.model_calls == 1
                for ledger in ledgers
            )
        )
        self.assertEqual(
            registry.get(REMOTE.effective_breaker_key).snapshot().state.value,
            "CLOSED",
        )


class CoordinatedInvocationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_coordinated_path_uses_router_and_succeeds_after_fallback(
        self,
    ) -> None:
        local = RecordingAdapter(
            [
                provider_error(
                    ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE
                )
                for _ in range(3)
            ]
        )
        remote = RecordingAdapter(["fallback answer"])

        class NoDirectCallEngine:
            def generate(self, *args, **kwargs):
                raise AssertionError("Coordinated 最终回答不得直接调用 Legacy Client")

        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryManager(str(Path(directory) / "memory.db"))
            router = AgentRouter(
                NoDirectCallEngine(),
                memory,
                orchestration_enabled=False,
                model_profiles=(LOCAL, REMOTE),
                model_resolver=ModelResolver(
                    {
                        ModelProfileId.LOCAL_FAST: NoDirectCallEngine(),
                        ModelProfileId.REMOTE_ADVANCED: NoDirectCallEngine(),
                    }
                ),
                model_adapter_resolver=ModelAdapterResolver(
                    {
                        ModelProfileId.LOCAL_FAST: local,
                        ModelProfileId.REMOTE_ADVANCED: remote,
                    }
                ),
            )
            output, result = await ChatService(router).run_coordinated_agent(
                "core_router", "简单问题", persist=False
            )
        self.assertEqual(output, "fallback answer")
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual((local.calls, remote.calls), (3, 1))

    async def test_all_candidates_failed_marks_step_and_run_failed(self) -> None:
        local = RecordingAdapter(
            [
                provider_error(ModelFailureCategory.PROVIDER_TIMEOUT)
                for _ in range(3)
            ]
        )
        remote = RecordingAdapter(
            [
                provider_error(ModelFailureCategory.RATE_LIMITED)
                for _ in range(3)
            ]
        )

        class Placeholder:
            def generate(self, *args, **kwargs):
                yield "unused"

        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryManager(str(Path(directory) / "memory.db"))
            engines = {
                ModelProfileId.LOCAL_FAST: Placeholder(),
                ModelProfileId.REMOTE_ADVANCED: Placeholder(),
            }
            router = AgentRouter(
                engines[ModelProfileId.LOCAL_FAST],
                memory,
                orchestration_enabled=False,
                model_profiles=(LOCAL, REMOTE),
                model_resolver=ModelResolver(engines),
                model_adapter_resolver=ModelAdapterResolver(
                    {
                        ModelProfileId.LOCAL_FAST: local,
                        ModelProfileId.REMOTE_ADVANCED: remote,
                    }
                ),
            )
            output, result = await ChatService(router).run_coordinated_agent(
                "core_router", "简单问题", persist=False
            )
        self.assertIsNone(output)
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.failed_step_ids, ("answer",))
        self.assertEqual((local.calls, remote.calls), (3, 3))


if __name__ == "__main__":
    unittest.main()
