from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from core.runtime import (
    DANGEROUS_FAULT_POINTS,
    FAULT_PLAN_SCHEMA_VERSION,
    FaultAction,
    FaultMatchContext,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InjectedFaultCode,
)

NOW = datetime(2026, 7, 31, tzinfo=UTC)
DIGEST = "a" * 64


def rule(**changes) -> FaultRule:
    values = {
        "rule_id": "rule-a",
        "fault_point": FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
        "action": FaultAction.RAISE_TYPED_ERROR,
        "trigger": FaultTrigger.FIRST_MATCH,
        "scope": FaultScope.INVOCATION_SCOPE,
        "max_hits": 1,
        "safe_fault_code": InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
    }
    values.update(changes)
    return FaultRule(**values)


def test_fault_point_catalog_is_fixed_and_complete_for_foundation() -> None:
    expected = {
        "MODEL_BEFORE_INVOCATION",
        "MODEL_BEFORE_PROVIDER_CALL",
        "MODEL_AFTER_PROVIDER_SUCCESS",
        "MODEL_BEFORE_USAGE_COMMIT",
        "MODEL_AFTER_USAGE_COMMIT",
        "TOOL_BEFORE_INVOCATION",
        "TOOL_BEFORE_ATTEMPT",
        "TOOL_BEFORE_PROVIDER_CALL",
        "TOOL_AFTER_PROVIDER_RETURN",
        "TOOL_BEFORE_SIDE_EFFECT_COMMIT",
        "TOOL_AFTER_SIDE_EFFECT_COMMIT",
        "TOOL_BEFORE_COMPLETION_EVENT",
        "RETRIEVAL_BEFORE_REWRITE",
        "RETRIEVAL_AFTER_REWRITE",
        "RETRIEVAL_BEFORE_SEARCH",
        "RETRIEVAL_AFTER_SEARCH",
        "RETRIEVAL_BEFORE_RESULT_COMMIT",
        "EVENT_BEFORE_JOURNAL_APPEND",
        "EVENT_AFTER_JOURNAL_APPEND",
        "EVENT_BEFORE_CHANNEL_ENQUEUE",
        "JOURNAL_BEFORE_READ",
        "JOURNAL_BEFORE_TERMINAL_APPEND",
        "SNAPSHOT_BEFORE_SAVE",
        "SNAPSHOT_AFTER_SAVE",
        "SNAPSHOT_BEFORE_READ",
        "RECOVERY_BEFORE_TAIL_READ",
        "RECOVERY_AFTER_TAIL_READ",
        "EXECUTOR_BEFORE_SUBMIT",
        "EXECUTOR_AFTER_SUBMIT",
        "CHANNEL_BEFORE_RECEIVE",
        "CHANNEL_BEFORE_DRAIN_HANDOFF",
        "OBSERVABILITY_BEFORE_RECORD",
        "OBSERVABILITY_BEFORE_FLUSH",
        "TRACE_BEFORE_SPAN_START",
        "TRACE_BEFORE_SPAN_END",
        "TRACE_BEFORE_FLUSH",
        "SHUTDOWN_BEFORE_RUN_CANCEL",
        "SHUTDOWN_BEFORE_WORKER_DRAIN",
        "SHUTDOWN_BEFORE_JOURNAL_CLOSE",
        "SHUTDOWN_BEFORE_MODEL_CLOSE",
        "SHUTDOWN_COMPONENT_CLOSE",
    }
    assert {point.value for point in FaultPoint} == expected
    assert {action.value for action in FaultAction} == {
        "RAISE_TYPED_ERROR",
        "DELAY",
        "BLOCK_UNTIL_RELEASED",
        "RETURN_TYPED_FAILURE",
        "CORRUPT_TEST_FIXTURE",
    }
    assert {trigger.value for trigger in FaultTrigger} == {
        "ALWAYS",
        "FIRST_MATCH",
        "ON_NTH_MATCH",
        "AFTER_N_MATCHES",
    }
    assert {scope.value for scope in FaultScope} == {
        "GLOBAL_TEST_SCOPE",
        "RUN_SCOPE",
        "STEP_SCOPE",
        "INVOCATION_SCOPE",
        "ATTEMPT_SCOPE",
        "COMPONENT_SCOPE",
    }


def test_match_context_is_frozen_slotted_and_accepts_only_safe_identity() -> None:
    context = FaultMatchContext(
        fault_point=FaultPoint.TOOL_BEFORE_ATTEMPT,
        component="tool_execution",
        run_id_digest=DIGEST,
        step_id="step-1",
        invocation_id_digest="b" * 64,
        attempt_number=1,
        runtime_mode="COORDINATED",
        operation_kind="READ_ONLY",
    )
    assert not hasattr(context, "__dict__")
    with pytest.raises(FrozenInstanceError):
        context.component = "changed"
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        FaultMatchContext(
            fault_point=FaultPoint.TOOL_BEFORE_ATTEMPT,
            run_id_digest="raw-run-id",
        )
    with pytest.raises(ValueError, match="safe token"):
        FaultMatchContext(
            fault_point=FaultPoint.TOOL_BEFORE_ATTEMPT,
            step_id="C:\\private\\step",
        )


@pytest.mark.parametrize("field", ["max_hits", "match_number", "attempt_number"])
def test_count_fields_reject_bool(field: str) -> None:
    changes = {field: True}
    if field == "match_number":
        changes["trigger"] = FaultTrigger.ON_NTH_MATCH
    with pytest.raises(ValueError, match="positive integer"):
        rule(**changes)


def test_trigger_contract_requires_bounded_positive_counts() -> None:
    with pytest.raises(ValueError, match="requires match_number"):
        rule(trigger=FaultTrigger.ON_NTH_MATCH)
    with pytest.raises(ValueError, match="requires match_number"):
        rule(trigger=FaultTrigger.AFTER_N_MATCHES)
    with pytest.raises(ValueError, match="positive integer"):
        rule(max_hits=0)
    with pytest.raises(ValueError, match="FIRST_MATCH requires max_hits=1"):
        rule(trigger=FaultTrigger.FIRST_MATCH, max_hits=2)
    with pytest.raises(ValueError, match="ON_NTH_MATCH requires max_hits=1"):
        rule(
            trigger=FaultTrigger.ON_NTH_MATCH,
            match_number=2,
            max_hits=2,
        )


@pytest.mark.parametrize("priority", [True, -1, 1_000_001])
def test_priority_is_a_bounded_non_bool_integer(priority) -> None:
    with pytest.raises(ValueError, match="priority must be an integer"):
        rule(priority=priority)


@pytest.mark.parametrize("delay", [True, -1, float("inf"), float("nan")])
def test_delay_requires_finite_non_negative_non_bool_value(delay) -> None:
    with pytest.raises(ValueError, match="finite non-negative"):
        rule(
            action=FaultAction.DELAY,
            safe_fault_code=None,
            delay_seconds=delay,
        )
    assert (
        rule(
            action=FaultAction.DELAY,
            safe_fault_code=None,
            delay_seconds=0,
        ).delay_seconds
        == 0
    )


def test_action_parameters_are_typed_and_action_specific() -> None:
    with pytest.raises(ValueError, match="requires safe_fault_code"):
        rule(safe_fault_code=None)
    with pytest.raises(ValueError, match="only valid for DELAY"):
        rule(delay_seconds=0.1)
    with pytest.raises(ValueError, match="fixture_mutation is required"):
        rule(
            action=FaultAction.CORRUPT_TEST_FIXTURE,
            safe_fault_code=None,
        )


def test_post_commit_tool_fault_requires_explicit_dangerous_window() -> None:
    with pytest.raises(ValueError, match="dangerous_window=true"):
        rule(fault_point=FaultPoint.TOOL_AFTER_SIDE_EFFECT_COMMIT)
    value = rule(
        fault_point=FaultPoint.TOOL_AFTER_SIDE_EFFECT_COMMIT,
        dangerous_window=True,
    )
    assert value.dangerous_window is True


def test_fixed_dangerous_fault_point_set_is_explicit() -> None:
    assert DANGEROUS_FAULT_POINTS == {
        FaultPoint.MODEL_AFTER_PROVIDER_SUCCESS,
        FaultPoint.MODEL_AFTER_USAGE_COMMIT,
        FaultPoint.TOOL_AFTER_PROVIDER_RETURN,
        FaultPoint.TOOL_AFTER_SIDE_EFFECT_COMMIT,
        FaultPoint.TOOL_BEFORE_COMPLETION_EVENT,
        FaultPoint.EVENT_AFTER_JOURNAL_APPEND,
        FaultPoint.EVENT_BEFORE_CHANNEL_ENQUEUE,
        FaultPoint.SNAPSHOT_AFTER_SAVE,
        FaultPoint.EXECUTOR_AFTER_SUBMIT,
    }


def test_plan_is_immutable_normalized_unique_and_has_stable_safe_digest() -> None:
    first = rule(rule_id="rule-z")
    second = rule(
        rule_id="rule-a",
        fault_point=FaultPoint.RETRIEVAL_BEFORE_SEARCH,
    )
    plan = FaultPlan("plan-a", (first, second), created_at=NOW)
    same = FaultPlan("plan-a", (second, first), created_at=NOW)

    assert plan.schema_version == FAULT_PLAN_SCHEMA_VERSION
    assert tuple(item.rule_id for item in plan.rules) == ("rule-a", "rule-z")
    assert plan.digest == same.digest
    assert len(plan.digest) == 64
    assert plan.to_safe_json() == same.to_safe_json()
    assert "matches" not in repr(plan)
    assert "run_id_digest" not in repr(plan)
    with pytest.raises(FrozenInstanceError):
        plan.plan_id = "changed"
    with pytest.raises(ValueError, match="unique"):
        FaultPlan("plan-a", (first, first), created_at=NOW)


def test_plan_digest_excludes_created_at_but_includes_priority_and_rules() -> None:
    later = datetime(2026, 8, 1, tzinfo=UTC)
    base = FaultPlan("plan-a", (rule(),), created_at=NOW)
    same_semantics = FaultPlan("plan-a", (rule(),), created_at=later)
    changed_priority = FaultPlan(
        "plan-a",
        (rule(priority=999),),
        created_at=NOW,
    )
    assert base.digest == same_semantics.digest
    assert base.to_safe_dict()["created_at"] != same_semantics.to_safe_dict()["created_at"]
    assert "created_at" not in base.digest_source()
    assert base.digest != changed_priority.digest


def test_rule_has_no_mutable_runtime_state_slots() -> None:
    value = rule()
    names = set(value.__slots__)
    assert {"counter", "lock", "event", "task"}.isdisjoint(names)
    assert not hasattr(value, "__dict__")
