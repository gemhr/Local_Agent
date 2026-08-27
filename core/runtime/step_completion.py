#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP4 full StepCompletionPipeline (keeps the StepResultCommitter name).

The Store cannot be written by the Driver; the completion owner is the only
write owner and implements the complete INTERNAL/FINAL branch:

    result validation
    -> Store PREPARED
    -> Step RUNNING -> SUCCEEDED
    -> Store READABLE
    -> [FINAL only] OutputGate.attempt_publish
         -> DELIVERED / FAILED / OUTCOME_UNKNOWN
    -> [DELIVERED only] run-level final Memory writer
    -> STEP_COMPLETED(SUCCEEDED)
    -> safe StepCompletionResult

INTERNAL steps never call the OutputGate and always report
delivery_status=NOT_APPLICABLE. The gate is never retried, a gate failure
never changes the Step to FAILED, and raw result text never enters the safe
report.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
import threading
import time
from typing import Protocol

from core.runtime.event_channel import (
    EventChannelClosedError,
    EventPublicationError,
)
from core.runtime.event_emitter import RunEventEmitter, StepEventEmitter
from core.runtime.events import RuntimeEventType, StepCompletedPayload
from core.runtime.final_memory_writer import CommittedExchangeReceipt
from core.runtime.output_gate import (
    DeliveryAttempt,
    DeliveryStatus,
    OutputGate,
)
from core.runtime.planning import OutputPolicy, Plan
from core.runtime.scheduler import StepClaim
from core.runtime.semantic_memory_formation import SemanticFormationResult
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
from core.runtime.fault_injection_contract import InjectedFaultError


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
    FINAL_OUTPUT_MEMORY_COMMIT_FAILED = "FINAL_OUTPUT_MEMORY_COMMIT_FAILED"
    OUTPUT_GATE_DUPLICATE_ATTEMPT = "OUTPUT_GATE_DUPLICATE_ATTEMPT"
    FINAL_OUTPUT_DELIVERY_FAILED = "FINAL_OUTPUT_DELIVERY_FAILED"
    FINAL_OUTPUT_DELIVERY_UNKNOWN = "FINAL_OUTPUT_DELIVERY_UNKNOWN"


class FinalMemoryWriter(Protocol):
    """Delivered-only run-level final Memory commit owner.

    Called by the completion pipeline only after OutputGate reports DELIVERED.
    WP2: a successful commit returns the immutable committed exchange receipt
    consumed by the independent post-delivery Semantic Formation component.
    """

    def write_delivered(
        self,
        *,
        final_step_id: str,
        store: StepResultStore,
    ) -> CommittedExchangeReceipt | None: ...


class SemanticMemoryFormationRunner(Protocol):
    """Independent post-delivery Semantic Memory Formation owner (WP2).

    Only called after DELIVERED + a committed exchange receipt. Its typed
    outcome never changes the delivered output, the final Step status, or the
    Run terminal decision.
    """

    async def run_formation(
        self,
        *,
        receipt: CommittedExchangeReceipt,
        final_step_id: str,
        store: StepResultStore,
    ) -> SemanticFormationResult: ...


@dataclass(frozen=True, slots=True)
class StepCompletionResult:
    """Safe control-plane completion metadata; never carries raw content.

    WP2 adds the independent post-delivery Formation outcome fields. They are
    pure observation: ``succeeded`` / ``error_code`` / terminal policy ignore
    them entirely.
    """

    step_id: str
    producer_agent_id: str
    content_type: ResultContentType | None
    char_count: int
    complete: bool
    commit_status: StepCommitStatus
    final_result_ready: bool
    event_emitted: bool
    output_policy: OutputPolicy | None = None
    delivery_status: DeliveryStatus = DeliveryStatus.NOT_APPLICABLE
    delivery_error_code: str | None = None
    completion_error_code: str | None = None
    memory_error_code: str | None = None
    formation_status: str | None = None
    formation_error_code: str | None = None
    error_code: str | None = None
    safe_message: str = ""

    @property
    def succeeded(self) -> bool:
        return self.error_code is None and self.commit_status is StepCommitStatus.COMMITTED


class StepResultCommitter:
    """Run-scoped completion owner; the only writer of StepResultStore.

    WP4 evolves the WP3 minimal skeleton into the full StepCompletionPipeline.
    There is exactly one Step completion owner per Run; the optional
    OutputGate/FinalMemoryWriter are injected by the Coordinator.
    """

    def __init__(
        self,
        *,
        store: StepResultStore,
        state_machine: AgentStateMachine,
        event_emitter: RunEventEmitter | None,
        plan: Plan,
        output_gate: OutputGate | None = None,
        final_memory_writer: FinalMemoryWriter | None = None,
        semantic_memory_formation: SemanticMemoryFormationRunner | None = None,
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
        if output_gate is not None and not isinstance(output_gate, OutputGate):
            raise TypeError("committer output_gate 必须合法")
        if final_memory_writer is not None and not callable(
            getattr(final_memory_writer, "write_delivered", None)
        ):
            raise TypeError("final_memory_writer 必须实现 write_delivered")
        if semantic_memory_formation is not None and not callable(
            getattr(semantic_memory_formation, "run_formation", None)
        ):
            raise TypeError(
                "semantic_memory_formation 必须实现 run_formation"
            )
        self._store = store
        self._state_machine = state_machine
        self._event_emitter = event_emitter
        self._plan = plan
        self._output_gate = output_gate
        self._final_memory_writer = final_memory_writer
        self._semantic_memory_formation = semantic_memory_formation
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

    @property
    def output_gate(self) -> OutputGate | None:
        return self._output_gate

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
                "Store 已关闭，拒绝迟到提交",
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
        except (StepResultStoreError, InjectedFaultError) as exc:
            if isinstance(exc, InjectedFaultError):
                code = StepCompletionErrorCode.STEP_RESULT_PREPARE_FAILED
            else:
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
                "Step 状态提交失败",
            )

        try:
            self._store.mark_readable(claim.step_id, agent_state)
        except (StepResultStoreError, InjectedFaultError):
            code = StepCompletionErrorCode.STEP_RESULT_COMMIT_FAILED
            await self._emit_step_completed(
                claim,
                agent_state,
                StepStatus.SUCCEEDED,
                code.value,
                result_char_count=result.char_count,
            )
            return self._failure(
                claim,
                result,
                StepCommitStatus.PREPARED,
                code,
                "Store mark READABLE 失败",
            )

        output_policy = self._plan_step(claim.step_id).output_policy
        is_final = claim.step_id == self._final_step_id
        delivery_attempt: DeliveryAttempt | None = None
        memory_error_code: str | None = None
        receipt: CommittedExchangeReceipt | None = None
        if is_final and self._output_gate is not None:
            delivery_started = time.monotonic()
            delivery_attempt = await self._output_gate.attempt_publish(
                claim=claim,
                result=result,
            )
            delivery_duration_ms = max(
                0, int((time.monotonic() - delivery_started) * 1000)
            )
        else:
            delivery_duration_ms = 0
        if (
            delivery_attempt is not None
            and delivery_attempt.delivery_status is DeliveryStatus.DELIVERED
            and self._final_memory_writer is not None
        ):
            try:
                receipt = await asyncio.to_thread(
                    self._final_memory_writer.write_delivered,
                    final_step_id=claim.step_id,
                    store=self._store,
                )
            except Exception:
                memory_error_code = (
                    StepCompletionErrorCode.FINAL_OUTPUT_MEMORY_COMMIT_FAILED.value
                )
        # Canonical WP2 ordering: DELIVERED -> conversation exchange committed
        # (receipt) -> independent post-delivery Semantic Formation -> existing
        # Step completion. Formation never runs without a committed receipt;
        # its outcome is pure observation and cannot alter delivery/terminal.
        formation_result: SemanticFormationResult | None = None
        formation_status: str | None = None
        formation_error_code: str | None = None
        if (
            receipt is not None
            and self._semantic_memory_formation is not None
        ):
            try:
                formation_result = await self._semantic_memory_formation.run_formation(
                    receipt=receipt,
                    final_step_id=claim.step_id,
                    store=self._store,
                )
            except asyncio.CancelledError:
                # DELIVERED 后的 Formation cancellation 是独立 business
                # outcome；即使第三方 runner 未像 production component 一样
                # 自行 typed 收口，也不得传播到现有 Step/Run terminal path。
                formation_result = None
                formation_status = "CANCELLED"
                formation_error_code = "FORMATION_CANCELLED"
            except Exception:
                formation_result = None
        if formation_result is not None:
            formation_status = formation_result.status.value
            formation_error_code = formation_result.safe_error_code
        elif (
            receipt is not None
            and self._semantic_memory_formation is not None
            and formation_status is None
        ):
            formation_status = "FAILED"
            formation_error_code = "FORMATION_INTERNAL_ERROR"

        emitted = await self._emit_step_completed(
            claim,
            agent_state,
            StepStatus.SUCCEEDED,
            None,
            result_char_count=result.char_count,
            delivery_status=(
                delivery_attempt.delivery_status.value
                if delivery_attempt is not None
                else None
            ),
            delivery_duration_ms=delivery_duration_ms,
        )
        if not emitted:
            return self._failure(
                claim,
                result,
                StepCommitStatus.COMMITTED,
                StepCompletionErrorCode.STEP_COMPLETION_EVENT_FAILED,
                "STEP_COMPLETED 事件发布失败",
                final_result_ready=is_final,
                event_emitted=False,
                output_policy=output_policy,
                delivery_attempt=delivery_attempt,
                memory_error_code=memory_error_code,
                formation_status=formation_status,
                formation_error_code=formation_error_code,
            )
        if memory_error_code is not None:
            return self._failure(
                claim,
                result,
                StepCommitStatus.COMMITTED,
                StepCompletionErrorCode.FINAL_OUTPUT_MEMORY_COMMIT_FAILED,
                "final output 已经交付但 Memory 提交失败",
                final_result_ready=is_final,
                event_emitted=True,
                output_policy=output_policy,
                delivery_attempt=delivery_attempt,
                memory_error_code=memory_error_code,
            )
        if delivery_attempt is not None and (
            delivery_attempt.delivery_status is not DeliveryStatus.DELIVERED
        ):
            return self._failure(
                claim,
                result,
                StepCommitStatus.COMMITTED,
                self._delivery_failure_code(delivery_attempt),
                delivery_attempt.safe_message,
                final_result_ready=is_final,
                event_emitted=True,
                output_policy=output_policy,
                delivery_attempt=delivery_attempt,
            )
        return StepCompletionResult(
            step_id=claim.step_id,
            producer_agent_id=claim.preferred_agent,
            content_type=result.content_type,
            char_count=result.char_count,
            complete=result.complete,
            commit_status=StepCommitStatus.COMMITTED,
            final_result_ready=is_final,
            event_emitted=True,
            output_policy=output_policy,
            delivery_status=(
                delivery_attempt.delivery_status
                if delivery_attempt is not None
                else DeliveryStatus.NOT_APPLICABLE
            ),
            delivery_error_code=(
                delivery_attempt.error_code
                if delivery_attempt is not None
                else None
            ),
            formation_status=formation_status,
            formation_error_code=formation_error_code,
        )

    @staticmethod
    def _delivery_failure_code(attempt: DeliveryAttempt) -> StepCompletionErrorCode:
        if attempt.error_code == "OUTPUT_GATE_DUPLICATE_ATTEMPT":
            return StepCompletionErrorCode.OUTPUT_GATE_DUPLICATE_ATTEMPT
        if attempt.delivery_status is DeliveryStatus.OUTCOME_UNKNOWN:
            return StepCompletionErrorCode.FINAL_OUTPUT_DELIVERY_UNKNOWN
        return StepCompletionErrorCode.FINAL_OUTPUT_DELIVERY_FAILED

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
                            error_message="步骤执行失败",
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
        *,
        result_char_count: int = 0,
        delivery_status: str | None = None,
        delivery_duration_ms: int = 0,
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
        plan_step = self._plan_step(claim.step_id)
        try:
            await emitter.emit(
                RuntimeEventType.STEP_COMPLETED,
                StepCompletedPayload(
                    status.value,
                    safe_error_code,
                    duration_ms=duration_ms,
                    result_char_count=result_char_count,
                    delivery_status=delivery_status,
                    delivery_duration_ms=delivery_duration_ms,
                    execution_kind=(
                        plan_step.execution_kind.value
                        if plan_step is not None
                        else None
                    ),
                    output_policy=(
                        plan_step.output_policy.value
                        if plan_step is not None
                        else None
                    ),
                ),
                component="step_completion",
                close=True,
                ignore_run_cancellation=True,
            )
            return True
        except asyncio.CancelledError:
            raise
        except (EventChannelClosedError, EventPublicationError, RuntimeError):
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
        output_policy: OutputPolicy | None = None,
        delivery_attempt: DeliveryAttempt | None = None,
        memory_error_code: str | None = None,
        formation_status: str | None = None,
        formation_error_code: str | None = None,
    ) -> StepCompletionResult:
        delivery_status = DeliveryStatus.NOT_APPLICABLE
        delivery_error_code: str | None = None
        if delivery_attempt is not None:
            delivery_status = delivery_attempt.delivery_status
            delivery_error_code = delivery_attempt.error_code
        return StepCompletionResult(
            step_id=claim.step_id,
            producer_agent_id=claim.preferred_agent,
            content_type=result.content_type if result is not None else None,
            char_count=result.char_count if result is not None else 0,
            complete=result.complete if result is not None else False,
            commit_status=commit_status,
            final_result_ready=final_result_ready,
            event_emitted=event_emitted,
            output_policy=output_policy,
            delivery_status=delivery_status,
            delivery_error_code=delivery_error_code,
            completion_error_code=(
                StepCompletionErrorCode.STEP_COMPLETION_EVENT_FAILED.value
                if error_code is StepCompletionErrorCode.STEP_COMPLETION_EVENT_FAILED
                else None
            ),
            memory_error_code=memory_error_code,
            formation_status=formation_status,
            formation_error_code=formation_error_code,
            error_code=error_code.value,
            safe_message=safe_message,
        )


__all__ = [
    "FinalMemoryWriter",
    "SemanticMemoryFormationRunner",
    "StepCommitStatus",
    "StepCompletionErrorCode",
    "StepCompletionResult",
    "StepResultCommitter",
]
