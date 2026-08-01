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
    RuntimeEventDraft,
    RuntimeEventType,
    RunStartedPayload,
)


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def event_controller(
    point: FaultPoint,
    *,
    action: FaultAction = FaultAction.RAISE_TYPED_ERROR,
    event_type: RuntimeEventType | None = None,
    max_hits: int = 1,
    enabled: bool = True,
    sleeper=None,
    blocker: FaultBlocker | None = None,
) -> FaultInjectionController:
    rule = FaultRule(
        rule_id="event-fault",
        fault_point=point,
        action=action,
        trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.RUN_SCOPE,
        max_hits=max_hits,
        component="event_channel",
        event_type=event_type.value if event_type is not None else None,
        safe_fault_code=(
            InjectedFaultCode.INJECTED_JOURNAL_FAILURE
            if action is FaultAction.RAISE_TYPED_ERROR
            else None
        ),
        delay_seconds=1.0 if action is FaultAction.DELAY else None,
        dangerous_window=point in DANGEROUS_FAULT_POINTS,
    )
    return FaultInjectionController(
        FaultPlan("event-plan", (rule,), created_at=NOW),
        enabled=enabled,
        sleeper=sleeper,
        blockers={"event-fault": blocker} if blocker is not None else None,
    )


def run_started_draft(run_id: str = "run-a") -> RuntimeEventDraft:
    return RuntimeEventDraft(
        run_id=run_id,
        trace_id=f"trace-{run_id}",
        event_type=RuntimeEventType.RUN_STARTED,
        component="test",
        payload=RunStartedPayload("RUNNING"),
    )
