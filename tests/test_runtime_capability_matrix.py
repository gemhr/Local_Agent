from __future__ import annotations

from core.runtime import (
    FaultInjectionController,
    FaultAction,
    FaultMatchContext,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InMemoryRunEventJournal,
    RecoveryStatus,
    RecoveryValidator,
    RunRegistry,
    InjectedFaultCode,
    NO_FAULT_DECISION,
)
from datetime import UTC, datetime
from tests._recovery_fixtures import recovery_plan, recovery_snapshot


def test_recovery_capability_is_validation_only_not_execution_or_replay() -> None:
    assessment = RecoveryValidator(
        journal=InMemoryRunEventJournal()
    ).assess_snapshot(
        snapshot=recovery_snapshot(),
        current_plan=recovery_plan(),
    )

    assert assessment.status is RecoveryStatus.RESUMABLE
    assert assessment.resume_prerequisites_satisfied is True
    assert assessment.automatic_resume_supported is False
    assert assessment.model_replay_allowed is False
    assert assessment.tool_replay_allowed is False
    assert assessment.retrieval_replay_allowed is False
    assert assessment.output_reconstruction_supported is False


def test_random_chaos_and_cross_process_registry_are_not_implicit_capabilities() -> None:
    assert "RANDOM" not in {item.name for item in FaultTrigger}
    first = RunRegistry()
    second = RunRegistry()
    assert first is not second
    assert first.observability_snapshot()["active_runs"] == 0
    assert second.observability_snapshot()["active_runs"] == 0


def test_deterministic_fault_injection_is_supported_only_by_explicit_test_scope() -> None:
    plan = FaultPlan(
        "deterministic-test-plan",
        (
            FaultRule(
                "one-shot",
                FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
                action=FaultAction.RAISE_TYPED_ERROR,
                trigger=FaultTrigger.FIRST_MATCH,
                scope=FaultScope.INVOCATION_SCOPE,
                max_hits=1,
                component="model",
                safe_fault_code=InjectedFaultCode.INJECTED_TRANSIENT_FAILURE,
            ),
        ),
        created_at=datetime(2026, 8, 2, tzinfo=UTC),
    )
    controller = FaultInjectionController.for_test(plan)

    context = FaultMatchContext(
        fault_point=FaultPoint.MODEL_BEFORE_PROVIDER_CALL,
        component="model",
    )
    first = controller.evaluate(context)
    second = controller.evaluate(context)

    assert first is not None
    assert first.rule_id == "one-shot"
    assert second is NO_FAULT_DECISION
    assert "RANDOM" not in {item.name for item in FaultTrigger}
    controller.close()
