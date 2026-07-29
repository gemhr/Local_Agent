#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Read-only snapshot/plan/journal recovery validation."""

from __future__ import annotations

from core.runtime.checkpoint_contract import CheckpointKind
from core.runtime.event_journal import (
    MAX_READ_LIMIT,
    JournalError,
    JournalErrorCode,
    RunEventJournal,
)
from core.runtime.journal_tail_reducer import (
    JournalTailReductionError,
    JournalTailValidationError,
    JournalTailValidator,
    LimitedJournalTailReducer,
)
from core.runtime.plan_fingerprint import PlanFingerprinter
from core.runtime.planning import Plan
from core.runtime.recovery_contract import (
    RecoveryAssessment,
    RecoveryProjection,
    RecoveryReason,
    RecoveryStatus,
    ResumeDataAvailability,
    ToolRecoveryDecisionStatus,
)
from core.runtime.snapshot_contract import (
    SNAPSHOT_SCHEMA_VERSION,
    RunSnapshot,
)
from core.runtime.snapshot_serialization import sha256_digest
from core.runtime.snapshot_store import (
    SnapshotErrorCode,
    SnapshotStore,
    SnapshotStoreError,
)
from core.runtime.state import RunStatus, StepStatus
from core.runtime.tool_recovery import ToolRecoveryDecisionEngine


_TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
)


class RecoveryValidator:
    """Assess recovery safety without starting or invoking any runtime adapter."""

    def __init__(
        self,
        *,
        snapshot_store: SnapshotStore | None = None,
        journal: RunEventJournal,
    ) -> None:
        self.snapshot_store = snapshot_store
        self.journal = journal

    def validate(
        self,
        *,
        snapshot_id: str,
        current_plan: Plan,
    ) -> RecoveryAssessment:
        """Load by ID and assess; ``assess`` is an equivalent spelling."""
        return self.assess(snapshot_id=snapshot_id, current_plan=current_plan)

    def assess(
        self,
        *,
        snapshot_id: str,
        current_plan: Plan,
    ) -> RecoveryAssessment:
        if self.snapshot_store is None:
            raise ValueError("snapshot_store is required when loading by ID")
        try:
            snapshot = self.snapshot_store.get(snapshot_id)
        except SnapshotStoreError as exc:
            if exc.error_code is SnapshotErrorCode.SNAPSHOT_SCHEMA_UNSUPPORTED:
                return _failure(
                    status=RecoveryStatus.INCOMPATIBLE_SCHEMA,
                    reason=RecoveryReason.SNAPSHOT_SCHEMA_UNSUPPORTED,
                    snapshot_id=snapshot_id,
                )
            if exc.error_code is SnapshotErrorCode.SNAPSHOT_CORRUPTED:
                return _failure(
                    status=RecoveryStatus.CORRUPTED,
                    reason=RecoveryReason.SNAPSHOT_DIGEST_INVALID,
                    snapshot_id=snapshot_id,
                )
            return _failure(
                status=RecoveryStatus.UNSUPPORTED,
                reason=RecoveryReason.SNAPSHOT_NOT_FOUND,
                snapshot_id=snapshot_id,
            )
        except (ValueError, TypeError):
            return _failure(
                status=RecoveryStatus.CORRUPTED,
                reason=RecoveryReason.SNAPSHOT_INTERNAL_INCONSISTENCY,
                snapshot_id=snapshot_id,
            )
        if snapshot is None:
            return _failure(
                status=RecoveryStatus.UNSUPPORTED,
                reason=RecoveryReason.SNAPSHOT_NOT_FOUND,
                snapshot_id=snapshot_id,
            )
        return self.assess_snapshot(snapshot=snapshot, current_plan=current_plan)

    def assess_snapshot(
        self,
        *,
        snapshot: RunSnapshot,
        current_plan: Plan,
    ) -> RecoveryAssessment:
        if not isinstance(snapshot, RunSnapshot):
            raise TypeError("snapshot must be RunSnapshot")
        identity = {
            "snapshot_id": _safe_identity(snapshot, "snapshot_id"),
            "run_id": _safe_identity(snapshot, "run_id"),
            "snapshot_sequence": _safe_sequence(snapshot),
        }

        # 1. Schema.
        if (
            isinstance(snapshot.snapshot_schema_version, bool)
            or not isinstance(snapshot.snapshot_schema_version, int)
            or snapshot.snapshot_schema_version != SNAPSHOT_SCHEMA_VERSION
        ):
            return _failure(
                status=RecoveryStatus.INCOMPATIBLE_SCHEMA,
                reason=RecoveryReason.SNAPSHOT_SCHEMA_UNSUPPORTED,
                **identity,
            )

        # 2. Digest. Do not call verify_digest here because it also performs
        # lower-priority semantic checks.
        try:
            digest_valid = (
                sha256_digest(snapshot.digest_source())
                == snapshot.payload_digest
            )
        except (ValueError, TypeError):
            digest_valid = False
        if not digest_valid:
            return _failure(
                status=RecoveryStatus.CORRUPTED,
                reason=RecoveryReason.SNAPSHOT_DIGEST_INVALID,
                **identity,
            )

        # 3-4. Internal cross-fields and activity snapshot.
        try:
            snapshot.validate_consistency()
            _validate_activity(snapshot)
            checkpoint_kind = _validate_checkpoint_before_tail(snapshot)
        except _UnsupportedCheckpoint:
            return _failure(
                status=RecoveryStatus.UNSUPPORTED,
                reason=RecoveryReason.UNSUPPORTED_CHECKPOINT_KIND,
                **identity,
            )
        except (ValueError, TypeError):
            return _failure(
                status=RecoveryStatus.CORRUPTED,
                reason=RecoveryReason.SNAPSHOT_INTERNAL_INCONSISTENCY,
                **identity,
            )

        # 5-8. The persisted PlanSnapshot and the caller's current Plan are
        # independently fingerprinted.
        try:
            persisted_fingerprint = PlanFingerprinter.fingerprint_snapshot(
                snapshot.plan_snapshot
            )
        except (ValueError, TypeError):
            return _failure(
                status=RecoveryStatus.CORRUPTED,
                reason=RecoveryReason.SNAPSHOT_INTERNAL_INCONSISTENCY,
                **identity,
            )
        if persisted_fingerprint != snapshot.plan_fingerprint:
            return _failure(
                status=RecoveryStatus.CORRUPTED,
                reason=RecoveryReason.SNAPSHOT_INTERNAL_INCONSISTENCY,
                **identity,
            )
        try:
            current_fingerprint = PlanFingerprinter.fingerprint(current_plan)
        except (ValueError, TypeError):
            return _failure(
                status=RecoveryStatus.PLAN_MISMATCH,
                reason=RecoveryReason.PLAN_FINGERPRINT_MISMATCH,
                **identity,
            )
        if current_fingerprint != snapshot.plan_fingerprint:
            return _failure(
                status=RecoveryStatus.PLAN_MISMATCH,
                reason=RecoveryReason.PLAN_FINGERPRINT_MISMATCH,
                **identity,
            )

        # 9-10. Journal alignment. None means an empty journal, sequence zero.
        try:
            journal_last = self.journal.last_sequence(snapshot.run_id) or 0
        except JournalError as exc:
            return _journal_read_failure(exc, **identity)
        except (ValueError, TypeError):
            return _failure(
                status=RecoveryStatus.CORRUPTED,
                reason=RecoveryReason.JOURNAL_RECORD_CORRUPTED,
                **identity,
            )
        if snapshot.last_journal_sequence > journal_last:
            return _failure(
                status=RecoveryStatus.JOURNAL_GAP_OR_CONFLICT,
                reason=RecoveryReason.SNAPSHOT_SEQUENCE_AHEAD_OF_JOURNAL,
                journal_last_sequence=journal_last,
                **identity,
            )

        try:
            records = self._read_tail(
                snapshot.run_id,
                snapshot.last_journal_sequence,
                journal_last,
            )
        except JournalError as exc:
            return _journal_read_failure(
                exc, journal_last_sequence=journal_last, **identity
            )
        except _JournalChanged:
            return _failure(
                status=RecoveryStatus.JOURNAL_GAP_OR_CONFLICT,
                reason=RecoveryReason.JOURNAL_SEQUENCE_CONFLICT,
                journal_last_sequence=journal_last,
                **identity,
            )
        except (ValueError, TypeError):
            return _failure(
                status=RecoveryStatus.CORRUPTED,
                reason=RecoveryReason.JOURNAL_RECORD_CORRUPTED,
                journal_last_sequence=journal_last,
                **identity,
            )

        # 11. Tail validation reuses JournalRecord.verify; numeric gaps are
        # intentionally legal.
        try:
            validated = JournalTailValidator.validate(
                run_id=snapshot.run_id,
                snapshot_sequence=snapshot.last_journal_sequence,
                records=records,
            )
        except JournalTailValidationError as exc:
            return _failure(
                status=exc.status,
                reason=exc.reason,
                journal_last_sequence=journal_last,
                **identity,
            )

        try:
            _validate_checkpoint_after_tail(
                snapshot, checkpoint_kind, validated.terminal_event_seen
            )
        except ValueError:
            return _failure(
                status=RecoveryStatus.CORRUPTED,
                reason=RecoveryReason.SNAPSHOT_INTERNAL_INCONSISTENCY,
                journal_last_sequence=journal_last,
                **identity,
            )

        # A terminal snapshot already includes its terminal fact at the
        # watermark. New business records after it violate terminal ordering.
        if (
            RunStatus(snapshot.run_status) in _TERMINAL_RUN_STATUSES
            and validated.records
        ):
            return _failure(
                status=RecoveryStatus.JOURNAL_GAP_OR_CONFLICT,
                reason=RecoveryReason.JOURNAL_TERMINAL_CONFLICT,
                journal_last_sequence=journal_last,
                **identity,
            )

        # 12-13. Limited metadata-only reduce and Tool evidence collection.
        try:
            reduced = LimitedJournalTailReducer.reduce(
                snapshot, validated.records
            )
        except JournalTailReductionError as exc:
            return _failure(
                status=exc.status,
                reason=exc.reason,
                journal_last_sequence=journal_last,
                **identity,
            )
        except (ValueError, TypeError):
            return _failure(
                status=RecoveryStatus.CORRUPTED,
                reason=RecoveryReason.JOURNAL_RECORD_CORRUPTED,
                journal_last_sequence=journal_last,
                **identity,
            )

        projection = reduced.projection
        if (
            projection.terminal_event_seen
            and any(
                item.status == StepStatus.RUNNING.value
                for item in projection.step_states.values()
            )
        ):
            return _failure(
                status=RecoveryStatus.JOURNAL_GAP_OR_CONFLICT,
                reason=RecoveryReason.JOURNAL_TERMINAL_CONFLICT,
                journal_last_sequence=journal_last,
                last_applied_sequence=projection.last_applied_sequence,
                reduced_projection=projection,
                tool_evidence=reduced.tool_evidence,
                **identity,
            )

        tool_decisions = ToolRecoveryDecisionEngine.decide(
            reduced.tool_evidence, projection
        )
        resume_data = _resume_data_availability(snapshot, projection)
        reasons = _snapshot_reconciliation_reasons(snapshot)
        for reason in reduced.reconciliation_reasons:
            if reason not in {
                RecoveryReason.TOOL_SIDE_EFFECT_EVIDENCE,
                RecoveryReason.TOOL_OUTCOME_UNKNOWN,
                RecoveryReason.TOOL_COMPENSATION_FAILED,
            }:
                _append_reason(reasons, reason)
        _append_tool_decision_reasons(
            reasons, tool_decisions, reduced.tool_evidence
        )
        blocking_ids = tuple(
            sorted(
                {
                    item.step_id
                    for item in snapshot.step_states
                    if item.status == StepStatus.RUNNING.value
                }
                | {
                    step_id
                    for step_id, item in projection.step_states.items()
                    if item.status == StepStatus.RUNNING.value
                }
            )
        )
        if blocking_ids:
            _append_reason(reasons, RecoveryReason.RUNNING_STEP_PRESENT)

        checkpoint_kind = CheckpointKind(snapshot.checkpoint_kind)
        dependency_output_blocked = (
            checkpoint_kind is CheckpointKind.STEP_BOUNDARY
            and resume_data.pending_steps_present
            and resume_data.completed_dependency_results_required
            and not resume_data.completed_dependency_results_available
        )
        if dependency_output_blocked:
            _append_reason(
                reasons, RecoveryReason.DEPENDENCY_OUTPUT_UNAVAILABLE
            )
            _append_reason(
                reasons,
                RecoveryReason.STEP_RESULT_REHYDRATION_UNSUPPORTED,
            )
            status = RecoveryStatus.UNSUPPORTED
            resume_ready = False
        elif reasons:
            status = RecoveryStatus.REQUIRES_RECONCILIATION
            resume_ready = False
        elif RunStatus(projection.run_status) in _TERMINAL_RUN_STATUSES:
            status = RecoveryStatus.TERMINAL
            resume_ready = False
            _append_reason(reasons, RecoveryReason.RUN_TERMINAL)
        else:
            status = RecoveryStatus.RESUMABLE
            resume_ready = True
            _append_reason(
                reasons, RecoveryReason.SAFE_RESUME_PREREQUISITES_MET
            )
        return RecoveryAssessment(
            status=status,
            snapshot_id=snapshot.snapshot_id,
            run_id=snapshot.run_id,
            snapshot_sequence=snapshot.last_journal_sequence,
            journal_last_sequence=journal_last,
            last_applied_sequence=projection.last_applied_sequence,
            reasons=tuple(reasons),
            blocking_step_ids=blocking_ids,
            reduced_projection=projection,
            tool_evidence=reduced.tool_evidence,
            resume_prerequisites_satisfied=resume_ready,
            tool_decisions=tool_decisions,
            resume_data_availability=resume_data,
            output_reconstruction_supported=False,
        )

    def _read_tail(
        self, run_id: str, after: int, expected_last: int
    ) -> tuple:
        records: list = []
        cursor = after
        while cursor < expected_last:
            page = self.journal.read_after(run_id, cursor, MAX_READ_LIMIT)
            page = tuple(
                record for record in page if record.sequence <= expected_last
            )
            if not page:
                raise _JournalChanged()
            records.extend(page)
            next_cursor = page[-1].sequence
            if next_cursor <= cursor:
                raise _JournalChanged()
            cursor = next_cursor
        latest = self.journal.last_sequence(run_id) or 0
        if latest != expected_last:
            raise _JournalChanged()
        return tuple(records)


def assess_recovery(
    *,
    snapshot: RunSnapshot,
    current_plan: Plan,
    journal: RunEventJournal,
) -> RecoveryAssessment:
    """Convenience entry point for callers that already loaded a snapshot."""
    return RecoveryValidator(journal=journal).assess_snapshot(
        snapshot=snapshot, current_plan=current_plan
    )


def _validate_activity(snapshot: RunSnapshot) -> None:
    activity = snapshot.activity_snapshot
    if activity is None:
        raise ValueError("checkpoint activity is required")
    running_steps = sum(
        item.status == StepStatus.RUNNING.value for item in snapshot.step_states
    )
    if activity.running_step_count != running_steps:
        raise ValueError("activity/state running count mismatch")
    if (
        activity.budget_reservation_count
        != snapshot.budget_snapshot.reservation_count
    ):
        raise ValueError("activity/budget reservation count mismatch")
    if snapshot.quiescent != activity.quiescent:
        raise ValueError("snapshot/activity quiescence mismatch")


def _validate_checkpoint_before_tail(snapshot: RunSnapshot) -> CheckpointKind:
    try:
        kind = CheckpointKind(snapshot.checkpoint_kind)
    except (TypeError, ValueError):
        raise _UnsupportedCheckpoint() from None
    statuses = tuple(StepStatus(item.status) for item in snapshot.step_states)
    run_status = RunStatus(snapshot.run_status)
    if kind is CheckpointKind.OBSERVATION:
        raise _UnsupportedCheckpoint()
    if kind is CheckpointKind.PRE_RUN:
        if (
            run_status is not RunStatus.CREATED
            or any(item.execution_started for item in snapshot.step_states)
            or StepStatus.RUNNING in statuses
            or snapshot.last_journal_sequence != 0
            or not snapshot.quiescent
        ):
            raise ValueError("invalid PRE_RUN checkpoint")
    elif kind is CheckpointKind.STEP_BOUNDARY:
        if (
            not snapshot.quiescent
            or run_status in _TERMINAL_RUN_STATUSES
            or StepStatus.RUNNING in statuses
        ):
            raise ValueError("invalid STEP_BOUNDARY checkpoint")
    elif kind is CheckpointKind.NON_QUIESCENT_AUDIT:
        if snapshot.quiescent or snapshot.activity_snapshot is None:
            raise ValueError("invalid NON_QUIESCENT_AUDIT checkpoint")
        if snapshot.activity_snapshot.quiescent:
            raise ValueError("audit activity does not explain non-quiescence")
    elif kind is CheckpointKind.TERMINAL:
        # A later authoritative RUN_COMPLETED in the tail is also allowed.
        pass
    return kind


def _validate_checkpoint_after_tail(
    snapshot: RunSnapshot,
    kind: CheckpointKind,
    tail_terminal: bool,
) -> None:
    if kind is CheckpointKind.TERMINAL and (
        RunStatus(snapshot.run_status) not in _TERMINAL_RUN_STATUSES
        and not tail_terminal
    ):
        raise ValueError("terminal checkpoint has no terminal fact")


def _snapshot_reconciliation_reasons(
    snapshot: RunSnapshot,
) -> list[RecoveryReason]:
    reasons: list[RecoveryReason] = []
    activity = snapshot.activity_snapshot
    if (
        not snapshot.quiescent
        or snapshot.checkpoint_kind
        == CheckpointKind.NON_QUIESCENT_AUDIT.value
    ):
        _append_reason(reasons, RecoveryReason.NON_QUIESCENT_SNAPSHOT)
    if any(
        item.status == StepStatus.RUNNING.value for item in snapshot.step_states
    ):
        _append_reason(reasons, RecoveryReason.RUNNING_STEP_PRESENT)
    if (
        snapshot.budget_snapshot.reservation_count
        or any(value != 0 for value in snapshot.budget_snapshot.reserved.values())
    ):
        _append_reason(reasons, RecoveryReason.BUDGET_RESERVATION_PRESENT)
    if activity is None:
        return reasons
    if activity.activity_unknown:
        _append_reason(reasons, RecoveryReason.ACTIVITY_UNKNOWN)
    if activity.model_attempts_active:
        _append_reason(reasons, RecoveryReason.MODEL_ACTIVITY_PRESENT)
    if activity.tool_attempts_active:
        _append_reason(reasons, RecoveryReason.TOOL_ACTIVITY_PRESENT)
    if activity.retrievals_active:
        _append_reason(reasons, RecoveryReason.RETRIEVAL_ACTIVITY_PRESENT)
    if activity.detached_tool_workers or activity.detached_retrieval_workers:
        _append_reason(reasons, RecoveryReason.DETACHED_WORKER_PRESENT)
    if activity.event_publications_in_flight:
        _append_reason(reasons, RecoveryReason.EVENT_PUBLICATION_PRESENT)
    if (
        activity.claim_in_progress
        or activity.step_workers_active
        or activity.state_event_transitions_in_flight
        or activity.state_event_transition_observed
    ):
        _append_reason(reasons, RecoveryReason.RUNTIME_ACTIVITY_PRESENT)
    return reasons


def _resume_data_availability(
    snapshot: RunSnapshot,
    projection: RecoveryProjection,
) -> ResumeDataAvailability:
    pending_statuses = {
        StepStatus.PENDING.value,
        StepStatus.BLOCKED.value,
    }
    pending_ids = {
        step_id
        for step_id, state in projection.step_states.items()
        if state.status in pending_statuses
    }
    completed_dependencies_required = any(
        step.step_id in pending_ids
        and any(
            dependency in projection.step_states
            and projection.step_states[dependency].status
            == StepStatus.SUCCEEDED.value
            for dependency in step.dependency_step_ids
        )
        for step in snapshot.plan_snapshot.steps
    )
    # Snapshot TextSummary values are only length/digest evidence. The current
    # CheckpointBarrier has no result body store or rehydration owner.
    return ResumeDataAvailability(
        pending_steps_present=bool(pending_ids),
        completed_dependency_results_required=(
            completed_dependencies_required
        ),
        completed_dependency_results_available=(
            not completed_dependencies_required
        ),
        result_rehydration_supported=False,
        output_reconstruction_supported=False,
    )


def _append_tool_decision_reasons(
    reasons: list[RecoveryReason],
    decisions: tuple,
    evidence: tuple,
) -> None:
    for decision in decisions:
        if (
            decision.status
            is ToolRecoveryDecisionStatus.MANUAL_RECONCILIATION
        ):
            if "COMPENSATION_FAILED" in decision.reasons:
                _append_reason(
                    reasons, RecoveryReason.TOOL_COMPENSATION_FAILED
                )
            elif "OUTCOME_UNKNOWN" in decision.reasons:
                _append_reason(reasons, RecoveryReason.TOOL_OUTCOME_UNKNOWN)
            elif any("PAIRING" in item for item in decision.reasons):
                _append_reason(
                    reasons, RecoveryReason.TOOL_EVENT_PAIRING_INVALID
                )
            else:
                _append_reason(
                    reasons, RecoveryReason.TOOL_SIDE_EFFECT_EVIDENCE
                )
        elif (
            decision.status
            is ToolRecoveryDecisionStatus.INSUFFICIENT_EVIDENCE
        ):
            _append_reason(
                reasons, RecoveryReason.TOOL_EVIDENCE_INSUFFICIENT
            )
            if any(
                item.invocation_identity_digest
                == decision.invocation_identity_digest
                and item.side_effect_state == "COMMITTED"
                for item in evidence
            ):
                _append_reason(
                    reasons, RecoveryReason.TOOL_SIDE_EFFECT_EVIDENCE
                )


def _journal_read_failure(
    error: JournalError, **identity
) -> RecoveryAssessment:
    if error.error_code in {
        JournalErrorCode.SEQUENCE_CONFLICT,
        JournalErrorCode.OUT_OF_ORDER,
        JournalErrorCode.RUN_ALREADY_TERMINAL,
    }:
        return _failure(
            status=RecoveryStatus.JOURNAL_GAP_OR_CONFLICT,
            reason=RecoveryReason.JOURNAL_SEQUENCE_CONFLICT,
            **identity,
        )
    return _failure(
        status=RecoveryStatus.CORRUPTED,
        reason=RecoveryReason.JOURNAL_RECORD_CORRUPTED,
        **identity,
    )


def _failure(
    *,
    status: RecoveryStatus,
    reason: RecoveryReason,
    snapshot_id: str | None = None,
    run_id: str | None = None,
    snapshot_sequence: int | None = None,
    journal_last_sequence: int | None = None,
    last_applied_sequence: int | None = None,
    reduced_projection: RecoveryProjection | None = None,
    tool_evidence: tuple = (),
) -> RecoveryAssessment:
    return RecoveryAssessment(
        status=status,
        snapshot_id=snapshot_id,
        run_id=run_id,
        snapshot_sequence=snapshot_sequence,
        journal_last_sequence=journal_last_sequence,
        last_applied_sequence=last_applied_sequence,
        reasons=(reason,),
        blocking_step_ids=(),
        reduced_projection=reduced_projection,
        tool_evidence=tool_evidence,
        resume_prerequisites_satisfied=False,
    )


def _safe_identity(snapshot: RunSnapshot, name: str) -> str | None:
    value = getattr(snapshot, name, None)
    return value if isinstance(value, str) and value.strip() else None


def _safe_sequence(snapshot: RunSnapshot) -> int | None:
    value = getattr(snapshot, "last_journal_sequence", None)
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _append_reason(
    reasons: list[RecoveryReason], reason: RecoveryReason
) -> None:
    if reason not in reasons:
        reasons.append(reason)


class _UnsupportedCheckpoint(Exception):
    pass


class _JournalChanged(Exception):
    pass


__all__ = ["RecoveryValidator", "assess_recovery"]
