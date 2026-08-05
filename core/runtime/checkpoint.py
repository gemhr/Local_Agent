#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Per-run checkpoint barrier and coordinated immutable snapshot capture."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import math
import threading
import time
from uuid import uuid4

from core.runtime.activity import RuntimeActivityProvider
from core.runtime.cancellation import CancellationToken, RunCancelledError
from core.runtime.checkpoint_contract import (
    CheckpointBarrierState,
    CheckpointKind,
    CheckpointMode,
    CheckpointResult,
    CheckpointStatus,
    RuntimeActivitySnapshot,
    SnapshotPublicationEvidence,
)
from core.runtime.claim_gate import (
    SchedulerClaimGate,
    SchedulerClaimGateBusyError,
    SchedulerClaimGateClosedError,
)
from core.runtime.event_channel import JournalWatermarkError, RuntimeEventChannel
from core.runtime.fault_injection import FaultInjectionController
from core.runtime.fault_injection_contract import (
    FaultAction,
    FaultMatchContext,
    FaultPoint,
    InjectedFaultError,
)
from core.runtime.plan_fingerprint import PlanFingerprinter
from core.runtime.planning import Plan
from core.runtime.snapshot_contract import (
    AgentStateSnapshot,
    BudgetSnapshot,
    PlanSnapshot,
    RunSnapshot,
    RuntimeMetadata,
)
from core.runtime.snapshot_store import SnapshotStore, SnapshotStoreError
from core.runtime.snapshot_serialization import text_digest
from core.runtime.state import RunStatus, StepStatus


class CheckpointBarrier:
    """Small state machine around a single run's SchedulerClaimGate."""

    def __init__(self, claim_gate: SchedulerClaimGate) -> None:
        self.claim_gate = claim_gate
        self._lock = threading.Lock()
        self._state = CheckpointBarrierState.IDLE

    @property
    def state(self) -> CheckpointBarrierState:
        with self._lock:
            return self._state

    def transition(self, state: CheckpointBarrierState) -> None:
        if not isinstance(state, CheckpointBarrierState):
            raise TypeError("state must be CheckpointBarrierState")
        with self._lock:
            self._state = state

    async def pause(
        self,
        *,
        timeout: float | None,
        cancellation_token: CancellationToken | None,
    ) -> None:
        self.transition(CheckpointBarrierState.PAUSING_CLAIMS)
        await self.claim_gate.pause(
            timeout=timeout, cancellation_token=cancellation_token
        )

    def resume(self) -> None:
        self.transition(CheckpointBarrierState.RESUMING_CLAIMS)
        self.claim_gate.resume()
        self.transition(CheckpointBarrierState.IDLE)


class CheckpointCoordinator:
    """Single owner for one run's checkpoint lifecycle.

    Concurrent requests for the same instance are rejected deterministically;
    coordinators for different runs never share this lock.
    """

    def __init__(
        self,
        *,
        run_context,
        plan: Plan,
        agent_state,
        budget_ledger,
        event_channel: RuntimeEventChannel,
        snapshot_store: SnapshotStore,
        claim_gate: SchedulerClaimGate,
        activity_provider: RuntimeActivityProvider,
        runtime_metadata: RuntimeMetadata,
    ) -> None:
        if run_context.run_id != agent_state.run_id:
            raise ValueError("checkpoint run/state ownership mismatch")
        if event_channel.run_id != run_context.run_id:
            raise ValueError("checkpoint run/channel ownership mismatch")
        self.run_context = run_context
        self.plan = plan
        self.agent_state = agent_state
        self.budget_ledger = budget_ledger
        self.event_channel = event_channel
        self.snapshot_store = snapshot_store
        self.claim_gate = claim_gate
        self.activity_provider = activity_provider
        self.runtime_metadata = runtime_metadata
        self.barrier = CheckpointBarrier(claim_gate)
        self._checkpoint_lock = threading.Lock()

    async def capture(
        self,
        *,
        mode: CheckpointMode,
        checkpoint_kind: CheckpointKind,
        timeout: float | None,
        cancellation_token: CancellationToken | None = None,
        shutdown_token: CancellationToken | None = None,
        fault_controller: FaultInjectionController | None = None,
    ) -> CheckpointResult:
        if not isinstance(mode, CheckpointMode):
            raise TypeError("mode must be CheckpointMode")
        if not isinstance(checkpoint_kind, CheckpointKind):
            raise TypeError("checkpoint_kind must be CheckpointKind")
        _validate_timeout(timeout)
        if fault_controller is not None and not isinstance(
            fault_controller, FaultInjectionController
        ):
            raise TypeError(
                "fault_controller must be FaultInjectionController or None"
            )
        if not self._checkpoint_lock.acquire(blocking=False):
            return self._result(
                CheckpointStatus.ALREADY_IN_PROGRESS,
                checkpoint_kind,
                "CHECKPOINT_ALREADY_IN_PROGRESS",
            )
        paused = False
        started = time.monotonic()
        activity: RuntimeActivitySnapshot | None = None
        sequence: int | None = None
        snapshot_id: str | None = None
        publication_evidence: SnapshotPublicationEvidence | None = None
        try:
            active_token = _CombinedCancellationToken(
                self.run_context.cancellation_token,
                cancellation_token,
            )
            self._raise_if_cancelled(active_token, shutdown_token)
            remaining = _remaining(timeout, started)
            await self.barrier.pause(
                timeout=remaining,
                cancellation_token=active_token,
            )
            paused = True

            if mode is CheckpointMode.REQUIRE_QUIESCENT:
                self.barrier.transition(
                    CheckpointBarrierState.WAITING_FOR_QUIESCENCE
                )
                activity = await self._wait_for_quiescence(
                    timeout=timeout,
                    started=started,
                    cancellation_token=active_token,
                    shutdown_token=shutdown_token,
                )
                if activity is None:
                    latest = self.activity_provider.capture()
                    return CheckpointResult(
                        status=CheckpointStatus.NOT_QUIESCENT,
                        snapshot_id=None,
                        quiescent=False,
                        checkpoint_kind=checkpoint_kind,
                        journal_sequence=None,
                        activity_summary=latest,
                        safe_error_code="CHECKPOINT_NOT_QUIESCENT",
                    )
            else:
                activity = self.activity_provider.capture()

            self._raise_if_cancelled(active_token, shutdown_token)
            self.barrier.transition(CheckpointBarrierState.CAPTURING)
            transition_epoch = activity.state_event_transition_epoch
            # Once REQUIRE reaches zero, claims remain paused and tracked work cannot
            # grow. Audit mode records any live work explicitly as non-quiescent.
            capture_remaining = _remaining(timeout, started)
            if capture_remaining is not None and capture_remaining <= 0:
                raise TimeoutError("checkpoint capture timed out")
            watermark = self.event_channel.capture_journal_watermark()
            sequence = (
                await watermark
                if capture_remaining is None
                else await asyncio.wait_for(watermark, capture_remaining)
            )
            state_view = self.agent_state.snapshot_copy()
            state_snapshot = AgentStateSnapshot.from_agent_state(state_view)
            runtime_budget = self.budget_ledger.snapshot()
            budget_snapshot = BudgetSnapshot.from_runtime_snapshot(runtime_budget)
            activity = self.activity_provider.capture()
            if activity.state_event_transition_epoch != transition_epoch:
                activity = replace(
                    activity,
                    state_event_transition_observed=True,
                )
            quiescent = self._is_quiescent(
                activity, state_snapshot, budget_snapshot
            )
            if mode is CheckpointMode.REQUIRE_QUIESCENT and not quiescent:
                return CheckpointResult(
                    status=CheckpointStatus.NOT_QUIESCENT,
                    snapshot_id=None,
                    quiescent=False,
                    checkpoint_kind=checkpoint_kind,
                    journal_sequence=sequence,
                    activity_summary=activity,
                    safe_error_code="CHECKPOINT_NOT_QUIESCENT",
                )
            effective_kind = (
                CheckpointKind.NON_QUIESCENT_AUDIT
                if not quiescent
                else checkpoint_kind
            )
            self._validate_checkpoint_kind(
                effective_kind,
                quiescent=quiescent,
                state_snapshot=state_snapshot,
                journal_sequence=sequence,
            )
            snapshot = RunSnapshot.create(
                snapshot_id=uuid4().hex,
                run_id=self.run_context.run_id,
                trace_id=self.run_context.trace_id,
                plan_snapshot=PlanSnapshot.from_plan(self.plan),
                plan_fingerprint=PlanFingerprinter.fingerprint(self.plan),
                state_snapshot=state_snapshot,
                budget_snapshot=budget_snapshot,
                last_journal_sequence=sequence,
                runtime_metadata=self.runtime_metadata,
                checkpoint_kind=effective_kind,
                quiescent=quiescent,
                activity_snapshot=activity,
                created_at=datetime.now(UTC),
            )
            snapshot.verify_digest()
            snapshot_id = snapshot.snapshot_id
            publication_evidence = SnapshotPublicationEvidence(
                run_id_digest=text_digest(snapshot.run_id),
                snapshot_version=None,
                schema_version=snapshot.snapshot_schema_version,
                snapshot_digest=snapshot.payload_digest,
                persisted=False,
                partially_persisted=False,
                retry_allowed=False,
            )
            self._raise_if_cancelled(active_token, shutdown_token)
            self.barrier.transition(CheckpointBarrierState.SAVING)
            await self._execute_fault_point(
                fault_controller,
                FaultPoint.SNAPSHOT_BEFORE_SAVE,
                checkpoint_kind=effective_kind,
                cancellation_token=active_token,
                shutdown_token=shutdown_token,
                timeout=timeout,
                started=started,
            )
            self.snapshot_store.save(snapshot)
            publication_evidence = replace(
                publication_evidence, persisted=True
            )
            await self._execute_fault_point(
                fault_controller,
                FaultPoint.SNAPSHOT_AFTER_SAVE,
                checkpoint_kind=effective_kind,
                cancellation_token=active_token,
                shutdown_token=shutdown_token,
                timeout=timeout,
                started=started,
            )
            return CheckpointResult(
                status=(
                    CheckpointStatus.SAVED
                    if quiescent
                    else CheckpointStatus.SAVED_NON_QUIESCENT_AUDIT
                ),
                snapshot_id=snapshot.snapshot_id,
                quiescent=quiescent,
                checkpoint_kind=effective_kind,
                journal_sequence=sequence,
                activity_summary=activity,
                safe_error_code=None,
                snapshot_publication_evidence=publication_evidence,
            )
        except RunCancelledError:
            publication_evidence = _failed_publication_evidence(
                publication_evidence
            )
            return self._result(
                CheckpointStatus.CANCELLED,
                checkpoint_kind,
                "CHECKPOINT_CANCELLED",
                activity=activity,
                sequence=sequence,
                snapshot_id=(
                    snapshot_id
                    if publication_evidence is not None
                    and publication_evidence.persisted
                    else None
                ),
                publication_evidence=publication_evidence,
            )
        except TimeoutError:
            publication_evidence = _failed_publication_evidence(
                publication_evidence
            )
            return self._result(
                CheckpointStatus.TIMED_OUT,
                checkpoint_kind,
                "CHECKPOINT_TIMED_OUT",
                activity=activity,
                sequence=sequence,
                snapshot_id=(
                    snapshot_id
                    if publication_evidence is not None
                    and publication_evidence.persisted
                    else None
                ),
                publication_evidence=publication_evidence,
            )
        except InjectedFaultError:
            publication_evidence = _failed_publication_evidence(
                publication_evidence
            )
            persisted = (
                publication_evidence is not None
                and publication_evidence.persisted
            )
            return self._result(
                CheckpointStatus.STORE_FAILED,
                checkpoint_kind,
                (
                    "SNAPSHOT_SAVE_PARTIALLY_PERSISTED"
                    if persisted
                    else "SNAPSHOT_SAVE_INJECTED_FAILURE"
                ),
                activity=activity,
                sequence=sequence,
                snapshot_id=snapshot_id if persisted else None,
                publication_evidence=publication_evidence,
            )
        except (SchedulerClaimGateBusyError,):
            return self._result(
                CheckpointStatus.ALREADY_IN_PROGRESS,
                checkpoint_kind,
                "CHECKPOINT_ALREADY_IN_PROGRESS",
                activity=activity,
                sequence=sequence,
            )
        except SchedulerClaimGateClosedError:
            return self._result(
                CheckpointStatus.CANCELLED,
                checkpoint_kind,
                "CHECKPOINT_GATE_CLOSED",
                activity=activity,
                sequence=sequence,
            )
        except SnapshotStoreError:
            return self._result(
                CheckpointStatus.STORE_FAILED,
                checkpoint_kind,
                "SNAPSHOT_STORE_FAILED",
                activity=activity,
                sequence=sequence,
            )
        except (JournalWatermarkError, ValueError, TypeError):
            return self._result(
                CheckpointStatus.CORRUPTED,
                checkpoint_kind,
                "SNAPSHOT_CORRUPTED",
                activity=activity,
                sequence=sequence,
            )
        except Exception:
            return self._result(
                CheckpointStatus.CORRUPTED,
                checkpoint_kind,
                "CHECKPOINT_CAPTURE_FAILED",
                activity=activity,
                sequence=sequence,
            )
        finally:
            if paused:
                self.barrier.resume()
            elif self.barrier.state is not CheckpointBarrierState.IDLE:
                self.barrier.transition(CheckpointBarrierState.IDLE)
            self._checkpoint_lock.release()

    async def _wait_for_quiescence(
        self,
        *,
        timeout: float | None,
        started: float,
        cancellation_token: CancellationToken | None,
        shutdown_token: CancellationToken | None,
    ) -> RuntimeActivitySnapshot | None:
        while True:
            self._raise_if_cancelled(cancellation_token, shutdown_token)
            activity = self.activity_provider.capture()
            if activity.quiescent:
                return activity
            remaining = _remaining(timeout, started)
            if remaining is not None and remaining <= 0:
                return None
            await asyncio.sleep(
                0.005 if remaining is None else min(0.005, remaining)
            )

    @staticmethod
    def _is_quiescent(
        activity: RuntimeActivitySnapshot,
        state_snapshot: AgentStateSnapshot,
        budget_snapshot: BudgetSnapshot,
    ) -> bool:
        return (
            activity.quiescent
            and not any(step.in_flight for step in state_snapshot.step_states)
            and budget_snapshot.reservation_count == 0
            and all(value == 0 for value in budget_snapshot.reserved.values())
        )

    @staticmethod
    def _validate_checkpoint_kind(
        checkpoint_kind: CheckpointKind,
        *,
        quiescent: bool,
        state_snapshot: AgentStateSnapshot,
        journal_sequence: int,
    ) -> None:
        run_status = RunStatus(state_snapshot.run_status)
        running = any(
            step.status == StepStatus.RUNNING.value
            for step in state_snapshot.step_states
        )
        terminal = run_status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
        if checkpoint_kind is CheckpointKind.PRE_RUN:
            if (
                not quiescent
                or run_status is not RunStatus.CREATED
                or journal_sequence != 0
                or running
                or any(
                    step.execution_started
                    for step in state_snapshot.step_states
                )
            ):
                raise ValueError("checkpoint kind is inconsistent")
        elif checkpoint_kind is CheckpointKind.POST_PLAN_PRE_EXECUTION:
            if (
                not quiescent
                or run_status is not RunStatus.RUNNING
                or running
                or any(
                    step.execution_started
                    for step in state_snapshot.step_states
                )
            ):
                raise ValueError("checkpoint kind is inconsistent")
        elif checkpoint_kind is CheckpointKind.STEP_BOUNDARY:
            if not quiescent or terminal or running:
                raise ValueError("checkpoint kind is inconsistent")
        elif checkpoint_kind is CheckpointKind.TERMINAL:
            if not quiescent or not terminal or running:
                raise ValueError("checkpoint kind is inconsistent")
        elif checkpoint_kind is CheckpointKind.NON_QUIESCENT_AUDIT:
            if quiescent:
                raise ValueError("checkpoint kind is inconsistent")
        else:
            raise ValueError("checkpoint kind is unsupported")

    @staticmethod
    def _raise_if_cancelled(
        cancellation_token: CancellationToken | None,
        shutdown_token: CancellationToken | None,
    ) -> None:
        if cancellation_token is not None:
            cancellation_token.raise_if_cancelled()
        if shutdown_token is not None:
            shutdown_token.raise_if_cancelled()

    @staticmethod
    def _result(
        status: CheckpointStatus,
        checkpoint_kind: CheckpointKind,
        safe_error_code: str,
        *,
        activity: RuntimeActivitySnapshot | None = None,
        sequence: int | None = None,
        snapshot_id: str | None = None,
        publication_evidence: SnapshotPublicationEvidence | None = None,
    ) -> CheckpointResult:
        return CheckpointResult(
            status=status,
            snapshot_id=snapshot_id,
            quiescent=False,
            checkpoint_kind=checkpoint_kind,
            journal_sequence=sequence,
            activity_summary=activity,
            safe_error_code=safe_error_code,
            snapshot_publication_evidence=publication_evidence,
        )

    async def _execute_fault_point(
        self,
        controller: FaultInjectionController | None,
        point: FaultPoint,
        *,
        checkpoint_kind: CheckpointKind,
        cancellation_token: CancellationToken | None,
        shutdown_token: CancellationToken | None,
        timeout: float | None,
        started: float,
    ) -> None:
        if controller is None:
            return
        task = asyncio.create_task(
            controller.execute_if_matched(
                FaultMatchContext(
                    fault_point=point,
                    component="checkpoint_coordinator",
                    run_id_digest=text_digest(self.run_context.run_id),
                    operation_kind="SNAPSHOT_SAVE",
                    checkpoint_kind=checkpoint_kind.value,
                ),
                allowed_actions={
                    FaultAction.RAISE_TYPED_ERROR,
                    FaultAction.DELAY,
                    FaultAction.BLOCK_UNTIL_RELEASED,
                },
            )
        )
        try:
            while not task.done():
                self._raise_if_cancelled(cancellation_token, shutdown_token)
                remaining = _remaining(timeout, started)
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("checkpoint fault wait timed out")
                await asyncio.wait(
                    {task},
                    timeout=(
                        0.01 if remaining is None else min(0.01, remaining)
                    ),
                )
            await task
        except BaseException:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise


def _failed_publication_evidence(
    evidence: SnapshotPublicationEvidence | None,
) -> SnapshotPublicationEvidence | None:
    if evidence is None or not evidence.persisted:
        return evidence
    return replace(
        evidence,
        partially_persisted=True,
        retry_allowed=False,
    )


def default_runtime_metadata() -> RuntimeMetadata:
    return RuntimeMetadata(
        runtime_schema_version=1,
        runtime_mode="coordinated",
        planner_version="1",
        scheduler_version="1",
        model_routing_policy_version="1",
        tool_contract_version="1",
        retrieval_contract_version="1",
        event_schema_version="2",
        journal_schema_version="2",
    )


def _validate_timeout(value: float | None) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("timeout must be a finite non-negative number or None")


def _remaining(timeout: float | None, started: float) -> float | None:
    if timeout is None:
        return None
    return max(0.0, float(timeout) - (time.monotonic() - started))


class _CombinedCancellationToken:
    """Minimal duck-typed token that preserves both run and request cancellation."""

    def __init__(self, *tokens: CancellationToken | None) -> None:
        self._tokens = tuple(
            token
            for index, token in enumerate(tokens)
            if token is not None and token not in tokens[:index]
        )

    def raise_if_cancelled(self) -> None:
        for token in self._tokens:
            token.raise_if_cancelled()


__all__ = [
    "CheckpointBarrier",
    "CheckpointCoordinator",
    "default_runtime_metadata",
]
