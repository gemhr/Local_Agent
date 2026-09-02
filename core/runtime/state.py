#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LocalAgent 每次运行所用的可序列化 AgentState 基础组件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
import threading
from typing import ClassVar


AGENT_STATE_SCHEMA_VERSION = 1


class AgentStateValidationError(ValueError):
    """违反 AgentState 或 StepState 不变量时引发。"""


class UnsupportedStateVersionError(ValueError):
    """序列化状态使用不支持的模式版本时引发。"""


class RunStatus(str, Enum):
    """单次聊天运行的粗粒度生命周期状态。"""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepStatus(str, Enum):
    """单个运行步骤的粗粒度生命周期状态。

    BLOCKED 表示前置条件失败、被取消或无法满足，因而该步骤不会在当前运行中执行。
    仅等待仍可能完成的前置条件的步骤应保持 PENDING。

    WAITING_FOR_APPROVAL 表示该 Step 已被 Scheduler claim 并开始执行，但当前
    worker 正在等待同一 Run 内 Tool Approval 的人类决定。它是 active、
    nonterminal、already-started Step：必须保留 ``started_at``、不得设置
    ``ended_at``，并继续留在 ``active_step_ids`` 中。
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


class StopReason(str, Enum):
    """说明运行停止原因的最终状态。"""

    COMPLETED = "COMPLETED"
    UNHANDLED_ERROR = "UNHANDLED_ERROR"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    USER_CANCELLED = "USER_CANCELLED"
    CLIENT_DISCONNECTED = "CLIENT_DISCONNECTED"
    SYSTEM_SHUTDOWN = "SYSTEM_SHUTDOWN"
    MAX_STEPS_REACHED = "MAX_STEPS_REACHED"
    NO_ACTION = "NO_ACTION"
    REPEATED_ACTION = "REPEATED_ACTION"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    PLANNING_FAILED = "PLANNING_FAILED"


_TERMINAL_RUN_STATUSES = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
_TERMINAL_STEP_STATUSES = {
    StepStatus.SUCCEEDED,
    StepStatus.FAILED,
    StepStatus.CANCELLED,
    StepStatus.BLOCKED,
    StepStatus.SKIPPED,
}
_CANCELLATION_REASONS = {
    StopReason.USER_CANCELLED,
    StopReason.CLIENT_DISCONNECTED,
    StopReason.SYSTEM_SHUTDOWN,
}


def utc_now() -> datetime:
    """返回供状态变更方法使用的带时区 UTC 时间戳。"""
    return datetime.now(UTC)


def _ensure_utc_datetime(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise AgentStateValidationError(f"{field_name} must be a timezone-aware UTC datetime")


def _parse_datetime(value: object, field_name: str, *, required: bool) -> datetime | None:
    if value is None:
        if required:
            raise AgentStateValidationError(f"{field_name} is required")
        return None
    if not isinstance(value, str):
        raise AgentStateValidationError(f"{field_name} must be an ISO 8601 string")
    parsed = datetime.fromisoformat(value)
    _ensure_utc_datetime(parsed, field_name)
    return parsed


def _safe_error_message(message: str | None) -> str | None:
    if message is None:
        return None
    compact = " ".join(str(message).split())
    return compact[:500]


@dataclass
class StepState:
    """AgentState 内单个执行步骤的可序列化状态。"""

    step_id: str
    name: str
    status: StepStatus = StepStatus.PENDING
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None

    def validate(self) -> None:
        """校验步骤级不变量。"""
        if not self.step_id:
            raise AgentStateValidationError("step_id must not be empty")
        if not self.name:
            raise AgentStateValidationError("step name must not be empty")
        _ensure_utc_datetime(self.created_at, f"step {self.step_id}.created_at")
        if self.started_at is not None:
            _ensure_utc_datetime(self.started_at, f"step {self.step_id}.started_at")
            if self.started_at < self.created_at:
                raise AgentStateValidationError("step started_at must not be before created_at")
        if self.ended_at is not None:
            _ensure_utc_datetime(self.ended_at, f"step {self.step_id}.ended_at")
            if self.ended_at < self.created_at:
                raise AgentStateValidationError("step ended_at must not be before created_at")
            if self.started_at is not None and self.ended_at < self.started_at:
                raise AgentStateValidationError("step ended_at must not be before started_at")
        if self.status == StepStatus.PENDING:
            if self.started_at is not None or self.ended_at is not None:
                raise AgentStateValidationError("pending step must not have started_at or ended_at")
        if self.status == StepStatus.RUNNING:
            if self.started_at is None or self.ended_at is not None:
                raise AgentStateValidationError("running step must have started_at and no ended_at")
        if self.status == StepStatus.WAITING_FOR_APPROVAL:
            if self.started_at is None or self.ended_at is not None:
                raise AgentStateValidationError(
                    "waiting-for-approval step must have started_at and no ended_at"
                )
        if self.status == StepStatus.BLOCKED and self.started_at is not None:
            raise AgentStateValidationError("blocked step must not have started_at")
        if self.status in _TERMINAL_STEP_STATUSES and self.ended_at is None:
            raise AgentStateValidationError("terminal step must have ended_at")
        if self.status == StepStatus.SUCCEEDED and (self.error_code or self.error_message):
            raise AgentStateValidationError("succeeded step must not contain error information")
        if self.status == StepStatus.FAILED and not (self.error_code or self.error_message):
            raise AgentStateValidationError("failed step must include a safe error summary")

    def to_dict(self) -> dict[str, str | None]:
        """将此步骤序列化为适用于 JSON 的基础类型。"""
        self.validate()
        return {
            "step_id": self.step_id,
            "name": self.name,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "StepState":
        """从适用于 JSON 的基础类型反序列化并校验一个 StepState。"""
        try:
            status = StepStatus(str(payload["status"]))
        except KeyError as exc:
            raise AgentStateValidationError("step status is required") from exc
        step = cls(
            step_id=str(payload.get("step_id", "")),
            name=str(payload.get("name", "")),
            status=status,
            created_at=_parse_datetime(payload.get("created_at"), "step.created_at", required=True) or utc_now(),
            started_at=_parse_datetime(payload.get("started_at"), "step.started_at", required=False),
            ended_at=_parse_datetime(payload.get("ended_at"), "step.ended_at", required=False),
            error_code=str(payload["error_code"]) if payload.get("error_code") is not None else None,
            error_message=str(payload["error_message"]) if payload.get("error_message") is not None else None,
        )
        step.validate()
        return step


@dataclass
class AgentState:
    """单次 LocalAgent 运行的可变但受约束的可序列化状态。"""

    run_id: str
    schema_version: int = AGENT_STATE_SCHEMA_VERSION
    status: RunStatus = RunStatus.CREATED
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    steps: dict[str, StepState] = field(default_factory=dict)
    active_step_ids: set[str] = field(default_factory=set)
    stop_reason: StopReason | None = None
    final_output: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    _runtime_lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
        compare=False,
    )

    SUPPORTED_SCHEMA_VERSION: ClassVar[int] = AGENT_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.validate()

    @classmethod
    def for_run_context(cls, run_id: str) -> "AgentState":
        """为 RunContext 的 run_id 创建初始状态。"""
        return cls(run_id=run_id)

    def assert_matches_run_context(self, run_id: str) -> None:
        """集成前确保状态与 RunContext 标识符匹配。"""
        if self.run_id != run_id:
            raise AgentStateValidationError("agent_state.run_id must match run_context.run_id")

    def mark_running(self) -> None:
        """将运行标记为运行中。"""
        self.status = RunStatus.RUNNING
        self.stop_reason = None
        self._touch_and_validate()

    def add_step(self, step_id: str, name: str) -> None:
        """添加具有唯一标识符的新待处理步骤。"""
        if step_id in self.steps:
            raise AgentStateValidationError("step_id must be unique")
        self.steps[step_id] = StepState(step_id=step_id, name=name)
        self._touch_and_validate()

    def start_step(self, step_id: str) -> None:
        """将现有步骤标记为运行中且活跃。"""
        step = self._get_step(step_id)
        now = utc_now()
        step.status = StepStatus.RUNNING
        step.started_at = now
        step.ended_at = None
        step.error_code = None
        step.error_message = None
        self.active_step_ids.add(step_id)
        self._touch_and_validate()

    def succeed_step(self, step_id: str) -> None:
        """将运行中的步骤标记为成功且不再活跃。"""
        step = self._get_step(step_id)
        step.status = StepStatus.SUCCEEDED
        step.ended_at = utc_now()
        step.error_code = None
        step.error_message = None
        self.active_step_ids.discard(step_id)
        self._touch_and_validate()

    def fail_step(self, step_id: str, *, error_code: str, error_message: str | None = None) -> None:
        """使用安全错误码和简短摘要将步骤标记为失败。"""
        step = self._get_step(step_id)
        step.status = StepStatus.FAILED
        step.ended_at = utc_now()
        step.error_code = error_code
        step.error_message = _safe_error_message(error_message)
        self.active_step_ids.discard(step_id)
        self._touch_and_validate()

    def cancel_step(self, step_id: str, *, error_code: str = "RUN_CANCELLED", error_message: str | None = None) -> None:
        """将步骤标记为已取消且不再活跃。"""
        step = self._get_step(step_id)
        step.status = StepStatus.CANCELLED
        step.ended_at = utc_now()
        step.error_code = error_code
        step.error_message = _safe_error_message(error_message)
        self.active_step_ids.discard(step_id)
        self._touch_and_validate()

    def block_step(self, step_id: str, *, error_code: str | None = None, error_message: str | None = None) -> None:
        """将从未启动的步骤标记为在本次运行中受阻，但不激活该步骤。"""
        step = self._get_step(step_id)
        step.status = StepStatus.BLOCKED
        step.started_at = None
        step.ended_at = utc_now()
        step.error_code = error_code
        step.error_message = _safe_error_message(error_message)
        self.active_step_ids.discard(step_id)
        self._touch_and_validate()

    def mark_succeeded(self, *, final_output: str | None = None) -> None:
        """将运行标记为成功完成。"""
        self.status = RunStatus.SUCCEEDED
        self.stop_reason = StopReason.COMPLETED
        self.final_output = final_output
        self.error_code = None
        self.error_message = None
        self.active_step_ids.clear()
        self._touch_and_validate()

    def mark_failed(self, *, stop_reason: StopReason, error_code: str, error_message: str | None = None) -> None:
        """使用安全错误摘要将运行标记为失败。"""
        self.status = RunStatus.FAILED
        self.stop_reason = stop_reason
        self.error_code = error_code
        self.error_message = _safe_error_message(error_message)
        self.active_step_ids.clear()
        self._touch_and_validate()

    def mark_cancelled(self, *, stop_reason: StopReason, error_code: str = "RUN_CANCELLED", error_message: str | None = None) -> None:
        """使用兼容的取消原因将运行标记为已取消。"""
        self.status = RunStatus.CANCELLED
        self.stop_reason = stop_reason
        self.error_code = error_code
        self.error_message = _safe_error_message(error_message)
        self.active_step_ids.clear()
        self._touch_and_validate()

    def validate(self) -> None:
        """校验运行级和跨步骤不变量。"""
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise UnsupportedStateVersionError(f"unsupported AgentState schema_version: {self.schema_version}")
        if self.schema_version != AGENT_STATE_SCHEMA_VERSION:
            raise UnsupportedStateVersionError(f"unsupported AgentState schema_version: {self.schema_version}")
        if not self.run_id:
            raise AgentStateValidationError("run_id must not be empty")
        _ensure_utc_datetime(self.created_at, "created_at")
        _ensure_utc_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise AgentStateValidationError("updated_at must not be before created_at")
        if self.status in {RunStatus.CREATED, RunStatus.RUNNING} and self.stop_reason is not None:
            raise AgentStateValidationError("non-terminal run must not have stop_reason")
        if self.status in _TERMINAL_RUN_STATUSES and self.stop_reason is None:
            raise AgentStateValidationError("terminal run must have stop_reason")
        if self.status in _TERMINAL_RUN_STATUSES and self.active_step_ids:
            raise AgentStateValidationError("terminal run must not have active steps")
        if self.status == RunStatus.SUCCEEDED and self.stop_reason != StopReason.COMPLETED:
            raise AgentStateValidationError("succeeded run must use COMPLETED stop_reason")
        if self.status == RunStatus.CANCELLED and self.stop_reason not in _CANCELLATION_REASONS:
            raise AgentStateValidationError("cancelled run must use a cancellation stop_reason")
        if self.status == RunStatus.FAILED and self.stop_reason in _CANCELLATION_REASONS | {StopReason.COMPLETED}:
            raise AgentStateValidationError("failed run must not use completed or cancellation stop_reason")
        if len(self.steps) != len(set(self.steps)):
            raise AgentStateValidationError("step_id must be unique")
        for step_id, step in self.steps.items():
            if step_id != step.step_id:
                raise AgentStateValidationError("step dictionary key must match step_id")
            step.validate()
        for step_id in self.active_step_ids:
            step = self.steps.get(step_id)
            if step is None:
                raise AgentStateValidationError("active step id must exist in steps")
            if step.status not in {
                StepStatus.RUNNING,
                StepStatus.WAITING_FOR_APPROVAL,
            }:
                raise AgentStateValidationError("active step must be RUNNING or WAITING_FOR_APPROVAL")
        running_step_ids = {
            step.step_id
            for step in self.steps.values()
            if step.status in {StepStatus.RUNNING, StepStatus.WAITING_FOR_APPROVAL}
        }
        if running_step_ids != self.active_step_ids:
            raise AgentStateValidationError(
                "all RUNNING / WAITING_FOR_APPROVAL steps must be present in active_step_ids"
            )

    def to_dict(self) -> dict[str, object]:
        """将状态序列化为确定性的适用于 JSON 的基础类型。"""
        with self._runtime_lock:
            self.validate()
            return {
                "schema_version": self.schema_version,
                "run_id": self.run_id,
                "status": self.status.value,
                "created_at": self.created_at.isoformat(),
                "updated_at": self.updated_at.isoformat(),
                "steps": [
                    self.steps[step_id].to_dict() for step_id in sorted(self.steps)
                ],
                "active_step_ids": sorted(self.active_step_ids),
                "stop_reason": self.stop_reason.value if self.stop_reason else None,
                "final_output": self.final_output,
                "error_code": self.error_code,
                "error_message": self.error_message,
            }

    def snapshot_copy(self) -> "AgentState":
        """Capture one detached, internally consistent state view."""
        with self._runtime_lock:
            return AgentState.from_dict(self.to_dict())

    @property
    def runtime_lock(self) -> threading.RLock:
        """Unique minimal synchronization boundary for runtime transitions."""
        return self._runtime_lock

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "AgentState":
        """反序列化、检查版本并校验 AgentState 载荷。"""
        if "schema_version" not in payload:
            raise UnsupportedStateVersionError("AgentState schema_version is required")
        version = payload["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise UnsupportedStateVersionError(f"unsupported AgentState schema_version: {version}")
        if version != AGENT_STATE_SCHEMA_VERSION:
            raise UnsupportedStateVersionError(f"unsupported AgentState schema_version: {version}")
        try:
            status = RunStatus(str(payload["status"]))
        except KeyError as exc:
            raise AgentStateValidationError("status is required") from exc
        stop_reason_value = payload.get("stop_reason")
        stop_reason = StopReason(str(stop_reason_value)) if stop_reason_value is not None else None
        raw_steps = payload.get("steps", [])
        if not isinstance(raw_steps, list):
            raise AgentStateValidationError("steps must be a list")
        steps: dict[str, StepState] = {}
        for raw_step in raw_steps:
            if not isinstance(raw_step, dict):
                raise AgentStateValidationError("each step must be an object")
            step = StepState.from_dict(raw_step)
            if step.step_id in steps:
                raise AgentStateValidationError("step_id must be unique")
            steps[step.step_id] = step
        raw_active = payload.get("active_step_ids", [])
        if not isinstance(raw_active, list):
            raise AgentStateValidationError("active_step_ids must be a list")
        state = cls(
            run_id=str(payload.get("run_id", "")),
            schema_version=AGENT_STATE_SCHEMA_VERSION,
            status=status,
            created_at=_parse_datetime(payload.get("created_at"), "created_at", required=True) or utc_now(),
            updated_at=_parse_datetime(payload.get("updated_at"), "updated_at", required=True) or utc_now(),
            steps=steps,
            active_step_ids={str(step_id) for step_id in raw_active},
            stop_reason=stop_reason,
            final_output=str(payload["final_output"]) if payload.get("final_output") is not None else None,
            error_code=str(payload["error_code"]) if payload.get("error_code") is not None else None,
            error_message=str(payload["error_message"]) if payload.get("error_message") is not None else None,
        )
        state.validate()
        return state

    def _get_step(self, step_id: str) -> StepState:
        if step_id not in self.steps:
            raise AgentStateValidationError("unknown step_id")
        return self.steps[step_id]

    def _touch_and_validate(self) -> None:
        self.updated_at = utc_now()
        self.validate()
