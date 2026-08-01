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


def diagnostic_controller(
    point: FaultPoint,
    *,
    component: str | None = None,
    event_type: str | None = None,
    action: FaultAction = FaultAction.RAISE_TYPED_ERROR,
    enabled: bool = True,
    max_hits: int = 1,
    delay_seconds: float = 1.0,
    sleeper=None,
    blocker: FaultBlocker | None = None,
) -> FaultInjectionController:
    rule = FaultRule(
        rule_id="diagnostic-fault",
        fault_point=point,
        action=action,
        trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.COMPONENT_SCOPE,
        max_hits=max_hits,
        component=component,
        event_type=event_type,
        safe_fault_code=(
            InjectedFaultCode.INJECTED_PERMANENT_FAILURE
            if action is FaultAction.RAISE_TYPED_ERROR
            else None
        ),
        delay_seconds=delay_seconds if action is FaultAction.DELAY else None,
        dangerous_window=point in DANGEROUS_FAULT_POINTS,
    )
    return FaultInjectionController(
        FaultPlan("diagnostic-plan", (rule,), created_at=NOW),
        enabled=enabled,
        sleeper=sleeper,
        blockers={"diagnostic-fault": blocker} if blocker is not None else None,
    )
