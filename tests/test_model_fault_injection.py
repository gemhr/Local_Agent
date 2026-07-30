from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.runtime import (
    FaultAction,
    FaultInjectionController,
    FaultInjectionRecorder,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InjectedFaultCode,
    ModelFailureCategory,
    ModelCircuitBreakerConfig,
    ModelCircuitBreakerRegistry,
    ModelInvocationChainError,
    ModelProfileId,
    RetryExecutor,
    RetryPolicy,
)
from tests.test_model_invocation import (
    InvocationFixture,
    LOCAL,
    REMOTE,
    RecordingAdapter,
    provider_error,
    routing,
)

NOW = datetime(2026, 7, 31, tzinfo=UTC)


def controller(
    point: FaultPoint,
    code: InjectedFaultCode,
    *,
    trigger: FaultTrigger = FaultTrigger.FIRST_MATCH,
    max_hits: int = 1,
    match_number: int | None = None,
    recorder: FaultInjectionRecorder | None = None,
) -> FaultInjectionController:
    rule = FaultRule(
        rule_id="model-fault",
        fault_point=point,
        action=FaultAction.RAISE_TYPED_ERROR,
        trigger=trigger,
        scope=FaultScope.ATTEMPT_SCOPE,
        max_hits=max_hits,
        match_number=match_number,
        component="model",
        safe_fault_code=code,
    )
    return FaultInjectionController.for_test(
        FaultPlan("model-plan", (rule,), created_at=NOW),
        recorder=recorder,
    )


def invoke(fixture: InvocationFixture, decision, fault_controller=None):
    return fixture.router.invoke(
        run_context=fixture.context,
        budget_ledger=fixture.ledger,
        routing_decision=decision,
        messages=({"role": "user", "content": "SECRET_PROMPT_TEXT"},),
        adapter_resolver=fixture.resolver,
        circuit_breaker_registry=fixture.registry,
        token_estimate=10,
        max_tokens=20,
        fault_controller=fault_controller,
    )


def test_invocation_fault_is_terminal_before_attempt_budget_and_provider() -> None:
    adapter = RecordingAdapter(["MODEL_OUTPUT_SECRET"])
    fixture = InvocationFixture({ModelProfileId.LOCAL_FAST: adapter})
    fixture.registry = ModelCircuitBreakerRegistry(
        ModelCircuitBreakerConfig(failure_threshold=3)
    )

    with pytest.raises(ModelInvocationChainError) as raised:
        invoke(
            fixture,
            routing(LOCAL),
            controller(
                FaultPoint.MODEL_BEFORE_INVOCATION,
                InjectedFaultCode.INJECTED_PERMANENT_FAILURE,
            ),
        )

    assert raised.value.failure_category is ModelFailureCategory.BUSINESS_FAILURE
    assert raised.value.failure.attempts == ()
    assert adapter.calls == 0
    assert fixture.ledger.snapshot().committed_usage.model_calls == 0
    assert "SECRET" not in str(raised.value)


@pytest.mark.parametrize(
    ("code", "category"),
    [
        (
            InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
            ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE,
        ),
        (InjectedFaultCode.INJECTED_RATE_LIMIT, ModelFailureCategory.RATE_LIMITED),
        (InjectedFaultCode.INJECTED_TIMEOUT, ModelFailureCategory.PROVIDER_TIMEOUT),
        (
            InjectedFaultCode.INJECTED_PERMANENT_FAILURE,
            ModelFailureCategory.BUSINESS_FAILURE,
        ),
    ],
)
def test_provider_fault_mapping_never_calls_provider(
    code: InjectedFaultCode,
    category: ModelFailureCategory,
) -> None:
    adapter = RecordingAdapter(["ok"])
    fixture = InvocationFixture({ModelProfileId.LOCAL_FAST: adapter})
    fixture.router = type(fixture.router)(
        retry_executor=RetryExecutor(
            RetryPolicy(max_attempts=1, base_delay_seconds=0, max_delay_seconds=0)
        )
    )

    with pytest.raises(ModelInvocationChainError) as raised:
        invoke(
            fixture,
            routing(LOCAL),
            controller(FaultPoint.MODEL_BEFORE_PROVIDER_CALL, code),
        )

    assert raised.value.failure_category is category
    assert adapter.calls == 0
    assert len(raised.value.failure.attempts) == 1
    assert raised.value.failure.attempts[0].started is False
    assert fixture.ledger.snapshot().committed_usage.model_calls == 0


def test_transient_fault_is_retried_only_by_existing_retry_executor() -> None:
    adapter = RecordingAdapter(["ok"])
    fixture = InvocationFixture({ModelProfileId.LOCAL_FAST: adapter})
    fixture.router = type(fixture.router)(
        retry_executor=RetryExecutor(
            RetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0)
        )
    )
    recorder = FaultInjectionRecorder()

    result = invoke(
        fixture,
        routing(LOCAL),
        controller(
            FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
            InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
            recorder=recorder,
        ),
    )

    assert result.output == "ok"
    assert [attempt.succeeded for attempt in result.attempts] == [False, True]
    assert adapter.calls == 1
    assert fixture.ledger.snapshot().committed_usage.model_calls == 1
    assert len(recorder.snapshot().records) == 1


def test_all_injected_retries_fail_without_provider_or_budget_commit() -> None:
    adapter = RecordingAdapter(["unused"])
    fixture = InvocationFixture({ModelProfileId.LOCAL_FAST: adapter})
    fixture.router = type(fixture.router)(
        retry_executor=RetryExecutor(
            RetryPolicy(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0)
        )
    )

    with pytest.raises(ModelInvocationChainError) as raised:
        invoke(
            fixture,
            routing(LOCAL),
            controller(
                FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
                InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
                trigger=FaultTrigger.ALWAYS,
                max_hits=3,
            ),
        )

    assert len(raised.value.failure.attempts) == 3
    assert adapter.calls == 0
    assert fixture.ledger.snapshot().committed_usage.model_calls == 0


def test_rate_limit_fault_uses_existing_fallback_policy() -> None:
    local = RecordingAdapter(["unused"])
    remote = RecordingAdapter(["fallback-ok"])
    fixture = InvocationFixture(
        {
            ModelProfileId.LOCAL_FAST: local,
            ModelProfileId.REMOTE_ADVANCED: remote,
        }
    )

    result = invoke(
        fixture,
        routing(LOCAL, REMOTE),
        controller(
            FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
            InjectedFaultCode.INJECTED_RATE_LIMIT,
        ),
    )

    assert result.output == "fallback-ok"
    assert local.calls == 0
    assert remote.calls == 1
    assert [attempt.profile_id for attempt in result.attempts] == [
        ModelProfileId.LOCAL_FAST,
        ModelProfileId.REMOTE_ADVANCED,
    ]


def test_on_second_match_uses_real_attempt_sequence() -> None:
    adapter = RecordingAdapter(
        [
            provider_error(ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE),
            "ok",
        ]
    )
    fixture = InvocationFixture({ModelProfileId.LOCAL_FAST: adapter})
    fixture.registry = ModelCircuitBreakerRegistry(
        ModelCircuitBreakerConfig(failure_threshold=3)
    )
    fixture.router = type(fixture.router)(
        retry_executor=RetryExecutor(
            RetryPolicy(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0)
        )
    )

    result = invoke(
        fixture,
        routing(LOCAL),
        controller(
            FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
            InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
            trigger=FaultTrigger.ON_NTH_MATCH,
            match_number=2,
        ),
    )

    assert result.output == "ok"
    assert len(result.attempts) == 3
    assert adapter.calls == 2
