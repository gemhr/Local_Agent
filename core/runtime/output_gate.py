#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run-scoped OutputGate: the single at-most-once final delivery owner.

Ownership:
    - one gate per dynamic Run, owned by the Coordinator / Dynamic RunScope;
    - only the StepCompletionPipeline may call ``attempt_publish``;
    - Driver, Adapter, Synthesis and Scheduler have no call authority;
    - the gate is never snapshotted, checkpointed, journaled or recovered;
    - after a terminal attempt the gate can never publish again.

The gate only publishes ``OUTPUT_DELTA`` with the final StepResult content.
It never writes Memory, never calls a model, never joins specialist results,
and never returns raw content into any report.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import threading
import time
from typing import Callable
import asyncio

from core.runtime.event_channel import (
    EventChannelClosedError,
    EventPublicationError,
)
from core.runtime.event_emitter import RunEventEmitter, StepEventEmitter
from core.runtime.events import OutputDeltaPayload, RuntimeEventType
from core.runtime.planning import OutputPolicy, Plan
from core.runtime.scheduler import StepClaim
from core.runtime.state import AgentState, RunStatus, StepStatus
from core.runtime.step_result import StepResult
from core.runtime.step_result_store import (
    StepResultStore,
    StepResultStoreError,
)
from core.runtime.fault_injection import (
    FaultInjectionController,
    evaluate_sync_fault,
)
from core.runtime.fault_injection_contract import FaultPoint
from core.runtime.trace_contract import (
    RUNTIME_OUTPUT_DELIVERY_SPAN,
    set_span_attributes,
)
from core.runtime.tracing import current_trace_context, start_span_safely


class OutputGateState(str, Enum):
    """At-most-once attempt state machine.

    NOT_STARTED
      -> PUBLISHING
           -> PUBLISHED
           -> FAILED
           -> OUTCOME_UNKNOWN
    """

    NOT_STARTED = "NOT_STARTED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class DeliveryStatus(str, Enum):
    """Layer of the final delivery attempt inside one Run.

    - INTERNAL steps: NOT_APPLICABLE (the gate is never called).
    - Gate publish returned normally: DELIVERED.
    - Failure provably before journal append: FAILED.
    - Journal written / partial persistence / cannot prove not committed:
      OUTCOME_UNKNOWN.
    """

    NOT_APPLICABLE = "NOT_APPLICABLE"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class OutputGateErrorCode(str, Enum):
    OUTPUT_GATE_DUPLICATE_ATTEMPT = "OUTPUT_GATE_DUPLICATE_ATTEMPT"
    OUTPUT_GATE_INTERNAL_STEP = "OUTPUT_GATE_INTERNAL_STEP"
    OUTPUT_GATE_NOT_FINAL = "OUTPUT_GATE_NOT_FINAL"
    OUTPUT_GATE_STEP_NOT_CLAIMED = "OUTPUT_GATE_STEP_NOT_CLAIMED"
    OUTPUT_GATE_STEP_NOT_SUCCEEDED = "OUTPUT_GATE_STEP_NOT_SUCCEEDED"
    OUTPUT_GATE_STORE_NOT_READABLE = "OUTPUT_GATE_STORE_NOT_READABLE"
    OUTPUT_GATE_STORE_SEALED = "OUTPUT_GATE_STORE_SEALED"
    OUTPUT_GATE_RUN_NOT_ACTIVE = "OUTPUT_GATE_RUN_NOT_ACTIVE"
    OUTPUT_GATE_CLAIM_MISMATCH = "OUTPUT_GATE_CLAIM_MISMATCH"
    OUTPUT_GATE_CLOSED = "OUTPUT_GATE_CLOSED"


class OutputGateRejectionError(RuntimeError):
    """Fail-closed authorization rejection without raw content."""

    def __init__(self, error_code: OutputGateErrorCode, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code.value})")


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    """Safe control-plane outcome of one gate attempt; never carries text."""

    step_id: str
    output_policy: OutputPolicy | None
    delivery_status: DeliveryStatus
    error_code: str | None = None
    safe_message: str = ""

    @property
    def delivered(self) -> bool:
        return self.delivery_status is DeliveryStatus.DELIVERED


class OutputGate:
    """Single-use per-Run final delivery gate.

    Authorization checks (claim, frozen plan, StepState SUCCEEDED, Store
    READABLE, FINAL policy, unique final source, not attempted, Store open,
    Run active) are validated before any PUBLISHING transition, so a rejected
    attempt never consumes the gate.
    """

    def __init__(
        self,
        *,
        plan: Plan,
        store: StepResultStore,
        event_emitter: RunEventEmitter | None,
        state_getter: Callable[[], AgentState] | None = None,
        run_active: Callable[[], bool] | None = None,
        span_recorder=None,
        metrics_recorder=None,
        fault_controller: FaultInjectionController | None = None,
    ) -> None:
        if not isinstance(plan, Plan):
            raise TypeError("OutputGate 需要冻结的 Plan")
        if not isinstance(store, StepResultStore):
            raise TypeError("OutputGate 需要 StepResultStore")
        if event_emitter is not None and not isinstance(
            event_emitter, RunEventEmitter
        ):
            raise TypeError("OutputGate event_emitter 必须合法")
        if state_getter is not None and not callable(state_getter):
            raise TypeError("state_getter 必须可调用")
        if run_active is not None and not callable(run_active):
            raise TypeError("run_active 必须可调用")
        if fault_controller is not None and not isinstance(
            fault_controller, FaultInjectionController
        ):
            raise TypeError("fault_controller 必须是 FaultInjectionController 或 None")
        self._plan = plan
        self._store = store
        self._event_emitter = event_emitter
        self._state_getter = state_getter
        self._run_active = run_active
        self._span_recorder = span_recorder
        self._metrics_recorder = metrics_recorder
        self._fault_controller = fault_controller
        finals = tuple(
            step
            for step in plan.steps
            if step.output_policy is not OutputPolicy.INTERNAL
        )
        if len(finals) != 1:
            raise ValueError("Plan 必须具有唯一 final Step")
        self._final_step_id = finals[0].step_id
        self._state = OutputGateState.NOT_STARTED
        self._lock = threading.Lock()
        self._last_attempt: DeliveryAttempt | None = None

    @property
    def state(self) -> OutputGateState:
        with self._lock:
            return self._state

    @property
    def final_step_id(self) -> str:
        return self._final_step_id

    @property
    def last_attempt(self) -> DeliveryAttempt | None:
        with self._lock:
            return self._last_attempt

    @property
    def attempted(self) -> bool:
        with self._lock:
            return self._state is not OutputGateState.NOT_STARTED

    @property
    def terminal(self) -> bool:
        with self._lock:
            return self._state in {
                OutputGateState.PUBLISHED,
                OutputGateState.FAILED,
                OutputGateState.OUTCOME_UNKNOWN,
            }

    def _plan_step(self, step_id: str):
        for step in self._plan.steps:
            if step.step_id == step_id:
                return step
        return None

    def _authorize_locked(
        self,
        claim: StepClaim,
        result: StepResult,
    ) -> tuple[OutputPolicy, object] | OutputGateRejectionError:
        plan_step = self._plan_step(claim.step_id)
        if plan_step is None:
            return OutputGateRejectionError(
                OutputGateErrorCode.OUTPUT_GATE_NOT_FINAL,
                "Step 不在冻结 Plan 中",
            )
        if claim.step_id != result.step_id:
            return OutputGateRejectionError(
                OutputGateErrorCode.OUTPUT_GATE_CLAIM_MISMATCH,
                "claim 与 result Step 不一致",
            )
        if claim.preferred_agent != plan_step.preferred_agent:
            return OutputGateRejectionError(
                OutputGateErrorCode.OUTPUT_GATE_CLAIM_MISMATCH,
                "claim 与 Plan Step Agent 不一致",
            )
        if plan_step.output_policy not in {
            OutputPolicy.FINAL_PASSTHROUGH,
            OutputPolicy.FINAL_SYNTHESIS,
        }:
            return OutputGateRejectionError(
                OutputGateErrorCode.OUTPUT_GATE_INTERNAL_STEP,
                "INTERNAL Step 永远不得调用 OutputGate",
            )
        if plan_step.step_id != self._final_step_id:
            return OutputGateRejectionError(
                OutputGateErrorCode.OUTPUT_GATE_NOT_FINAL,
                "仅唯一 final source 可以发布",
            )
        state = (
            self._state_getter() if self._state_getter is not None else None
        )
        if state is not None:
            step_state = state.steps.get(claim.step_id)
            if (
                step_state is None
                or step_state.status is not StepStatus.SUCCEEDED
            ):
                return OutputGateRejectionError(
                    OutputGateErrorCode.OUTPUT_GATE_STEP_NOT_SUCCEEDED,
                    "Step 尚未 SUCCEEDED",
                )
        if self._store.is_sealed:
            return OutputGateRejectionError(
                OutputGateErrorCode.OUTPUT_GATE_STORE_SEALED,
                "Store 已 seal，拒绝发布",
            )
        if not self._store.has_readable(claim.step_id):
            return OutputGateRejectionError(
                OutputGateErrorCode.OUTPUT_GATE_STORE_NOT_READABLE,
                "Store entry 尚未 READABLE",
            )
        if self._run_active is not None and not self._run_active():
            return OutputGateRejectionError(
                OutputGateErrorCode.OUTPUT_GATE_RUN_NOT_ACTIVE,
                "Run 已不在允许完成的活动状态",
            )
        return plan_step.output_policy, state

    def close(self) -> None:
        """Irreversibly close the gate; later attempts fail closed."""
        with self._lock:
            if self._state is OutputGateState.NOT_STARTED:
                self._state = OutputGateState.PUBLISHING
                self._state = OutputGateState.FAILED

    async def attempt_publish(
        self,
        *,
        claim: StepClaim,
        result: StepResult,
    ) -> DeliveryAttempt:
        """Publish the final candidate exactly once (at-most-once).

        Returns a safe DeliveryAttempt; never raises EventPublicationError.
        Any duplicate/closed attempt fails closed with
        OUTPUT_GATE_DUPLICATE_ATTEMPT.
        """
        if not isinstance(claim, StepClaim):
            raise TypeError("attempt_publish 需要 StepClaim")
        if not isinstance(result, StepResult):
            raise TypeError("attempt_publish 需要 StepResult")
        with self._lock:
            if self._state is OutputGateState.PUBLISHING:
                return DeliveryAttempt(
                    claim.step_id,
                    None,
                    DeliveryStatus.FAILED,
                    OutputGateErrorCode.OUTPUT_GATE_DUPLICATE_ATTEMPT.value,
                    "publish 尝试正在进行中，拒绝并发第二次发布",
                )
            if self._state is not OutputGateState.NOT_STARTED:
                return DeliveryAttempt(
                    claim.step_id,
                    None,
                    DeliveryStatus.FAILED,
                    OutputGateErrorCode.OUTPUT_GATE_DUPLICATE_ATTEMPT.value,
                    "Gate 已终态，拒绝重复 publish 尝试",
                )
            authorized = self._authorize_locked(claim, result)
            if isinstance(authorized, OutputGateRejectionError):
                return DeliveryAttempt(
                    claim.step_id,
                    None,
                    DeliveryStatus.FAILED,
                    authorized.error_code.value,
                    authorized.safe_message,
                )
            output_policy, _state = authorized
            self._state = OutputGateState.PUBLISHING

        publish_started = time.monotonic()
        delivery_span = None
        if self._event_emitter is not None and self._span_recorder is not None:
            delivery_span = start_span_safely(
                self._span_recorder,
                trace_id=self._event_emitter.trace_id,
                run_id=self._event_emitter.run_id,
                component="output_gate",
                operation=RUNTIME_OUTPUT_DELIVERY_SPAN,
                step_id=claim.step_id,
                parent_context=current_trace_context(),
            )
        partially_persisted = False
        try:
            await self._publish_output(claim, result)
        except EventPublicationError as exc:
            partially_persisted = exc.partially_persisted
            status = (
                DeliveryStatus.OUTCOME_UNKNOWN
                if exc.partially_persisted
                else DeliveryStatus.FAILED
            )
            error_code = (
                "FINAL_OUTPUT_DELIVERY_UNKNOWN"
                if exc.partially_persisted
                else "FINAL_OUTPUT_DELIVERY_FAILED"
            )
            attempt = DeliveryAttempt(
                claim.step_id,
                output_policy,
                status,
                error_code,
                (
                    "final output 可能已 journaled，无法确认消费者是否收到"
                    if exc.partially_persisted
                    else "final output 在 journal append 前失败"
                ),
            )
        except (EventChannelClosedError, RuntimeError):
            attempt = DeliveryAttempt(
                claim.step_id,
                output_policy,
                DeliveryStatus.FAILED,
                "FINAL_OUTPUT_DELIVERY_FAILED",
                "EventChannel 不可用，final output 未能发布",
            )
        except asyncio.CancelledError:
            attempt = DeliveryAttempt(
                claim.step_id,
                output_policy,
                DeliveryStatus.OUTCOME_UNKNOWN,
                "FINAL_OUTPUT_DELIVERY_UNKNOWN",
                "publish 被取消，无法确认是否已持久化",
            )
        except Exception:
            attempt = DeliveryAttempt(
                claim.step_id,
                output_policy,
                DeliveryStatus.OUTCOME_UNKNOWN,
                "FINAL_OUTPUT_DELIVERY_UNKNOWN",
                "publish 遇到未知异常，无法确认是否已持久化",
            )
        else:
            attempt = DeliveryAttempt(
                claim.step_id,
                output_policy,
                DeliveryStatus.DELIVERED,
            )
        delivery_duration_ms = max(
            0, int((time.monotonic() - publish_started) * 1000)
        )
        with self._lock:
            self._state = {
                DeliveryStatus.DELIVERED: OutputGateState.PUBLISHED,
                DeliveryStatus.FAILED: OutputGateState.FAILED,
                DeliveryStatus.OUTCOME_UNKNOWN: (
                    OutputGateState.OUTCOME_UNKNOWN
                ),
            }[attempt.delivery_status]
            self._last_attempt = attempt
            terminal_state = self._state.value
        if delivery_span is not None:
            set_span_attributes(
                delivery_span,
                final_step_id=claim.step_id,
                output_policy=(
                    output_policy.value if output_policy is not None else None
                ),
                delivery_status=attempt.delivery_status.value,
                gate_terminal_state=terminal_state,
                publish_attempt_count=1,
                partially_persisted=partially_persisted,
                output_char_count=result.char_count,
            )
            if attempt.delivery_status is DeliveryStatus.DELIVERED:
                delivery_span.end_ok()
            else:
                delivery_span.end_error(attempt.error_code or "DELIVERY_FAILED")
        self._record_delivery_metrics(
            attempt=attempt,
            duration_ms=delivery_duration_ms,
            partially_persisted=partially_persisted,
        )
        return attempt

    def _record_delivery_metrics(
        self,
        *,
        attempt: DeliveryAttempt,
        duration_ms: int,
        partially_persisted: bool,
    ) -> None:
        recorder = self._metrics_recorder
        if recorder is None:
            return
        status = attempt.delivery_status.value
        error_code = attempt.error_code or "OK"
        try:
            recorder.increment_counter(
                "runtime_output_delivery_total",
                labels={"status": status, "error_code": error_code},
            )
            recorder.observe_histogram(
                "runtime_output_delivery_duration_seconds",
                max(0.0, duration_ms / 1000.0),
                labels={"status": status},
            )
            if partially_persisted:
                recorder.increment_counter(
                    "runtime_output_partial_persisted_total"
                )
        except Exception:
            return

    async def _publish_output(
        self,
        claim: StepClaim,
        result: StepResult,
    ) -> None:
        evaluate_sync_fault(
            self._fault_controller,
            point=FaultPoint.OUTPUT_BEFORE_PUBLISH,
            component="output_gate",
            run_id=self._store.run_id,
            step_id=claim.step_id,
            operation_kind="OUTPUT_DELTA",
        )
        if self._event_emitter is None:
            raise EventChannelClosedError("OutputGate 没有可用的 EventEmitter")
        emitter: StepEventEmitter = self._event_emitter.for_step(claim.step_id)
        if emitter.is_closed:
            raise RuntimeError("StepEmitter 已关闭，拒绝再次发布")
        await emitter.emit(
            RuntimeEventType.OUTPUT_DELTA,
            OutputDeltaPayload(result.content),
            component="output_gate",
        )

    def __repr__(self) -> str:
        return (
            "OutputGate("
            f"final_step_id={self._final_step_id!r}, "
            f"state={self.state.value!r}, "
            f"attempted={self.attempted!r})"
        )


__all__ = [
    "DeliveryAttempt",
    "DeliveryStatus",
    "OutputGate",
    "OutputGateErrorCode",
    "OutputGateRejectionError",
    "OutputGateState",
]
