#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Immutable, payload-free contracts for snapshot recovery assessment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from core.runtime.snapshot_contract import BudgetSnapshot, StepStateSnapshot


class RecoveryStatus(str, Enum):
    FAILED = "FAILED"
    TERMINAL = "TERMINAL"
    RESUMABLE = "RESUMABLE"
    REQUIRES_RECONCILIATION = "REQUIRES_RECONCILIATION"
    INCOMPATIBLE_SCHEMA = "INCOMPATIBLE_SCHEMA"
    PLAN_MISMATCH = "PLAN_MISMATCH"
    CORRUPTED = "CORRUPTED"
    JOURNAL_GAP_OR_CONFLICT = "JOURNAL_GAP_OR_CONFLICT"
    UNSUPPORTED = "UNSUPPORTED"


# Never infer priority from Enum declaration order or values.
RECOVERY_STATUS_PRIORITY: tuple[RecoveryStatus, ...] = (
    RecoveryStatus.CORRUPTED,
    RecoveryStatus.FAILED,
    RecoveryStatus.INCOMPATIBLE_SCHEMA,
    RecoveryStatus.PLAN_MISMATCH,
    RecoveryStatus.JOURNAL_GAP_OR_CONFLICT,
    RecoveryStatus.UNSUPPORTED,
    RecoveryStatus.REQUIRES_RECONCILIATION,
    RecoveryStatus.TERMINAL,
    RecoveryStatus.RESUMABLE,
)


class RecoveryReason(str, Enum):
    SNAPSHOT_DIGEST_INVALID = "SNAPSHOT_DIGEST_INVALID"
    SNAPSHOT_SCHEMA_UNSUPPORTED = "SNAPSHOT_SCHEMA_UNSUPPORTED"
    SNAPSHOT_INTERNAL_INCONSISTENCY = "SNAPSHOT_INTERNAL_INCONSISTENCY"
    SNAPSHOT_NOT_FOUND = "SNAPSHOT_NOT_FOUND"
    PLAN_FINGERPRINT_MISMATCH = "PLAN_FINGERPRINT_MISMATCH"
    SNAPSHOT_SEQUENCE_AHEAD_OF_JOURNAL = (
        "SNAPSHOT_SEQUENCE_AHEAD_OF_JOURNAL"
    )
    JOURNAL_RECORD_CORRUPTED = "JOURNAL_RECORD_CORRUPTED"
    JOURNAL_SEQUENCE_CONFLICT = "JOURNAL_SEQUENCE_CONFLICT"
    JOURNAL_TERMINAL_CONFLICT = "JOURNAL_TERMINAL_CONFLICT"
    JOURNAL_SCHEMA_UNSUPPORTED = "JOURNAL_SCHEMA_UNSUPPORTED"
    EVENT_SCHEMA_UNSUPPORTED = "EVENT_SCHEMA_UNSUPPORTED"
    UNSUPPORTED_EVENT_TYPE = "UNSUPPORTED_EVENT_TYPE"
    UNSUPPORTED_CHECKPOINT_KIND = "UNSUPPORTED_CHECKPOINT_KIND"
    NON_QUIESCENT_SNAPSHOT = "NON_QUIESCENT_SNAPSHOT"
    ACTIVITY_UNKNOWN = "ACTIVITY_UNKNOWN"
    RUNTIME_ACTIVITY_PRESENT = "RUNTIME_ACTIVITY_PRESENT"
    RUNNING_STEP_PRESENT = "RUNNING_STEP_PRESENT"
    BUDGET_RESERVATION_PRESENT = "BUDGET_RESERVATION_PRESENT"
    DETACHED_WORKER_PRESENT = "DETACHED_WORKER_PRESENT"
    MODEL_ACTIVITY_PRESENT = "MODEL_ACTIVITY_PRESENT"
    TOOL_ACTIVITY_PRESENT = "TOOL_ACTIVITY_PRESENT"
    RETRIEVAL_ACTIVITY_PRESENT = "RETRIEVAL_ACTIVITY_PRESENT"
    EVENT_PUBLICATION_PRESENT = "EVENT_PUBLICATION_PRESENT"
    TOOL_SIDE_EFFECT_EVIDENCE = "TOOL_SIDE_EFFECT_EVIDENCE"
    TOOL_OUTCOME_UNKNOWN = "TOOL_OUTCOME_UNKNOWN"
    TOOL_COMPENSATION_FAILED = "TOOL_COMPENSATION_FAILED"
    MODEL_OUTCOME_UNKNOWN = "MODEL_OUTCOME_UNKNOWN"
    RETRIEVAL_OUTCOME_UNKNOWN = "RETRIEVAL_OUTCOME_UNKNOWN"
    RUN_TERMINAL = "RUN_TERMINAL"
    SAFE_RESUME_PREREQUISITES_MET = "SAFE_RESUME_PREREQUISITES_MET"
    STEP_RESULT_REHYDRATION_UNSUPPORTED = (
        "STEP_RESULT_REHYDRATION_UNSUPPORTED"
    )
    DEPENDENCY_OUTPUT_UNAVAILABLE = "DEPENDENCY_OUTPUT_UNAVAILABLE"
    FINAL_OUTPUT_RECONSTRUCTION_UNSUPPORTED = (
        "FINAL_OUTPUT_RECONSTRUCTION_UNSUPPORTED"
    )
    TOOL_EVENT_PAIRING_INVALID = "TOOL_EVENT_PAIRING_INVALID"
    TOOL_EVIDENCE_INSUFFICIENT = "TOOL_EVIDENCE_INSUFFICIENT"
    SNAPSHOT_READ_FAILED = "SNAPSHOT_READ_FAILED"
    JOURNAL_TAIL_READ_NOT_EXECUTED = "JOURNAL_TAIL_READ_NOT_EXECUTED"
    RECOVERY_VALIDATION_FAILED = "RECOVERY_VALIDATION_FAILED"
    RECOVERY_VALIDATION_CANCELLED = "RECOVERY_VALIDATION_CANCELLED"


# These are the only human-readable recovery explanations. They deliberately
# contain no payload, path, SQL, exception text, or runtime identifiers.
RECOVERY_REASON_TEXT: Mapping[RecoveryReason, str] = MappingProxyType(
    {
        RecoveryReason.SNAPSHOT_DIGEST_INVALID: "Snapshot integrity verification failed.",
        RecoveryReason.SNAPSHOT_SCHEMA_UNSUPPORTED: "Snapshot schema is not supported.",
        RecoveryReason.SNAPSHOT_INTERNAL_INCONSISTENCY: "Snapshot fields are internally inconsistent.",
        RecoveryReason.SNAPSHOT_NOT_FOUND: "Snapshot was not found.",
        RecoveryReason.PLAN_FINGERPRINT_MISMATCH: "Current plan does not match the snapshot plan.",
        RecoveryReason.SNAPSHOT_SEQUENCE_AHEAD_OF_JOURNAL: "Snapshot watermark is ahead of the journal.",
        RecoveryReason.JOURNAL_RECORD_CORRUPTED: "A journal record failed integrity validation.",
        RecoveryReason.JOURNAL_SEQUENCE_CONFLICT: "Journal sequence ordering or ownership is invalid.",
        RecoveryReason.JOURNAL_TERMINAL_CONFLICT: "Journal terminal event ordering is invalid.",
        RecoveryReason.JOURNAL_SCHEMA_UNSUPPORTED: "Journal schema is not supported.",
        RecoveryReason.EVENT_SCHEMA_UNSUPPORTED: "Runtime event schema is not supported.",
        RecoveryReason.UNSUPPORTED_EVENT_TYPE: "Journal event type is not supported for recovery.",
        RecoveryReason.UNSUPPORTED_CHECKPOINT_KIND: "Checkpoint kind is not supported for recovery.",
        RecoveryReason.NON_QUIESCENT_SNAPSHOT: "Snapshot was captured while runtime work was active.",
        RecoveryReason.ACTIVITY_UNKNOWN: "Snapshot contains unknown runtime activity.",
        RecoveryReason.RUNTIME_ACTIVITY_PRESENT: "Snapshot contains active runtime work.",
        RecoveryReason.RUNNING_STEP_PRESENT: "At least one step is still in flight.",
        RecoveryReason.BUDGET_RESERVATION_PRESENT: "Budget reservations are still active.",
        RecoveryReason.DETACHED_WORKER_PRESENT: "Detached worker activity requires reconciliation.",
        RecoveryReason.MODEL_ACTIVITY_PRESENT: "Model activity was active at capture time.",
        RecoveryReason.TOOL_ACTIVITY_PRESENT: "Tool activity was active at capture time.",
        RecoveryReason.RETRIEVAL_ACTIVITY_PRESENT: "Retrieval activity was active at capture time.",
        RecoveryReason.EVENT_PUBLICATION_PRESENT: "Event publication was active at capture time.",
        RecoveryReason.TOOL_SIDE_EFFECT_EVIDENCE: "Tool side-effect evidence requires reconciliation.",
        RecoveryReason.TOOL_OUTCOME_UNKNOWN: "A tool outcome is not authoritative.",
        RecoveryReason.TOOL_COMPENSATION_FAILED: "Tool compensation did not complete safely.",
        RecoveryReason.MODEL_OUTCOME_UNKNOWN: "A model attempt has no matching completion fact.",
        RecoveryReason.RETRIEVAL_OUTCOME_UNKNOWN: "A retrieval has no matching completion fact.",
        RecoveryReason.RUN_TERMINAL: "The reduced run state is terminal.",
        RecoveryReason.SAFE_RESUME_PREREQUISITES_MET: "Future resume safety prerequisites are satisfied.",
        RecoveryReason.STEP_RESULT_REHYDRATION_UNSUPPORTED: "Completed step results cannot be rehydrated.",
        RecoveryReason.DEPENDENCY_OUTPUT_UNAVAILABLE: "A pending step dependency output is unavailable.",
        RecoveryReason.FINAL_OUTPUT_RECONSTRUCTION_UNSUPPORTED: "The final output cannot be reconstructed.",
        RecoveryReason.TOOL_EVENT_PAIRING_INVALID: "Tool attempt event pairing is invalid.",
        RecoveryReason.TOOL_EVIDENCE_INSUFFICIENT: "Historical tool evidence is insufficient.",
        RecoveryReason.SNAPSHOT_READ_FAILED: "Snapshot read did not complete.",
        RecoveryReason.JOURNAL_TAIL_READ_NOT_EXECUTED: "Journal tail read did not execute.",
        RecoveryReason.RECOVERY_VALIDATION_FAILED: "Recovery validation did not complete.",
        RecoveryReason.RECOVERY_VALIDATION_CANCELLED: "Recovery validation was cancelled.",
    }
)


class ToolRecoveryDecisionStatus(str, Enum):
    NO_ACTION_REQUIRED = "NO_ACTION_REQUIRED"
    SAFE_RETRY_CANDIDATE = "SAFE_RETRY_CANDIDATE"
    DO_NOT_RETRY = "DO_NOT_RETRY"
    MANUAL_RECONCILIATION = "MANUAL_RECONCILIATION"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


def select_recovery_status(
    statuses: tuple[RecoveryStatus, ...] | list[RecoveryStatus] | set[RecoveryStatus],
) -> RecoveryStatus:
    """Select the highest-priority status using the explicit contract."""
    candidates = set(statuses)
    for status in RECOVERY_STATUS_PRIORITY:
        if status in candidates:
            return status
    raise ValueError("at least one recovery status is required")


@dataclass(frozen=True, slots=True)
class ToolRecoveryEvidence:
    tool_name: str
    invocation_identity_digest: str | None
    attempt_identity_digest: str | None
    side_effect_kind: str | None
    side_effect_state: str | None
    retry_disposition: str | None
    execution_detached: bool
    worker_terminated: bool
    safe_error_code: str | None
    sequence: int
    event_kind: str = "UNKNOWN"
    step_id: str | None = None
    attempt_sequence: int | None = None
    tool_evidence_schema_version: int | None = None
    idempotency_kind: str | None = None
    idempotency_key_digest: str | None = None
    replay_supported: bool | None = None
    compensation_state: str | None = None
    outcome_classification: str | None = None
    provider_started: bool | None = None
    succeeded: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("tool_name must be a non-empty string")
        for name in (
            "invocation_identity_digest",
            "attempt_identity_digest",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if type(self.execution_detached) is not bool:
            raise ValueError("execution_detached must be bool")
        if type(self.worker_terminated) is not bool:
            raise ValueError("worker_terminated must be bool")
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence <= 0
        ):
            raise ValueError("sequence must be a positive integer")
        if self.idempotency_key_digest is not None and (
            len(self.idempotency_key_digest) != 64
            or any(
                char not in "0123456789abcdef"
                for char in self.idempotency_key_digest
            )
        ):
            raise ValueError(
                "idempotency_key_digest must be a lowercase SHA-256 digest"
            )
        if self.event_kind not in {"STARTED", "COMPLETED", "UNKNOWN"}:
            raise ValueError("event_kind is invalid")
        if self.step_id is not None and (
            not isinstance(self.step_id, str) or not self.step_id.strip()
        ):
            raise ValueError("step_id must be a non-empty string")
        if self.attempt_sequence is not None and (
            isinstance(self.attempt_sequence, bool)
            or not isinstance(self.attempt_sequence, int)
            or self.attempt_sequence < 0
        ):
            raise ValueError("attempt_sequence must be non-negative")
        if self.tool_evidence_schema_version is not None and (
            isinstance(self.tool_evidence_schema_version, bool)
            or not isinstance(self.tool_evidence_schema_version, int)
            or self.tool_evidence_schema_version <= 0
        ):
            raise ValueError("tool_evidence_schema_version must be positive")
        for name in ("replay_supported", "provider_started", "succeeded"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise ValueError(f"{name} must be bool or None")


@dataclass(frozen=True, slots=True)
class ToolRecoveryDecision:
    invocation_identity_digest: str | None
    tool_name: str
    status: ToolRecoveryDecisionStatus
    reasons: tuple[str, ...]
    attempt_sequences: tuple[int, ...]
    side_effect_kind: str | None
    idempotency_kind: str | None
    side_effect_state: str | None
    compensation_state: str | None
    replay_supported: bool | None
    idempotency_key_available: bool
    execution_detached: bool
    worker_terminated: bool
    retry_candidate: bool
    automatic_action_allowed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("tool_name must be a non-empty string")
        if not isinstance(self.status, ToolRecoveryDecisionStatus):
            raise TypeError("status must be ToolRecoveryDecisionStatus")
        if not isinstance(self.reasons, tuple) or not all(
            isinstance(item, str) and item for item in self.reasons
        ):
            raise TypeError("reasons must be non-empty strings")
        if tuple(sorted(set(self.attempt_sequences))) != self.attempt_sequences:
            raise ValueError("attempt_sequences must be unique and sorted")
        for name in (
            "idempotency_key_available",
            "execution_detached",
            "worker_terminated",
            "retry_candidate",
            "automatic_action_allowed",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")
        if self.automatic_action_allowed:
            raise ValueError("day-22 decisions never permit automatic actions")
        if self.retry_candidate != (
            self.status is ToolRecoveryDecisionStatus.SAFE_RETRY_CANDIDATE
        ):
            raise ValueError("retry_candidate must match decision status")


@dataclass(frozen=True, slots=True)
class ResumeDataAvailability:
    pending_steps_present: bool = False
    completed_dependency_results_required: bool = False
    completed_dependency_results_available: bool = False
    result_rehydration_supported: bool = False
    output_reconstruction_supported: bool = False

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")


@dataclass(frozen=True, slots=True)
class RecoveryProjection:
    run_status: str
    stop_reason: str | None
    cancellation_reason: str | None
    step_states: Mapping[str, StepStateSnapshot]
    budget_snapshot: BudgetSnapshot
    last_applied_sequence: int
    terminal_event_seen: bool
    output_available: bool
    budget_exhausted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.step_states, Mapping):
            raise ValueError("step_states must be a mapping")
        frozen: dict[str, StepStateSnapshot] = {}
        for step_id, state in self.step_states.items():
            if not isinstance(step_id, str) or not isinstance(
                state, StepStateSnapshot
            ):
                raise ValueError("step_states must contain snapshot states")
            frozen[step_id] = state
        object.__setattr__(
            self, "step_states", MappingProxyType(dict(sorted(frozen.items())))
        )
        if not isinstance(self.budget_snapshot, BudgetSnapshot):
            raise ValueError("budget_snapshot must be BudgetSnapshot")
        if (
            isinstance(self.last_applied_sequence, bool)
            or not isinstance(self.last_applied_sequence, int)
            or self.last_applied_sequence < 0
        ):
            raise ValueError("last_applied_sequence must be non-negative")
        for name in (
            "terminal_event_seen",
            "output_available",
            "budget_exhausted",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be bool")


@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    status: RecoveryStatus
    snapshot_id: str | None
    run_id: str | None
    snapshot_sequence: int | None
    journal_last_sequence: int | None
    last_applied_sequence: int | None
    reasons: tuple[RecoveryReason, ...]
    blocking_step_ids: tuple[str, ...]
    reduced_projection: RecoveryProjection | None
    tool_evidence: tuple[ToolRecoveryEvidence, ...]
    resume_prerequisites_satisfied: bool
    automatic_resume_supported: bool = False
    model_replay_allowed: bool = False
    tool_replay_allowed: bool = False
    retrieval_replay_allowed: bool = False
    tool_decisions: tuple[ToolRecoveryDecision, ...] = ()
    resume_data_availability: ResumeDataAvailability = field(
        default_factory=ResumeDataAvailability
    )
    output_reconstruction_supported: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.status, RecoveryStatus):
            raise TypeError("status must be RecoveryStatus")
        if not isinstance(self.reasons, tuple) or not all(
            isinstance(reason, RecoveryReason) for reason in self.reasons
        ):
            raise TypeError("reasons must contain RecoveryReason values")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("reasons must be unique")
        if tuple(sorted(set(self.blocking_step_ids))) != self.blocking_step_ids:
            raise ValueError("blocking_step_ids must be unique and sorted")
        if not all(
            isinstance(item, ToolRecoveryEvidence) for item in self.tool_evidence
        ):
            raise TypeError("tool_evidence must contain ToolRecoveryEvidence")
        if not all(
            isinstance(item, ToolRecoveryDecision) for item in self.tool_decisions
        ):
            raise TypeError("tool_decisions must contain ToolRecoveryDecision")
        if not isinstance(
            self.resume_data_availability, ResumeDataAvailability
        ):
            raise TypeError(
                "resume_data_availability must be ResumeDataAvailability"
            )
        for name in (
            "resume_prerequisites_satisfied",
            "automatic_resume_supported",
            "model_replay_allowed",
            "tool_replay_allowed",
            "retrieval_replay_allowed",
            "output_reconstruction_supported",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")
        if (
            self.automatic_resume_supported
            or self.model_replay_allowed
            or self.tool_replay_allowed
            or self.retrieval_replay_allowed
            or self.output_reconstruction_supported
        ):
            raise ValueError("day-22 recovery assessment must never enable replay")

    def reason_texts(self) -> tuple[str, ...]:
        return tuple(RECOVERY_REASON_TEXT[reason] for reason in self.reasons)


__all__ = [
    "RECOVERY_REASON_TEXT",
    "RECOVERY_STATUS_PRIORITY",
    "RecoveryAssessment",
    "RecoveryProjection",
    "RecoveryReason",
    "RecoveryStatus",
    "ResumeDataAvailability",
    "ToolRecoveryDecision",
    "ToolRecoveryDecisionStatus",
    "ToolRecoveryEvidence",
    "select_recovery_status",
]
