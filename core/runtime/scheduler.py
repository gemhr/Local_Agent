#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""基于不可变 Plan 和 AgentState 的最小串行调度器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

from core.runtime.budget import BudgetLedger, BudgetUsage, BudgetReservation
from core.runtime.claim_gate import SchedulerClaimGate
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


class SchedulerPartialClaimError(SchedulerError):
    """批量认领中途失败时保留已成功 Claim 的安全异常。"""

    def __init__(self, *, plan: Plan, succeeded_step_ids: tuple[str, ...], step_id: str, current_state: str) -> None:
        self.succeeded_step_ids = succeeded_step_ids
        super().__init__(error_code="SCHEDULER_PARTIAL_CLAIM_FAILED", message="批量步骤认领部分成功", plan_id=plan.plan_id, plan_version=plan.version, step_id=step_id, current_state=current_state)


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
    claimable_step_ids: tuple[str, ...] = ()
    max_parallelism: int = 1
    available_slots: int = 0


class SerialScheduler:
    """在单进程单实例锁内注册、评估并认领一个 Plan Step。"""

    def __init__(
        self,
        state_machine: AgentStateMachine | None = None,
        *,
        claim_gate: SchedulerClaimGate | None = None,
    ) -> None:
        self._state_machine = state_machine or AgentStateMachine()
        self.claim_gate = claim_gate or SchedulerClaimGate()
        self._claim_lock = Lock()
        self._binding: tuple[str, str, int, tuple[object, ...]] | None = None

    def prepare(
        self, plan: Plan, state: AgentState, occurred_at: datetime, max_parallelism: int = 1
    ) -> SchedulerSnapshot:
        """注册尚不存在的 Plan Step，并返回传播 BLOCKED 后的快照。"""
        self._validate_utc(occurred_at)
        self._validate_parallelism(max_parallelism)
        with self._claim_lock:
            with state.runtime_lock:
                graph = self._prepare_locked(plan, state)
                effective_at = self._effective_time(occurred_at, state)
                self._propagate_blocked(plan, graph, state, effective_at)
                return self._build_snapshot(plan, graph, state, max_parallelism)

    def evaluate(self, plan: Plan, state: AgentState, max_parallelism: int = 1) -> SchedulerSnapshot:
        """传播可确定的 BLOCKED，并动态计算当前调度快照。"""
        self._validate_parallelism(max_parallelism)
        with self._claim_lock:
            with state.runtime_lock:
                graph = PlanGraphValidator.validate(plan)
                self._validate_plan_state_alignment(plan, state)
                occurred_at = self._effective_time(datetime.now(UTC), state)
                self._propagate_blocked(plan, graph, state, occurred_at)
                return self._build_snapshot(plan, graph, state, max_parallelism)

    def claim_next(self, plan: Plan, state: AgentState, occurred_at: datetime) -> StepClaim | None:
        """兼容串行入口，委托给批量认领接口。"""
        claims = self.claim_ready(plan, state, max_parallelism=1, occurred_at=occurred_at)
        return claims[0] if claims else None

    def claim_ready(self, plan: Plan, state: AgentState, max_parallelism: int, occurred_at: datetime, *, budget_ledger: BudgetLedger | None = None) -> tuple[StepClaim, ...]:
        """在同一锁内按稳定拓扑顺序认领不超过容量的全部 Ready Step。"""
        self._validate_parallelism(max_parallelism)
        self._validate_utc(occurred_at)
        with self.claim_gate.claim() as entered:
            if not entered:
                return ()
            return self._claim_ready_entered(
                plan,
                state,
                max_parallelism,
                occurred_at,
                budget_ledger=budget_ledger,
            )

    def _claim_ready_entered(
        self,
        plan: Plan,
        state: AgentState,
        max_parallelism: int,
        occurred_at: datetime,
        *,
        budget_ledger: BudgetLedger | None,
    ) -> tuple[StepClaim, ...]:
        """Recheck claimability and commit RUNNING inside the claim gate."""
        with self._claim_lock:
            with state.runtime_lock:
                graph = self._prepare_locked(plan, state)
                effective_at = self._effective_time(occurred_at, state)
                self._propagate_blocked(plan, graph, state, effective_at)
                running_count = len(self._running_steps(plan, state))
                available_slots = max(0, max_parallelism - running_count)
                candidates = self._compute_ready_steps(plan, graph, state)[:available_slots]
                reservation: BudgetReservation | None = None
                if budget_ledger is not None:
                    # 预算预留在任何 STARTED 之前完成，避免并发 check-then-act。
                    reservation = budget_ledger.reserve(BudgetUsage(step_starts=len(candidates)), reservation_type="step_start")
                # 在发送任何 STARTED 前完成所有候选的预检。
                for step in candidates:
                    current = state.steps[step.step_id]
                    if current.status != StepStatus.PENDING or step.step_id in state.active_step_ids:
                        raise SchedulerClaimError(plan=plan, step_id=step.step_id, current_state=current.status.value)
                claims: list[StepClaim] = []
                for step in candidates:
                    claim_at = self._effective_time(effective_at, state)
                    try:
                        self._state_machine.apply_step_event(state, StepStateEvent(StepEventType.STARTED, step.step_id, occurred_at=claim_at))
                    except Exception as exc:
                        if reservation is not None:
                            if claims:
                                budget_ledger.commit(reservation, BudgetUsage(step_starts=len(claims)))
                            else:
                                budget_ledger.release(reservation)
                        current = state.steps.get(step.step_id)
                        if claims:
                            raise SchedulerPartialClaimError(plan=plan, succeeded_step_ids=tuple(item.step_id for item in claims), step_id=step.step_id, current_state=current.status.value if current else "MISSING") from exc
                        raise SchedulerClaimError(plan=plan, step_id=step.step_id, current_state=current.status.value if current else "MISSING") from exc
                    claimed = state.steps[step.step_id]
                    if claimed.status != StepStatus.RUNNING or step.step_id not in state.active_step_ids:
                        if reservation is not None:
                            if claims:
                                budget_ledger.commit(reservation, BudgetUsage(step_starts=len(claims)))
                            else:
                                budget_ledger.release(reservation)
                        raise SchedulerPartialClaimError(plan=plan, succeeded_step_ids=tuple(item.step_id for item in claims), step_id=step.step_id, current_state=claimed.status.value)
                    claims.append(StepClaim(plan.plan_id, plan.version, step.step_id, claimed.started_at or claim_at, step.capability_requirements, step.preferred_agent))
                if reservation is not None:
                    budget_ledger.commit(reservation, BudgetUsage(step_starts=len(claims)))
                return tuple(claims)

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
        if state.status != RunStatus.RUNNING:
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

    def _build_snapshot(self, plan: Plan, graph: PlanGraph, state: AgentState, max_parallelism: int = 1) -> SchedulerSnapshot:
        ready_step_ids = tuple(
            step.step_id for step in self._compute_ready_steps(plan, graph, state)
        )
        available_slots = max(0, max_parallelism - len(self._running_steps(plan, state)))
        claimable_step_ids = ready_step_ids[:available_slots]
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
            claimable_step_ids=claimable_step_ids,
            max_parallelism=max_parallelism,
            available_slots=available_slots,
            is_complete=is_complete,
            is_waiting=is_waiting,
            has_unresolved_pending=has_unresolved_pending,
        )

    @staticmethod
    def _validate_parallelism(value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("max_parallelism 必须是正整数")

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
