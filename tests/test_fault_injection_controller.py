from datetime import UTC, datetime

import pytest

from core.runtime import (
    ControllableFaultSleeper,
    FaultAction,
    FaultConfigurationCode,
    FaultExecutionConfigurationError,
    FaultInjectionController,
    FaultInjectionRecorder,
    FaultMatchContext,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InjectedFailureResult,
    InjectedFaultCode,
    InjectedFaultError,
    NO_FAULT_DECISION,
)

NOW = datetime(2026, 7, 31, 1, 2, 3, tzinfo=UTC)


class FixedClock:
    def utc_now(self) -> datetime:
        return NOW


def make_rule(**changes) -> FaultRule:
    values = {
        "rule_id": "rule-a",
        "fault_point": FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
        "action": FaultAction.RAISE_TYPED_ERROR,
        "trigger": FaultTrigger.ALWAYS,
        "scope": FaultScope.INVOCATION_SCOPE,
        "max_hits": 1,
        "component": "model",
        "safe_fault_code": InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
    }
    values.update(changes)
    return FaultRule(**values)


def context(
    point: FaultPoint = FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
    component: str = "model",
) -> FaultMatchContext:
    return FaultMatchContext(fault_point=point, component=component)


def controller(*rules: FaultRule, **kwargs) -> FaultInjectionController:
    return FaultInjectionController.for_test(
        FaultPlan("plan-a", tuple(rules), created_at=NOW),
        clock=FixedClock(),
        **kwargs,
    )


def test_default_and_explicit_disabled_controller_are_no_fault_null_objects() -> None:
    default = FaultInjectionController()
    explicit = FaultInjectionController(
        FaultPlan("plan-a", (make_rule(),), created_at=NOW),
        enabled=False,
    )
    assert default.evaluate(context()) is NO_FAULT_DECISION
    assert explicit.evaluate(context()) is NO_FAULT_DECISION
    assert default.snapshot().plan_id is None


def test_matching_is_content_free_and_fixed_rule_order_wins() -> None:
    later = make_rule(rule_id="z-rule")
    first = make_rule(
        rule_id="a-rule",
        safe_fault_code=InjectedFaultCode.INJECTED_RATE_LIMIT,
    )
    value = controller(later, first)
    decision = value.evaluate(context())
    assert decision.rule_id == "a-rule"
    assert decision.safe_fault_code is InjectedFaultCode.INJECTED_RATE_LIMIT
    assert decision.match_ordinal == decision.hit_ordinal == 1
    assert decision.triggered_at == NOW
    assert value.snapshot().counters[1].match_count == 0


@pytest.mark.parametrize(
    ("trigger", "match_number", "outcomes"),
    [
        (FaultTrigger.ALWAYS, None, [True, True, False, False]),
        (FaultTrigger.FIRST_MATCH, None, [True, False, False, False]),
        (FaultTrigger.ON_NTH_MATCH, 3, [False, False, True, False]),
        (FaultTrigger.AFTER_N_MATCHES, 2, [False, False, True, True]),
    ],
)
def test_trigger_semantics_are_deterministic(
    trigger: FaultTrigger,
    match_number: int | None,
    outcomes: list[bool],
) -> None:
    value = controller(
        make_rule(
            trigger=trigger,
            match_number=match_number,
            max_hits=(
                1
                if trigger in {
                    FaultTrigger.FIRST_MATCH,
                    FaultTrigger.ON_NTH_MATCH,
                }
                else 2
            ),
        )
    )
    decisions = [value.evaluate(context()) for _ in outcomes]
    assert [item.matched for item in decisions] == outcomes
    snapshot = value.snapshot().counters[0]
    assert snapshot.match_count == len(outcomes)
    assert snapshot.hit_count == sum(outcomes)
    assert [item.hit_ordinal for item in decisions if item.matched] == list(
        range(1, sum(outcomes) + 1)
    )


def test_priority_precedes_rule_id_and_equal_priority_uses_rule_id() -> None:
    value = controller(
        make_rule(rule_id="a-low", priority=100),
        make_rule(
            rule_id="z-high",
            priority=1,
            safe_fault_code=InjectedFaultCode.INJECTED_RATE_LIMIT,
        ),
    )
    assert (
        value.evaluate(context()).safe_fault_code
        is InjectedFaultCode.INJECTED_RATE_LIMIT
    )

    tied = controller(
        make_rule(rule_id="z-rule", priority=10),
        make_rule(
            rule_id="a-rule",
            priority=10,
            safe_fault_code=InjectedFaultCode.INJECTED_TIMEOUT,
        ),
    )
    assert tied.evaluate(context()).rule_id == "a-rule"


def test_rule_match_conditions_and_rule_counters_are_independent() -> None:
    value = controller(
        make_rule(rule_id="model-rule"),
        make_rule(
            rule_id="tool-rule",
            fault_point=FaultPoint.TOOL_BEFORE_ATTEMPT,
            component="tool",
        ),
    )
    model_decision = value.evaluate(context())
    tool_decision = value.evaluate(context(FaultPoint.TOOL_BEFORE_ATTEMPT, "tool"))
    counters = {item.rule_id: item for item in value.snapshot().counters}
    assert model_decision.rule_id == "model-rule"
    assert tool_decision.rule_id == "tool-rule"
    assert counters["model-rule"].hit_count == 1
    assert counters["tool-rule"].hit_count == 1


def test_close_is_idempotent_and_prevents_future_matches() -> None:
    value = controller(make_rule(max_hits=2))
    assert value.evaluate(context()).matched
    value.close()
    value.close()
    assert value.evaluate(context()) is NO_FAULT_DECISION
    assert value.snapshot().closed is True
    assert value.snapshot().counters[0].hit_count == 1


def test_recorder_receives_only_safe_matched_decisions() -> None:
    recorder = FaultInjectionRecorder(capacity=2)
    value = controller(make_rule(), recorder=recorder)
    value.evaluate(context())
    value.evaluate(context())
    snapshot = recorder.snapshot()
    assert len(snapshot.records) == 1
    assert snapshot.records[0].plan_id == "plan-a"
    assert snapshot.records[0].component == "model"
    assert snapshot.records[0].timestamp == NOW


@pytest.mark.asyncio
async def test_raise_typed_error_contains_only_fixed_code() -> None:
    value = controller(make_rule())
    with pytest.raises(InjectedFaultError) as exc:
        await value.execute_if_matched(context())
    assert exc.value.code is InjectedFaultCode.INJECTED_TRANSIENT_FAILURE
    assert str(exc.value) == "INJECTED_TRANSIENT_FAILURE"


@pytest.mark.asyncio
async def test_delay_uses_injected_cancellable_sleeper() -> None:
    sleeper = ControllableFaultSleeper()
    value = controller(
        make_rule(
            action=FaultAction.DELAY,
            safe_fault_code=None,
            delay_seconds=2.5,
        ),
        sleeper=sleeper,
    )
    task = __import__("asyncio").create_task(value.execute_if_matched(context()))
    await sleeper.entered.wait()
    assert sleeper.requested_delays == [2.5]
    sleeper.release.set()
    decision = await task
    assert decision.matched is True


@pytest.mark.asyncio
async def test_return_typed_failure_preserves_fixed_result_contract() -> None:
    value = controller(
        make_rule(
            action=FaultAction.RETURN_TYPED_FAILURE,
            safe_fault_code=InjectedFaultCode.INJECTED_RATE_LIMIT,
        )
    )
    result = await value.execute_if_matched(context())
    assert result == InjectedFailureResult(InjectedFaultCode.INJECTED_RATE_LIMIT)


@pytest.mark.asyncio
async def test_corrupt_fixture_requires_explicit_test_mutator_and_uses_descriptor() -> (
    None
):
    fault_rule = make_rule(
        action=FaultAction.CORRUPT_TEST_FIXTURE,
        safe_fault_code=None,
        fixture_mutation="truncate_fixture",
    )
    with pytest.raises(FaultExecutionConfigurationError) as exc:
        await controller(fault_rule).execute_if_matched(context())
    assert exc.value.code is FaultConfigurationCode.FIXTURE_MUTATOR_REQUIRED

    mutations: list[str] = []
    result = await controller(
        fault_rule,
        fixture_mutator=mutations.append,
    ).execute_if_matched(context())
    assert result.matched is True
    assert mutations == ["truncate_fixture"]


@pytest.mark.asyncio
async def test_block_action_without_runtime_blocker_fails_closed() -> None:
    value = controller(
        make_rule(
            action=FaultAction.BLOCK_UNTIL_RELEASED,
            safe_fault_code=None,
        )
    )
    with pytest.raises(FaultExecutionConfigurationError) as exc:
        await value.execute_if_matched(context())
    assert exc.value.code is FaultConfigurationCode.BLOCKER_REQUIRED
