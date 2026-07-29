#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Immutable, payload-free contracts for snapshot recovery assessment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from core.runtime.snapshot_contract import BudgetSnapshot, StepStateSnapshot


class RecoveryStatus(str, Enum):
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
    }
)


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
        for name in (
            "resume_prerequisites_satisfied",
            "automatic_resume_supported",
            "model_replay_allowed",
            "tool_replay_allowed",
            "retrieval_replay_allowed",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")
        if (
            self.automatic_resume_supported
            or self.model_replay_allowed
            or self.tool_replay_allowed
            or self.retrieval_replay_allowed
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
    "ToolRecoveryEvidence",
    "select_recovery_status",
]
