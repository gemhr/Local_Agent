#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""基于不可变 Plan 和 AgentState 的最小串行调度器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

from core.runtime.planning import (
    Plan,
    PlanStep,
    TaskCapabilityRequirements,
)
from core.runtime.plan_graph import PlanGraph, PlanGraphValidator
from core.runtime.state import AgentState, RunStatus, StepStatus
from core.runtime.state_machine import AgentStateMachine, StepEventType, StepStateEvent

_BLOCKING_DEPENDENCY_STATUSES = frozenset(
    {
        StepStatus.FAILED,
        StepStatus.CANCELLED,
        StepStatus.BLOCKED,
        StepStatus.SKIPPED,
    }
)
_TERMINAL_STEP_STATUSES = frozenset(
    {
        StepStatus.SUCCEEDED,
        StepStatus.FAILED,
        StepStatus.CANCELLED,
        StepStatus.BLOCKED,
        StepStatus.SKIPPED,
    }
)


class SchedulerError(RuntimeError):
    """Scheduler 边界内不包含用户正文的安全异常。"""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        plan_id: str,
        plan_version: int,
        step_id: str | None,
        current_state: str,
    ) -> None:
        self.error_code = error_code
        self.plan_id = plan_id
        self.plan_version = plan_version
        self.step_id = step_id
        self.current_state = current_state
        self.safe_message = message
        super().__init__(
            f"{message} (error_code={error_code}, plan_id={plan_id}, "
            f"plan_version={plan_version}, step_id={step_id or '-'}, "
            f"current_state={current_state})"
        )


class SchedulerPlanStateMismatchError(SchedulerError):
    """Plan 与当前 Scheduler/AgentState 绑定不一致时引发。"""

    def __init__(
        self,
        *,
        plan: Plan,
        step_id: str | None,
        current_state: str,
        message: str = "Plan 与当前执行状态不一致",
    ) -> None:
        super().__init__(
            error_code="SCHEDULER_PLAN_STATE_MISMATCH",
            message=message,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            step_id=step_id,
            current_state=current_state,
        )


class SchedulerClaimError(SchedulerError):
    """State Machine 未能安全完成 Step Claim 时引发。"""

    def __init__(self, *, plan: Plan, step_id: str, current_state: str) -> None:
        super().__init__(
            error_code="SCHEDULER_STEP_CLAIM_FAILED",
            message="步骤认领失败，执行状态未产生有效 Claim",
            plan_id=plan.plan_id,
            plan_version=plan.version,
            step_id=step_id,
            current_state=current_state,
        )


@dataclass(frozen=True, slots=True)
class StepClaim:
    """成功发送 STARTED 后交给执行层的不可变调度凭据。"""

    plan_id: str
    plan_version: int
    step_id: str
    claimed_at: datetime
    capability_requirements: TaskCapabilityRequirements
    preferred_agent: str

    def __post_init__(self) -> None:
        if self.claimed_at.tzinfo is None or self.claimed_at.utcoffset() != timedelta(
            0
        ):
            raise ValueError("claimed_at 必须是带时区的 UTC 时间")


@dataclass(frozen=True, slots=True)
class SchedulerSnapshot:
    """Plan 与 AgentState 的不可变派生调度视图。"""

    ready_step_ids: tuple[str, ...]
    running_step_ids: tuple[str, ...]
    pending_step_ids: tuple[str, ...]
    blocked_step_ids: tuple[str, ...]
    terminal_step_ids: tuple[str, ...]
    is_complete: bool
    is_waiting: bool
    has_unresolved_pending: bool


class SerialScheduler:
    """在单进程单实例锁内注册、评估并认领一个 Plan Step。"""

    def __init__(self, state_machine: AgentStateMachine | None = None) -> None:
        self._state_machine = state_machine or AgentStateMachine()
        self._claim_lock = Lock()
        self._binding: tuple[str, str, int, tuple[object, ...]] | None = None

    def prepare(
        self, plan: Plan, state: AgentState, occurred_at: datetime
    ) -> SchedulerSnapshot:
        """注册尚不存在的 Plan Step，并返回传播 BLOCKED 后的快照。"""
        self._validate_utc(occurred_at)
        with self._claim_lock:
            graph = self._prepare_locked(plan, state)
            effective_at = self._effective_time(occurred_at, state)
            self._propagate_blocked(plan, graph, state, effective_at)
            return self._build_snapshot(plan, graph, state)

    def evaluate(self, plan: Plan, state: AgentState) -> SchedulerSnapshot:
        """传播可确定的 BLOCKED，并动态计算当前调度快照。"""
        with self._claim_lock:
            graph = PlanGraphValidator.validate(plan)
            self._validate_plan_state_alignment(plan, state)
            occurred_at = self._effective_time(datetime.now(UTC), state)
            self._propagate_blocked(plan, graph, state, occurred_at)
            return self._build_snapshot(plan, graph, state)

    def claim_next(
        self,
        plan: Plan,
        state: AgentState,
        occurred_at: datetime,
    ) -> StepClaim | None:
        """在同一锁内准备、传播、计算并通过 STARTED 原子认领一个 Step。"""
        self._validate_utc(occurred_at)
        with self._claim_lock:
            graph = self._prepare_locked(plan, state)
            effective_at = self._effective_time(occurred_at, state)
            self._propagate_blocked(plan, graph, state, effective_at)
            if self._running_steps(plan, state):
                return None
            ready_steps = self._compute_ready_steps(plan, graph, state)
            if not ready_steps:
                return None

            step = ready_steps[0]
            claim_at = self._effective_time(effective_at, state)
            try:
                self._state_machine.apply_step_event(
                    state,
                    StepStateEvent(
                        StepEventType.STARTED, step.step_id, occurred_at=claim_at
                    ),
                )
            except Exception as exc:
                current = state.steps.get(step.step_id)
                raise SchedulerClaimError(
                    plan=plan,
                    step_id=step.step_id,
                    current_state=(
                        current.status.value if current is not None else "MISSING"
                    ),
                ) from exc

            claimed = state.steps[step.step_id]
            if (
                claimed.status != StepStatus.RUNNING
                or step.step_id not in state.active_step_ids
            ):
                raise SchedulerClaimError(
                    plan=plan,
                    step_id=step.step_id,
                    current_state=claimed.status.value,
                )
            return StepClaim(
                plan_id=plan.plan_id,
                plan_version=plan.version,
                step_id=step.step_id,
                claimed_at=claimed.started_at or claim_at,
                capability_requirements=step.capability_requirements,
                preferred_agent=step.preferred_agent,
            )

    def _prepare_locked(self, plan: Plan, state: AgentState) -> PlanGraph:
        # 必须在读取或修改 Runtime 状态前完成静态图校验。
        graph = PlanGraphValidator.validate(plan)
        state.validate()
        self._validate_binding(plan, state)
        if state.status != RunStatus.RUNNING:
            raise SchedulerPlanStateMismatchError(
                plan=plan,
                step_id=None,
                current_state=state.status.value,
                message="只有 RUNNING Run 可以准备调度步骤",
            )

        # 先完整预检，避免名称冲突时只注册部分 Plan。
        for step in plan.steps:
            existing = state.steps.get(step.step_id)
            if existing is not None and existing.name != step.title:
                raise SchedulerPlanStateMismatchError(
                    plan=plan,
                    step_id=step.step_id,
                    current_state=existing.status.value,
                    message="相同步骤标识对应的名称不一致",
                )
        for step in plan.steps:
            if step.step_id not in state.steps:
                self._state_machine.add_step(
                    state, step_id=step.step_id, name=step.title
                )
        self._binding = self._binding_value(plan, state)
        return graph

    def _validate_plan_state_alignment(self, plan: Plan, state: AgentState) -> None:
        state.validate()
        self._validate_binding(plan, state)
        for step in plan.steps:
            existing = state.steps.get(step.step_id)
            if existing is None:
                raise SchedulerPlanStateMismatchError(
                    plan=plan,
                    step_id=step.step_id,
                    current_state="MISSING",
                    message="Plan Step 尚未注册到 AgentState",
                )
            if existing.name != step.title:
                raise SchedulerPlanStateMismatchError(
                    plan=plan,
                    step_id=step.step_id,
                    current_state=existing.status.value,
                    message="相同步骤标识对应的名称不一致",
                )

    def _validate_binding(self, plan: Plan, state: AgentState) -> None:
        if self._binding is None:
            return
        if self._binding != self._binding_value(plan, state):
            raise SchedulerPlanStateMismatchError(
                plan=plan,
                step_id=None,
                current_state=state.status.value,
                message="Scheduler 实例已绑定其他 Run 或 Plan 版本",
            )

    @staticmethod
    def _binding_value(
        plan: Plan, state: AgentState
    ) -> tuple[str, str, int, tuple[object, ...]]:
        scheduling_signature: tuple[object, ...] = tuple(
            (
                step.step_id,
                step.title,
                step.depends_on,
                step.preferred_agent,
                step.capability_requirements,
            )
            for step in plan.steps
        )
        return state.run_id, plan.plan_id, plan.version, scheduling_signature

    def _propagate_blocked(
        self, plan: Plan, graph: PlanGraph, state: AgentState, occurred_at: datetime
    ) -> None:
        if state.status != RunStatus.RUNNING:
            return
        changed = True
        while changed:
            changed = False
            for step_id in graph.topological_order:
                current = state.steps[step_id]
                if current.status != StepStatus.PENDING:
                    continue
                dependency_statuses = (
                    state.steps[dependency_id].status
                    for dependency_id in graph.dependencies_of(step_id)
                )
                if not any(
                    status in _BLOCKING_DEPENDENCY_STATUSES
                    for status in dependency_statuses
                ):
                    continue
                event_at = self._effective_time(occurred_at, state)
                self._state_machine.apply_step_event(
                    state,
                    StepStateEvent(
                        StepEventType.BLOCKED,
                        step_id,
                        occurred_at=event_at,
                        error_code="DEPENDENCY_NOT_SUCCESSFUL",
                        error_message="前置步骤未成功，当前步骤无法执行",
                    ),
                )
                changed = True

    def _compute_ready_steps(
        self, plan: Plan, graph: PlanGraph, state: AgentState
    ) -> tuple[PlanStep, ...]:
        if state.status != RunStatus.RUNNING or self._running_steps(plan, state):
            return ()
        ready: list[PlanStep] = []
        steps_by_id = {step.step_id: step for step in plan.steps}
        for step_id in graph.topological_order:
            step = steps_by_id[step_id]
            current = state.steps[step_id]
            if (
                current.status != StepStatus.PENDING
                or step.step_id in state.active_step_ids
            ):
                continue
            if all(
                state.steps[dependency_id].status == StepStatus.SUCCEEDED
                for dependency_id in graph.dependencies_of(step_id)
            ):
                ready.append(step)
        return tuple(ready)

    @staticmethod
    def _running_steps(plan: Plan, state: AgentState) -> tuple[PlanStep, ...]:
        return tuple(
            step
            for step in plan.steps
            if state.steps[step.step_id].status == StepStatus.RUNNING
        )

    def _build_snapshot(self, plan: Plan, graph: PlanGraph, state: AgentState) -> SchedulerSnapshot:
        ready_step_ids = tuple(
            step.step_id for step in self._compute_ready_steps(plan, graph, state)
        )
        ordered_step_ids = graph.topological_order
        running_step_ids = tuple(
            step_id for step_id in ordered_step_ids if state.steps[step_id].status == StepStatus.RUNNING
        )
        pending_step_ids = tuple(
            step_id for step_id in ordered_step_ids if state.steps[step_id].status == StepStatus.PENDING
        )
        blocked_step_ids = tuple(
            step_id for step_id in ordered_step_ids if state.steps[step_id].status == StepStatus.BLOCKED
        )
        terminal_step_ids = tuple(
            step_id for step_id in ordered_step_ids if state.steps[step_id].status in _TERMINAL_STEP_STATUSES
        )
        is_complete = all(
            state.steps[step_id].status == StepStatus.SUCCEEDED
            for step_id in ordered_step_ids
        )
        is_waiting = bool(running_step_ids) and not ready_step_ids
        has_unresolved_pending = (
            bool(pending_step_ids) and not running_step_ids and not ready_step_ids
        )
        return SchedulerSnapshot(
            ready_step_ids=ready_step_ids,
            running_step_ids=running_step_ids,
            pending_step_ids=pending_step_ids,
            blocked_step_ids=blocked_step_ids,
            terminal_step_ids=terminal_step_ids,
            is_complete=is_complete,
            is_waiting=is_waiting,
            has_unresolved_pending=has_unresolved_pending,
        )

    @staticmethod
    def _effective_time(occurred_at: datetime, state: AgentState) -> datetime:
        return max(occurred_at, state.updated_at)

    @staticmethod
    def _validate_utc(value: datetime) -> None:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("occurred_at 必须是带时区的 UTC 时间")
