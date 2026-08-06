#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""共享的纯 Runtime Event 状态投影（WP5）。

``RunProjection`` 只由 Runtime Events / JournalRecord 构建，永不携带 raw
正文；同一事件重复输入幂等；sequence 倒退或冲突拒绝；未知 control event
安全忽略；投影器不会触发执行、重试或 Memory 写入。前端与测试共享此对象，
避免在客户端用字符串拼装状态。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum

from core.runtime.events import (
    OutputDeltaPayload,
    RuntimeEvent,
    RuntimeEventType,
)


class ProjectionSequenceError(RuntimeError):
    """固定码 sequence 冲突；不携带事件内容。"""

    def __init__(self, error_code: str, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code})")


class PlanningStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    PLANNING = "PLANNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class PlanStatus(str, Enum):
    NONE = "NONE"
    CREATED = "CREATED"


class SynthesisStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class RunProjection:
    """可解释的 Run 分层状态；无 raw 内容、无高基数 ID 之外的数据。"""

    planning_status: PlanningStatus = PlanningStatus.NOT_STARTED
    plan_status: PlanStatus = PlanStatus.NONE
    plan_shape: str | None = None
    active_steps: tuple[str, ...] = ()
    completed_steps: tuple[str, ...] = ()
    failed_steps: tuple[str, ...] = ()
    synthesis_status: SynthesisStatus = SynthesisStatus.NOT_STARTED
    delivery_status: str | None = None
    memory_commit_status: str | None = None
    run_status: str | None = None
    stop_reason: str | None = None
    safe_error_code: str | None = None
    output_journaled: bool = False
    last_sequence: int = 0

    @property
    def planning_required(self) -> bool:
        return self.planning_status is PlanningStatus.PLANNING

    @property
    def terminal(self) -> bool:
        return self.run_status in {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
        }


class RuntimeProjectionBuilder:
    """Runtime Event -> RunProjection 的唯一纯投影 owner。

    幂等/sequence 合同：
    - 同一 (sequence, event_id) 重复输入：忽略（幂等）；
    - 同一 sequence 不同 event_id：拒绝（冲突）；
    - sequence 倒退：拒绝；
    - 未知 control event：安全忽略，不影响状态。
    """

    def __init__(self) -> None:
        self._events: dict[int, str] = {}
        self._steps: dict[str, str] = {}
        self._projection = RunProjection()

    @property
    def projection(self) -> RunProjection:
        return self._projection

    @staticmethod
    def _event_id(event) -> str:
        value = getattr(event, "event_id", None)
        return str(value) if value is not None else ""

    def apply(self, event) -> RunProjection:
        if not hasattr(event, "event_type") or not hasattr(event, "sequence"):
            raise ProjectionSequenceError(
                "PROJECTION_INVALID_EVENT",
                "投影只接受 RuntimeEvent 或 JournalRecord",
            )
        sequence = event.sequence
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ProjectionSequenceError(
                "PROJECTION_SEQUENCE_INVALID",
                "sequence 必须是正整数",
            )
        event_id = self._event_id(event)
        existing = self._events.get(sequence)
        if existing is not None:
            if existing == event_id:
                return self._projection
            raise ProjectionSequenceError(
                "PROJECTION_SEQUENCE_CONFLICT",
                "同一 sequence 收到不同事件，拒绝乱序投影",
            )
        if sequence <= self._projection.last_sequence:
            raise ProjectionSequenceError(
                "PROJECTION_SEQUENCE_REGRESSION",
                "sequence 倒退，拒绝投影",
            )
        self._events[sequence] = event_id
        self._projection = self._reduce(event, self._projection)
        return self._projection

    def _reduce(
        self, event, projection: RunProjection
    ) -> RunProjection:
        event_type = event.event_type
        payload = getattr(event, "safe_payload", None)
        if payload is None:
            payload = getattr(event, "payload", None)
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            try:
                payload = asdict(payload)
            except Exception:
                payload = {}
        step_id = getattr(event, "step_id", None)

        if event_type is RuntimeEventType.RUN_STARTED:
            return self._replace(
                projection,
                run_status=str(payload.get("status", "RUNNING")),
                last_sequence=event.sequence,
            )
        if event_type is RuntimeEventType.PLANNING_STARTED:
            return self._replace(
                projection,
                planning_status=PlanningStatus.PLANNING,
                last_sequence=event.sequence,
            )
        if event_type is RuntimeEventType.PLAN_CREATED:
            shape = payload.get("shape")
            return self._replace(
                projection,
                planning_status=PlanningStatus.COMPLETED,
                plan_status=PlanStatus.CREATED,
                plan_shape=shape if isinstance(shape, str) else None,
                last_sequence=event.sequence,
            )
        if event_type is RuntimeEventType.STEP_STARTED:
            if step_id is None:
                return self._replace(projection, last_sequence=event.sequence)
            self._steps[step_id] = "RUNNING"
            active = tuple(
                step
                for step in self._steps
                if self._steps[step] == "RUNNING"
            )
            execution_kind = payload.get("execution_kind")
            synthesis_status = projection.synthesis_status
            if (
                execution_kind == "SYNTHESIS"
                and synthesis_status is SynthesisStatus.NOT_STARTED
            ):
                synthesis_status = SynthesisStatus.RUNNING
            return self._replace(
                projection,
                active_steps=active,
                synthesis_status=synthesis_status,
                last_sequence=event.sequence,
            )
        if event_type is RuntimeEventType.STEP_COMPLETED:
            if step_id is None:
                return self._replace(projection, last_sequence=event.sequence)
            status = str(payload.get("status", "UNKNOWN"))
            self._steps[step_id] = status
            active = tuple(
                step
                for step in self._steps
                if self._steps[step] == "RUNNING"
            )
            completed = tuple(
                step
                for step in self._steps
                if self._steps[step] == "SUCCEEDED"
            )
            failed = tuple(
                step
                for step in self._steps
                if self._steps[step] in {"FAILED", "CANCELLED"}
            )
            execution_kind = payload.get("execution_kind")
            synthesis_status = projection.synthesis_status
            if execution_kind == "SYNTHESIS":
                if status == "SUCCEEDED":
                    synthesis_status = SynthesisStatus.COMPLETED
                elif status in {"FAILED", "CANCELLED"}:
                    synthesis_status = SynthesisStatus.FAILED
            delivery_status = payload.get("delivery_status")
            return self._replace(
                projection,
                active_steps=active,
                completed_steps=completed,
                failed_steps=failed,
                synthesis_status=synthesis_status,
                delivery_status=(
                    str(delivery_status)
                    if delivery_status is not None
                    else projection.delivery_status
                ),
                last_sequence=event.sequence,
            )
        if event_type is RuntimeEventType.OUTPUT_DELTA:
            return self._replace(
                projection,
                output_journaled=True,
                last_sequence=event.sequence,
            )
        if event_type is RuntimeEventType.ERROR:
            delivery_status = payload.get("delivery_status")
            memory_status = payload.get("memory_commit_status")
            final_status = payload.get("final_step_status")
            return self._replace(
                projection,
                delivery_status=(
                    str(delivery_status)
                    if delivery_status is not None
                    else projection.delivery_status
                ),
                memory_commit_status=(
                    str(memory_status)
                    if memory_status is not None
                    else projection.memory_commit_status
                ),
                safe_error_code=(
                    str(payload.get("safe_error_code"))
                    if payload.get("safe_error_code") is not None
                    else projection.safe_error_code
                ),
                last_sequence=event.sequence,
            )
        if event_type is RuntimeEventType.RUN_COMPLETED:
            delivery_status = payload.get("delivery_status")
            memory_status = payload.get("memory_commit_status")
            return self._replace(
                projection,
                run_status=str(payload.get("status", "UNKNOWN")),
                stop_reason=(
                    str(payload.get("stop_reason"))
                    if payload.get("stop_reason") is not None
                    else projection.stop_reason
                ),
                delivery_status=(
                    str(delivery_status)
                    if delivery_status is not None
                    else projection.delivery_status
                ),
                memory_commit_status=(
                    str(memory_status)
                    if memory_status is not None
                    else projection.memory_commit_status
                ),
                safe_error_code=(
                    str(payload.get("safe_error_code"))
                    if payload.get("safe_error_code") is not None
                    else projection.safe_error_code
                ),
                last_sequence=event.sequence,
            )
        if event_type in {
            RuntimeEventType.CANCELLATION,
            RuntimeEventType.TIMEOUT,
            RuntimeEventType.BUDGET_EXHAUSTED,
        }:
            code = payload.get("safe_error_code")
            return self._replace(
                projection,
                safe_error_code=(
                    str(code) if code is not None else projection.safe_error_code
                ),
                last_sequence=event.sequence,
            )
        # 未知 / 观测类 control event 安全忽略（MODEL/TOOL/RETRIEVAL 等）。
        return self._replace(projection, last_sequence=event.sequence)

    @staticmethod
    def _replace(projection: RunProjection, **changes) -> RunProjection:
        return RunProjection(
            planning_status=changes.get(
                "planning_status", projection.planning_status
            ),
            plan_status=changes.get("plan_status", projection.plan_status),
            plan_shape=changes.get("plan_shape", projection.plan_shape),
            active_steps=changes.get("active_steps", projection.active_steps),
            completed_steps=changes.get(
                "completed_steps", projection.completed_steps
            ),
            failed_steps=changes.get("failed_steps", projection.failed_steps),
            synthesis_status=changes.get(
                "synthesis_status", projection.synthesis_status
            ),
            delivery_status=changes.get(
                "delivery_status", projection.delivery_status
            ),
            memory_commit_status=changes.get(
                "memory_commit_status", projection.memory_commit_status
            ),
            run_status=changes.get("run_status", projection.run_status),
            stop_reason=changes.get("stop_reason", projection.stop_reason),
            safe_error_code=changes.get(
                "safe_error_code", projection.safe_error_code
            ),
            output_journaled=changes.get(
                "output_journaled", projection.output_journaled
            ),
            last_sequence=changes.get(
                "last_sequence", projection.last_sequence
            ),
        )


__all__ = [
    "PlanStatus",
    "PlanningStatus",
    "ProjectionSequenceError",
    "RunProjection",
    "RuntimeProjectionBuilder",
    "SynthesisStatus",
]
