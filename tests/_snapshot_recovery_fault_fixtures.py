from __future__ import annotations

from datetime import UTC, datetime

from core.runtime import (
    DANGEROUS_FAULT_POINTS,
    FaultAction,
    FaultBlocker,
    FaultInjectionController,
    FaultPlan,
    FaultPoint,
    FaultRule,
    FaultScope,
    FaultTrigger,
    InjectedFaultCode,
)


NOW = datetime(2026, 1, 24, tzinfo=UTC)


def operation_controller(
    point: FaultPoint,
    *,
    action: FaultAction = FaultAction.RAISE_TYPED_ERROR,
    enabled: bool = True,
    max_hits: int = 1,
    sleeper=None,
    blocker: FaultBlocker | None = None,
    fixture_mutator=None,
) -> FaultInjectionController:
    component = (
        "checkpoint_coordinator"
        if point in {FaultPoint.SNAPSHOT_BEFORE_SAVE, FaultPoint.SNAPSHOT_AFTER_SAVE}
        else "recovery_validator"
    )
    rule = FaultRule(
        rule_id="operation-fault",
        fault_point=point,
        action=action,
        trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.COMPONENT_SCOPE,
        max_hits=max_hits,
        component=component,
        safe_fault_code=(
            InjectedFaultCode.INJECTED_STORE_FAILURE
            if action is FaultAction.RAISE_TYPED_ERROR
            else None
        ),
        delay_seconds=1.0 if action is FaultAction.DELAY else None,
        fixture_mutation=(
            "MUTATE_TEST_FIXTURE"
            if action is FaultAction.CORRUPT_TEST_FIXTURE
            else None
        ),
        dangerous_window=point in DANGEROUS_FAULT_POINTS,
    )
    return FaultInjectionController(
        FaultPlan("snapshot-recovery-plan", (rule,), created_at=NOW),
        enabled=enabled,
        sleeper=sleeper,
        blockers={"operation-fault": blocker} if blocker is not None else None,
        fixture_mutator=fixture_mutator,
    )
