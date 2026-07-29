#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AgentState 的最小同步内存状态机。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
import re
from types import MappingProxyType

from core.runtime.state import AgentState, RunStatus, StepStatus, StopReason


_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CANCELLATION_REASONS = frozenset({
    StopReason.USER_CANCELLED,
    StopReason.CLIENT_DISCONNECTED,
    StopReason.SYSTEM_SHUTDOWN,
})
_FAILURE_REASONS = frozenset(set(StopReason) - _CANCELLATION_REASONS - {StopReason.COMPLETED})
_TERMINAL_RUN_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})
_TERMINAL_STEP_STATUSES = frozenset({
    StepStatus.SUCCEEDED,
    StepStatus.FAILED,
    StepStatus.CANCELLED,
    StepStatus.BLOCKED,
    StepStatus.SKIPPED,
})


class RunEventType(str, Enum):
    """Run 生命周期支持的状态事件。"""

    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    MAX_STEPS_REACHED = "MAX_STEPS_REACHED"
    NO_ACTION = "NO_ACTION"
    REPEATED_ACTION = "REPEATED_ACTION"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CANCELLED = "CANCELLED"


class StepEventType(str, Enum):
    """Step 生命周期支持的状态事件。"""

    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


_SPECIAL_FAILURE_REASONS = MappingProxyType({
    RunEventType.DEADLINE_EXCEEDED: StopReason.DEADLINE_EXCEEDED,
    RunEventType.MAX_STEPS_REACHED: StopReason.MAX_STEPS_REACHED,
    RunEventType.NO_ACTION: StopReason.NO_ACTION,
    RunEventType.REPEATED_ACTION: StopReason.REPEATED_ACTION,
    RunEventType.BUDGET_EXHAUSTED: StopReason.BUDGET_EXHAUSTED,
})


def _utc_now() -> datetime:
    """返回事件默认使用的带时区 UTC 时间。"""
    return datetime.now(UTC)


def _validate_utc_datetime(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("occurred_at 必须是带时区的 UTC datetime")


def _validate_error_code(value: str | None, *, required: bool) -> None:
    if value is None:
        if required:
            raise ValueError("失败或取消事件必须包含安全 error_code")
        return
    if not isinstance(value, str) or not value.strip() or not _SAFE_ERROR_CODE.fullmatch(value):
        raise ValueError("error_code 必须是非空安全标识符")


def _validate_error_message(value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError("error_message 提供时必须是非空字符串")
    if len(value) > 500 or "\n" in value or "\r" in value or "traceback" in value.casefold():
        raise ValueError("error_message 必须是单行安全摘要且不得包含 traceback")


@dataclass(frozen=True)
class RunStateEvent:
    """不包含原始异常或自由 payload 的 Run 状态事件。"""

    event_type: RunEventType
    occurred_at: datetime = field(default_factory=_utc_now)
    stop_reason: StopReason | None = None
    final_output: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        """在事件进入状态机前校验字段组合。"""
        if not isinstance(self.event_type, RunEventType):
            raise ValueError("event_type 必须是 RunEventType")
        _validate_utc_datetime(self.occurred_at)
        if self.stop_reason is not None and not isinstance(self.stop_reason, StopReason):
            raise ValueError("stop_reason 必须是 StopReason")
        if self.final_output is not None and not isinstance(self.final_output, str):
            raise ValueError("final_output 提供时必须是字符串")
        _validate_error_message(self.error_message)

        if self.event_type == RunEventType.STARTED:
            if any(
                value is not None
                for value in (self.stop_reason, self.final_output, self.error_code, self.error_message)
            ):
                raise ValueError("STARTED 事件不得携带终态字段")
            return

        if self.event_type == RunEventType.COMPLETED:
            if self.stop_reason != StopReason.COMPLETED:
                raise ValueError("COMPLETED 事件必须使用 COMPLETED stop_reason")
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("COMPLETED 事件不得携带错误信息")
            return

        if self.final_output is not None:
            raise ValueError("只有 COMPLETED 事件可以携带 final_output")

        if self.event_type == RunEventType.CANCELLED:
            if self.stop_reason not in _CANCELLATION_REASONS:
                raise ValueError("CANCELLED 事件必须使用合法取消 StopReason")
            _validate_error_code(self.error_code, required=True)
            return

        if self.stop_reason not in _FAILURE_REASONS:
            raise ValueError("失败类事件必须使用合法失败 StopReason")
        expected_reason = _SPECIAL_FAILURE_REASONS.get(self.event_type)
        if expected_reason is not None and self.stop_reason != expected_reason:
            raise ValueError("专用失败事件必须使用对应 StopReason")
        _validate_error_code(self.error_code, required=True)


@dataclass(frozen=True)
class StepStateEvent:
    """不包含原始异常或自由 payload 的 Step 状态事件。"""

    event_type: StepEventType
    step_id: str
    occurred_at: datetime = field(default_factory=_utc_now)
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        """在事件进入状态机前校验字段组合。"""
        if not isinstance(self.event_type, StepEventType):
            raise ValueError("event_type 必须是 StepEventType")
        if not isinstance(self.step_id, str) or not self.step_id.strip():
            raise ValueError("step_id 不得为空")
        _validate_utc_datetime(self.occurred_at)
        _validate_error_message(self.error_message)

        if self.event_type in {StepEventType.STARTED, StepEventType.SUCCEEDED, StepEventType.SKIPPED}:
            if self.error_code is not None or self.error_message is not None:
                raise ValueError(f"{self.event_type.value} Step 事件不得携带错误信息")
            return
        if self.event_type == StepEventType.FAILED:
            if self.error_code is None and self.error_message is None:
                raise ValueError("FAILED Step 事件必须包含安全错误信息")
            _validate_error_code(self.error_code, required=False)
            return
        _validate_error_code(self.error_code, required=False)


class InvalidStateTransitionError(ValueError):
    """状态、事件或 Guard 不允许转移时引发的安全异常。"""

    def __init__(
        self,
        *,
        entity_type: str,
        current_status: str,
        event_type: str,
        entity_id: str,
        reason: str,
    ) -> None:
        self.entity_type = entity_type
        self.current_status = current_status
        self.event_type = event_type
        self.entity_id = entity_id
        self.reason = reason
        super().__init__(
            f"非法 {entity_type} 状态转移: id={entity_id}, "
            f"status={current_status}, event={event_type}, reason={reason}"
        )


class AgentStateMachine:
    """校验事件和 Guard，并原子更新调用方提供的 AgentState。"""

    _RUN_TRANSITIONS = MappingProxyType({
        (RunStatus.CREATED, RunEventType.STARTED): RunStatus.RUNNING,
        (RunStatus.CREATED, RunEventType.FAILED): RunStatus.FAILED,
        (RunStatus.CREATED, RunEventType.CANCELLED): RunStatus.CANCELLED,
        (RunStatus.RUNNING, RunEventType.COMPLETED): RunStatus.SUCCEEDED,
        (RunStatus.RUNNING, RunEventType.FAILED): RunStatus.FAILED,
        (RunStatus.RUNNING, RunEventType.DEADLINE_EXCEEDED): RunStatus.FAILED,
        (RunStatus.RUNNING, RunEventType.MAX_STEPS_REACHED): RunStatus.FAILED,
        (RunStatus.RUNNING, RunEventType.NO_ACTION): RunStatus.FAILED,
        (RunStatus.RUNNING, RunEventType.REPEATED_ACTION): RunStatus.FAILED,
        (RunStatus.RUNNING, RunEventType.BUDGET_EXHAUSTED): RunStatus.FAILED,
        (RunStatus.RUNNING, RunEventType.CANCELLED): RunStatus.CANCELLED,
    })
    _STEP_TRANSITIONS = MappingProxyType({
        (StepStatus.PENDING, StepEventType.STARTED): StepStatus.RUNNING,
        (StepStatus.PENDING, StepEventType.CANCELLED): StepStatus.CANCELLED,
        (StepStatus.PENDING, StepEventType.BLOCKED): StepStatus.BLOCKED,
        (StepStatus.PENDING, StepEventType.SKIPPED): StepStatus.SKIPPED,
        (StepStatus.RUNNING, StepEventType.SUCCEEDED): StepStatus.SUCCEEDED,
        (StepStatus.RUNNING, StepEventType.FAILED): StepStatus.FAILED,
        (StepStatus.RUNNING, StepEventType.CANCELLED): StepStatus.CANCELLED,
    })

    def add_step(self, state: AgentState, *, step_id: str, name: str) -> None:
        """在 RUNNING Run 中原子注册一个 PENDING Step。"""
        with state.runtime_lock:
            state.validate()
            if state.status != RunStatus.RUNNING:
                self._raise_run(state, "ADD_STEP", "只有 RUNNING Run 可以注册 Step")
            self._register_step_locked(state, step_id=step_id, name=name)

    def register_plan_step(
        self, state: AgentState, *, step_id: str, name: str
    ) -> None:
        """Register immutable Plan structure before the Run starts or while running."""
        with state.runtime_lock:
            state.validate()
            if state.status not in {RunStatus.CREATED, RunStatus.RUNNING}:
                self._raise_run(
                    state,
                    "REGISTER_PLAN_STEP",
                    "只有 CREATED/RUNNING Run 可以注册 Plan Step",
                )
            self._register_step_locked(state, step_id=step_id, name=name)

    def _register_step_locked(
        self, state: AgentState, *, step_id: str, name: str
    ) -> None:
        candidate = self._clone(state)
        candidate.add_step(step_id, name)
        self._commit(state, candidate)

    def apply_run_event(self, state: AgentState, event: RunStateEvent) -> None:
        """校验并原子应用一个 Run 状态事件。"""
        with state.runtime_lock:
            self._apply_run_event_locked(state, event)

    def _apply_run_event_locked(self, state: AgentState, event: RunStateEvent) -> None:
        state.validate()
        if state.status in _TERMINAL_RUN_STATUSES:
            self._raise_run(state, event.event_type.value, "终态 Run 拒绝所有后续事件")
        target_status = self._RUN_TRANSITIONS.get((state.status, event.event_type))
        if target_status is None:
            self._raise_run(state, event.event_type.value, "当前状态不允许该事件")
        self._guard_event_time(state, event.occurred_at, "Run", state.run_id, event.event_type.value)
        if target_status in _TERMINAL_RUN_STATUSES:
            self._guard_run_has_no_active_steps(state, event.event_type.value)

        candidate = self._clone(state)
        candidate.status = target_status
        candidate.updated_at = event.occurred_at
        if event.event_type == RunEventType.STARTED:
            candidate.stop_reason = None
            candidate.final_output = None
            candidate.error_code = None
            candidate.error_message = None
        elif event.event_type == RunEventType.COMPLETED:
            candidate.stop_reason = StopReason.COMPLETED
            candidate.final_output = event.final_output
            candidate.error_code = None
            candidate.error_message = None
        else:
            candidate.stop_reason = event.stop_reason
            candidate.final_output = None
            candidate.error_code = event.error_code
            candidate.error_message = event.error_message
        candidate.validate()
        self._commit(state, candidate)

    def apply_step_event(self, state: AgentState, event: StepStateEvent) -> None:
        """校验并原子应用一个 Step 状态事件。"""
        with state.runtime_lock:
            self._apply_step_event_locked(state, event)

    def _apply_step_event_locked(self, state: AgentState, event: StepStateEvent) -> None:
        step = state.steps.get(event.step_id)
        if (
            step is not None
            and step.status == StepStatus.RUNNING
            and event.event_type in {StepEventType.SUCCEEDED, StepEventType.FAILED, StepEventType.CANCELLED}
            and event.step_id not in state.active_step_ids
        ):
            self._raise_step(state, event, "RUNNING Step 必须位于 active 集合")
        state.validate()
        if state.status in _TERMINAL_RUN_STATUSES:
            self._raise_step(state, event, "终态 Run 拒绝所有后续 Step 事件")
        if step is None:
            self._raise_step(state, event, "Step 不存在", current_status="MISSING")
        if step.status in _TERMINAL_STEP_STATUSES:
            self._raise_step(state, event, "终态 Step 拒绝所有后续事件")
        target_status = self._STEP_TRANSITIONS.get((step.status, event.event_type))
        if target_status is None:
            self._raise_step(state, event, "当前状态不允许该事件")
        self._guard_event_time(state, event.occurred_at, "Step", event.step_id, event.event_type.value)
        if event.occurred_at < step.created_at:
            self._raise_step(state, event, "事件时间不得早于 Step 创建时间")

        if event.event_type == StepEventType.STARTED:
            if state.status != RunStatus.RUNNING:
                self._raise_step(state, event, "只有 RUNNING Run 可以启动 Step")
            if event.step_id in state.active_step_ids:
                self._raise_step(state, event, "PENDING Step 不得已位于 active 集合")
        elif step.status == StepStatus.RUNNING:
            if event.step_id not in state.active_step_ids:
                self._raise_step(state, event, "RUNNING Step 必须位于 active 集合")
            if step.started_at is not None and event.occurred_at < step.started_at:
                self._raise_step(state, event, "结束事件时间不得早于 Step 启动时间")
        elif event.step_id in state.active_step_ids:
            self._raise_step(state, event, "未启动 Step 不得位于 active 集合")

        candidate = self._clone(state)
        candidate_step = candidate.steps[event.step_id]
        candidate_step.status = target_status
        candidate.updated_at = event.occurred_at
        if event.event_type == StepEventType.STARTED:
            candidate_step.started_at = event.occurred_at
            candidate_step.ended_at = None
            candidate_step.error_code = None
            candidate_step.error_message = None
            candidate.active_step_ids.add(event.step_id)
        else:
            candidate_step.ended_at = event.occurred_at
            candidate_step.error_code = event.error_code
            candidate_step.error_message = event.error_message
            candidate.active_step_ids.discard(event.step_id)
        candidate.validate()
        self._commit(state, candidate)

    @staticmethod
    def _clone(state: AgentState) -> AgentState:
        return AgentState.from_dict(state.to_dict())

    @staticmethod
    def _commit(state: AgentState, candidate: AgentState) -> None:
        """候选状态完整通过校验后，才替换原状态的字段。"""
        candidate.validate()
        state.schema_version = candidate.schema_version
        state.status = candidate.status
        state.created_at = candidate.created_at
        state.updated_at = candidate.updated_at
        state.steps = candidate.steps
        state.active_step_ids = candidate.active_step_ids
        state.stop_reason = candidate.stop_reason
        state.final_output = candidate.final_output
        state.error_code = candidate.error_code
        state.error_message = candidate.error_message
        state.validate()

    @staticmethod
    def _guard_event_time(
        state: AgentState,
        occurred_at: datetime,
        entity_type: str,
        entity_id: str,
        event_type: str,
    ) -> None:
        if occurred_at < state.updated_at:
            raise InvalidStateTransitionError(
                entity_type=entity_type,
                current_status=state.status.value,
                event_type=event_type,
                entity_id=entity_id,
                reason="事件时间不得早于当前状态更新时间",
            )

    def _guard_run_has_no_active_steps(self, state: AgentState, event_type: str) -> None:
        has_running_step = any(step.status == StepStatus.RUNNING for step in state.steps.values())
        if state.active_step_ids or has_running_step:
            self._raise_run(state, event_type, "Run 进入终态前必须先结束所有 active Step")

    @staticmethod
    def _raise_run(state: AgentState, event_type: str, reason: str) -> None:
        raise InvalidStateTransitionError(
            entity_type="Run",
            current_status=state.status.value,
            event_type=event_type,
            entity_id=state.run_id,
            reason=reason,
        )

    @staticmethod
    def _raise_step(
        state: AgentState,
        event: StepStateEvent,
        reason: str,
        *,
        current_status: str | None = None,
    ) -> None:
        step = state.steps.get(event.step_id)
        raise InvalidStateTransitionError(
            entity_type="Step",
            current_status=current_status or (step.status.value if step is not None else "MISSING"),
            event_type=event.event_type.value,
            entity_id=event.step_id,
            reason=reason,
        )
