#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP3 minimal result completion skeleton.

The Store cannot be written by the Driver; ``StepResultCommitter`` is the only
write owner and implements the WP3 result/state branch only:

    result validation
    -> Store PREPARED
    -> Step state terminal
    -> Store READABLE
    -> STEP_COMPLETED
    -> safe StepCompletionResult

WP4 will add the OutputGate and delivery branch; this module deliberately
contains no OutputGate or delivery status.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
import threading

from core.runtime.event_channel import EventChannelClosedError
from core.runtime.event_emitter import RunEventEmitter, StepEventEmitter
from core.runtime.events import RuntimeEventType, StepCompletedPayload
from core.runtime.planning import OutputPolicy, Plan
from core.runtime.scheduler import StepClaim
from core.runtime.state import AgentState, StepStatus
from core.runtime.state_machine import (
    AgentStateMachine,
    InvalidStateTransitionError,
    StepEventType,
    StepStateEvent,
)
from core.runtime.step_result import ResultContentType, StepResult
from core.runtime.step_result_store import (
    StoreStatus,
    StepResultStore,
    StepResultStoreError,
    StepResultStoreErrorCode,
)


class StepCommitStatus(str, Enum):
    NONE = "NONE"
    PREPARED = "PREPARED"
    COMMITTED = "COMMITTED"


class StepCompletionErrorCode(str, Enum):
    STEP_RESULT_INVALID = "STEP_RESULT_INVALID"
    STEP_RESULT_PREPARE_FAILED = "STEP_RESULT_PREPARE_FAILED"
    STEP_STATE_COMMIT_FAILED = "STEP_STATE_COMMIT_FAILED"
    STEP_RESULT_COMMIT_FAILED = "STEP_RESULT_COMMIT_FAILED"
    STEP_COMPLETION_EVENT_FAILED = "STEP_COMPLETION_EVENT_FAILED"
    STEP_RESULT_DUPLICATE_COMMIT = "STEP_RESULT_DUPLICATE_COMMIT"
    STEP_RESULT_LATE_COMMIT = "STEP_RESULT_LATE_COMMIT"


@dataclass(frozen=True, slots=True)
class StepCompletionResult:
    """Safe control-plane completion metadata; never carries raw content."""

    step_id: str
    producer_agent_id: str
    content_type: ResultContentType | None
    char_count: int
    complete: bool
    commit_status: StepCommitStatus
    final_result_ready: bool
    event_emitted: bool
    error_code: str | None = None
    safe_message: str = ""

    @property
    def succeeded(self) -> bool:
        return self.error_code is None and self.commit_status is StepCommitStatus.COMMITTED


class StepResultCommitter:
    """Run-scoped completion owner; the only writer of StepResultStore."""

    def __init__(
        self,
        *,
        store: StepResultStore,
        state_machine: AgentStateMachine,
        event_emitter: RunEventEmitter | None,
        plan: Plan,
    ) -> None:
        if not isinstance(store, StepResultStore):
            raise TypeError("committer 需要 StepResultStore")
        if not isinstance(state_machine, AgentStateMachine):
            raise TypeError("committer 需要 AgentStateMachine")
        if not isinstance(plan, Plan):
            raise TypeError("committer 需要冻结的 Plan")
        if event_emitter is not None and not isinstance(
            event_emitter, RunEventEmitter
        ):
            raise TypeError("committer event_emitter 必须合法")
        self._store = store
        self._state_machine = state_machine
        self._event_emitter = event_emitter
        self._plan = plan
        self._guard_lock = threading.Lock()
        self._completed_steps: set[str] = set()
        finals = tuple(
            step
            for step in plan.steps
            if step.output_policy is not OutputPolicy.INTERNAL
        )
        if len(finals) != 1:
            raise ValueError("Plan 必须具有唯一 final Step")
        self._final_step_id = finals[0].step_id

    @property
    def store(self) -> StepResultStore:
        return self._store

    def _acquire_guard(self, step_id: str) -> bool:
        with self._guard_lock:
            if step_id in self._completed_steps:
                return False
            self._completed_steps.add(step_id)
            return True

    def _plan_step(self, step_id: str):
        for step in self._plan.steps:
            if step.step_id == step_id:
                return step
        return None

    async def commit(
        self,
        claim: StepClaim,
        result: StepResult,
        agent_state: AgentState,
    ) -> StepCompletionResult:
        """Commit one StepResult; returns safe metadata, never raises for
        business failures."""
        if not self._acquire_guard(claim.step_id):
            return self._failure(
                claim,
                result,
                StepCommitStatus.NONE,
                StepCompletionErrorCode.STEP_RESULT_DUPLICATE_COMMIT,
                "重复 completion 回调被拒绝",
            )
        if self._store.status is not StoreStatus.OPEN:
            return self._failure(
                claim,
                result,
                StepCommitStatus.NONE,
                StepCompletionErrorCode.STEP_RESULT_LATE_COMMIT,
                "Store 已终结，拒绝迟到结果",
            )

        validation_error = self._validate(claim, result)
        if validation_error is not None:
            await self._apply_failed_and_emit(
                claim,
                agent_state,
                validation_error.value,
            )
            return self._failure(
                claim,
                result,
                StepCommitStatus.NONE,
                validation_error,
                "result 与 claim/Plan 不一致",
            )

        try:
            self._store.write_prepared(
                result,
                expected_agent_id=claim.preferred_agent,
            )
        except StepResultStoreError as exc:
            code = self._map_prepare_error(exc.error_code)
            await self._apply_failed_and_emit(claim, agent_state, code.value)
            return self._failure(
                claim,
                result,
                StepCommitStatus.NONE,
                code,
                "result prepare 失败",
            )

        try:
            self._apply_succeeded(claim, agent_state)
        except (InvalidStateTransitionError, ValueError) as exc:
            # Step remains RUNNING; the Coordinator settles it at Run terminal.
            return self._failure(
                claim,
                result,
                StepCommitStatus.PREPARED,
                StepCompletionErrorCode.STEP_STATE_COMMIT_FAILED,
                "Step 成功状态提交失败",
            )

        try:
            self._store.mark_readable(claim.step_id, agent_state)
        except StepResultStoreError as exc:
            code = (
                StepCompletionErrorCode.STEP_RESULT_COMMIT_FAILED
                if exc.error_code
                in {
                    StepResultStoreErrorCode.PRODUCER_NOT_SUCCEEDED,
                    StepResultStoreErrorCode.DUPLICATE_WRITE,
                }
                else StepCompletionErrorCode.STEP_RESULT_COMMIT_FAILED
            )
            await self._emit_step_completed(
                claim, agent_state, StepStatus.SUCCEEDED, code.value
            )
            return self._failure(
                claim,
                result,
                StepCommitStatus.PREPARED,
                code,
                "Store mark READABLE 失败",
            )

        emitted = await self._emit_step_completed(
            claim, agent_state, StepStatus.SUCCEEDED, None
        )
        if not emitted:
            return self._failure(
                claim,
                result,
                StepCommitStatus.COMMITTED,
                StepCompletionErrorCode.STEP_COMPLETION_EVENT_FAILED,
                "STEP_COMPLETED 事件发布失败",
                final_result_ready=(claim.step_id == self._final_step_id),
                event_emitted=False,
            )
        return StepCompletionResult(
            step_id=claim.step_id,
            producer_agent_id=claim.preferred_agent,
            content_type=result.content_type,
            char_count=result.char_count,
            complete=result.complete,
            commit_status=StepCommitStatus.COMMITTED,
            final_result_ready=(claim.step_id == self._final_step_id),
            event_emitted=True,
        )

    def _validate(
        self,
        claim: StepClaim,
        result: StepResult,
    ) -> StepCompletionErrorCode | None:
        if not isinstance(result, StepResult):
            return StepCompletionErrorCode.STEP_RESULT_INVALID
        plan_step = self._plan_step(claim.step_id)
        if plan_step is None:
            return StepCompletionErrorCode.STEP_RESULT_INVALID
        if claim.step_id != result.step_id:
            return StepCompletionErrorCode.STEP_RESULT_INVALID
        if claim.preferred_agent != plan_step.preferred_agent:
            return StepCompletionErrorCode.STEP_RESULT_INVALID
        if result.producer_agent_id != claim.preferred_agent:
            return StepCompletionErrorCode.STEP_RESULT_INVALID
        if result.complete is not True:
            return StepCompletionErrorCode.STEP_RESULT_INVALID
        return None

    @staticmethod
    def _map_prepare_error(
        code: StepResultStoreErrorCode,
    ) -> StepCompletionErrorCode:
        if code in {
            StepResultStoreErrorCode.STORE_SEALED,
            StepResultStoreErrorCode.STORE_CLEARED,
        }:
            return StepCompletionErrorCode.STEP_RESULT_LATE_COMMIT
        if code is StepResultStoreErrorCode.DUPLICATE_WRITE:
            return StepCompletionErrorCode.STEP_RESULT_DUPLICATE_COMMIT
        if code in {
            StepResultStoreErrorCode.IDENTITY_MISMATCH,
            StepResultStoreErrorCode.UNKNOWN_PRODUCER,
        }:
            return StepCompletionErrorCode.STEP_RESULT_INVALID
        return StepCompletionErrorCode.STEP_RESULT_PREPARE_FAILED

    def _apply_succeeded(self, claim: StepClaim, agent_state: AgentState) -> None:
        self._state_machine.apply_step_event(
            agent_state,
            StepStateEvent(
                StepEventType.SUCCEEDED,
                claim.step_id,
                occurred_at=self._event_time(agent_state),
            ),
        )

    async def _apply_failed_and_emit(
        self,
        claim: StepClaim,
        agent_state: AgentState,
        error_code: str,
    ) -> None:
        current = agent_state.steps.get(claim.step_id)
        if current is not None and current.status is StepStatus.RUNNING:
            try:
                self._state_machine.apply_step_event(
                    agent_state,
                    StepStateEvent(
                        StepEventType.FAILED,
                        claim.step_id,
                        occurred_at=self._event_time(agent_state),
                        error_code=error_code,
                        error_message="结果提交失败",
                    ),
                )
            except (InvalidStateTransitionError, ValueError):
                return
        await self._emit_step_completed(
            claim, agent_state, StepStatus.FAILED, error_code
        )

    async def _emit_step_completed(
        self,
        claim: StepClaim,
        agent_state: AgentState,
        status: StepStatus,
        safe_error_code: str | None,
    ) -> bool:
        """Return False only when the event could not be published."""
        if self._event_emitter is None:
            return True
        emitter: StepEventEmitter = self._event_emitter.for_step(claim.step_id)
        if emitter.is_closed:
            return False
        step = agent_state.steps.get(claim.step_id)
        duration_ms = 0
        if (
            step is not None
            and step.started_at is not None
            and step.ended_at is not None
        ):
            duration_ms = max(
                0, int((step.ended_at - step.started_at).total_seconds() * 1000)
            )
        try:
            await emitter.emit(
                RuntimeEventType.STEP_COMPLETED,
                StepCompletedPayload(
                    status.value, safe_error_code, duration_ms=duration_ms
                ),
                component="step_completion",
                close=True,
                ignore_run_cancellation=True,
            )
            return True
        except asyncio.CancelledError:
            raise
        except (EventChannelClosedError, RuntimeError):
            return False

    @staticmethod
    def _event_time(agent_state: AgentState) -> datetime:
        return max(datetime.now(UTC), agent_state.updated_at)

    @staticmethod
    def _failure(
        claim: StepClaim,
        result: StepResult | None,
        commit_status: StepCommitStatus,
        error_code: StepCompletionErrorCode,
        safe_message: str,
        *,
        final_result_ready: bool = False,
        event_emitted: bool = False,
    ) -> StepCompletionResult:
        return StepCompletionResult(
            step_id=claim.step_id,
            producer_agent_id=claim.preferred_agent,
            content_type=result.content_type if result is not None else None,
            char_count=result.char_count if result is not None else 0,
            complete=result.complete if result is not None else False,
            commit_status=commit_status,
            final_result_ready=final_result_ready,
            event_emitted=event_emitted,
            error_code=error_code.value,
            safe_message=safe_message,
        )


__all__ = [
    "StepCommitStatus",
    "StepCompletionErrorCode",
    "StepCompletionResult",
    "StepResultCommitter",
]
