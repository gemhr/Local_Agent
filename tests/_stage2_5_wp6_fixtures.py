"""WP6 shared deterministic fault controllers and blocking fakes."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from core.runtime import (
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


NOW = datetime(2026, 8, 1, tzinfo=UTC)


def wp6_controller(
    point: FaultPoint,
    *,
    component: str | None = None,
    operation_kind: str | None = None,
    step_id: str | None = None,
    event_type: str | None = None,
    action: FaultAction = FaultAction.RAISE_TYPED_ERROR,
    blocker: FaultBlocker | None = None,
    max_hits: int = 1,
) -> FaultInjectionController:
    """Deterministic single-rule controller for WP6 run-scoped seams."""
    from core.runtime import DANGEROUS_FAULT_POINTS

    rule = FaultRule(
        rule_id="wp6-fault",
        fault_point=point,
        action=action,
        trigger=FaultTrigger.ALWAYS,
        scope=FaultScope.RUN_SCOPE,
        max_hits=max_hits,
        component=component,
        operation_kind=operation_kind,
        step_id=step_id,
        event_type=event_type.value if event_type is not None else None,
        safe_fault_code=(
            InjectedFaultCode.INJECTED_PERMANENT_FAILURE
            if action is FaultAction.RAISE_TYPED_ERROR
            else None
        ),
        dangerous_window=point in DANGEROUS_FAULT_POINTS,
    )
    return FaultInjectionController(
        FaultPlan("wp6-plan", (rule,), created_at=NOW),
        blockers={"wp6-fault": blocker} if blocker is not None else None,
    )


class GatedPlanningRouter:
    """Planning model that signals entry then blocks until released."""

    def __init__(
        self,
        planning_output: str,
        *,
        release: threading.Event | None = None,
    ) -> None:
        from tests._runtime_assembly_fixtures import FakeMemoryManager

        self._planning_output = planning_output
        self.entered = threading.Event()
        self.release = release or threading.Event()
        self.memory_manager = FakeMemoryManager()
        self.planning_calls = 0
        self.released = False

    def complete_planning_decision(self, user_request: str, **kwargs) -> str:
        self.planning_calls += 1
        self.entered.set()
        self.release.wait(timeout=10)
        self.released = True
        return self._planning_output

    def build_single_agent_plan(self, agent_id: str, query: str):
        from core.runtime import TaskCapabilityRequirements, create_single_step_plan

        return create_single_step_plan(agent_id, TaskCapabilityRequirements())

    def complete_single_agent(self, agent_id: str, query: str, **kwargs) -> str:
        return f"result-{agent_id}"


__all__ = [
    "GatedPlanningRouter",
    "wp6_controller",
]
