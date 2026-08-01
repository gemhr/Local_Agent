#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Safe, immutable contracts used by runtime checkpoint coordination."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class SchedulerClaimGateState(str, Enum):
    OPEN = "OPEN"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    RESUMING = "RESUMING"
    CLOSED = "CLOSED"


class CheckpointBarrierState(str, Enum):
    IDLE = "IDLE"
    PAUSING_CLAIMS = "PAUSING_CLAIMS"
    WAITING_FOR_QUIESCENCE = "WAITING_FOR_QUIESCENCE"
    CAPTURING = "CAPTURING"
    SAVING = "SAVING"
    RESUMING_CLAIMS = "RESUMING_CLAIMS"


class CheckpointMode(str, Enum):
    REQUIRE_QUIESCENT = "REQUIRE_QUIESCENT"
    ALLOW_NON_QUIESCENT_AUDIT = "ALLOW_NON_QUIESCENT_AUDIT"


class CheckpointKind(str, Enum):
    PRE_RUN = "PRE_RUN"
    STEP_BOUNDARY = "STEP_BOUNDARY"
    TERMINAL = "TERMINAL"
    NON_QUIESCENT_AUDIT = "NON_QUIESCENT_AUDIT"
    # Fixed legacy kind used only by the unpublished day-22 foundation fixtures.
    OBSERVATION = "OBSERVATION"


class CheckpointStatus(str, Enum):
    SAVED = "SAVED"
    SAVED_NON_QUIESCENT_AUDIT = "SAVED_NON_QUIESCENT_AUDIT"
    NOT_QUIESCENT = "NOT_QUIESCENT"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    STORE_FAILED = "STORE_FAILED"
    CORRUPTED = "CORRUPTED"
    ALREADY_IN_PROGRESS = "ALREADY_IN_PROGRESS"


@dataclass(frozen=True, slots=True)
class SnapshotPublicationEvidence:
    """Safe identity/integrity facts; snapshot payload is never retained."""

    run_id_digest: str
    snapshot_version: int | None
    schema_version: int
    snapshot_digest: str
    persisted: bool
    partially_persisted: bool
    retry_allowed: bool

    def __post_init__(self) -> None:
        for name in ("run_id_digest", "snapshot_digest"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if self.snapshot_version is not None:
            _require_count(self.snapshot_version, "snapshot_version")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version <= 0
        ):
            raise ValueError("schema_version must be a positive integer")
        for name in ("persisted", "partially_persisted", "retry_allowed"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")
        if self.partially_persisted and not self.persisted:
            raise ValueError("partial persistence requires persisted=true")
        if self.partially_persisted and self.retry_allowed:
            raise ValueError("partial persistence can never be retryable")


@dataclass(frozen=True, slots=True)
class SchedulerClaimGateSnapshot:
    state: SchedulerClaimGateState
    claim_in_progress: int
    captured_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.state, SchedulerClaimGateState):
            raise TypeError("state must be SchedulerClaimGateState")
        _require_count(self.claim_in_progress, "claim_in_progress")
        _require_utc(self.captured_at, "captured_at")


@dataclass(frozen=True, slots=True)
class RuntimeActivitySnapshot:
    """Content-free per-run activity view used for quiescence decisions."""

    claim_in_progress: int
    running_step_count: int
    budget_reservation_count: int
    model_attempts_active: int
    tool_attempts_active: int
    retrievals_active: int
    detached_tool_workers: int
    detached_retrieval_workers: int
    event_publications_in_flight: int
    step_workers_active: int
    activity_unknown: bool
    captured_at: datetime
    state_event_transitions_in_flight: int = 0
    state_event_transition_epoch: int = 0
    state_event_transition_observed: bool = False

    def __post_init__(self) -> None:
        for name in (
            "claim_in_progress",
            "running_step_count",
            "budget_reservation_count",
            "model_attempts_active",
            "tool_attempts_active",
            "retrievals_active",
            "detached_tool_workers",
            "detached_retrieval_workers",
            "event_publications_in_flight",
            "step_workers_active",
            "state_event_transitions_in_flight",
            "state_event_transition_epoch",
        ):
            _require_count(getattr(self, name), name)
        if type(self.activity_unknown) is not bool:
            raise TypeError("activity_unknown must be bool")
        if type(self.state_event_transition_observed) is not bool:
            raise TypeError("state_event_transition_observed must be bool")
        _require_utc(self.captured_at, "captured_at")

    @property
    def quiescent(self) -> bool:
        return (
            not self.activity_unknown
            and not self.state_event_transition_observed
            and all(
            getattr(self, name) == 0
            for name in (
                "claim_in_progress",
                "running_step_count",
                "budget_reservation_count",
                "model_attempts_active",
                "tool_attempts_active",
                "retrievals_active",
                "detached_tool_workers",
                "detached_retrieval_workers",
                "event_publications_in_flight",
                "step_workers_active",
                "state_event_transitions_in_flight",
            )
            )
        )


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    status: CheckpointStatus
    snapshot_id: str | None
    quiescent: bool
    checkpoint_kind: CheckpointKind
    journal_sequence: int | None
    activity_summary: RuntimeActivitySnapshot | None
    safe_error_code: str | None
    snapshot_publication_evidence: SnapshotPublicationEvidence | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, CheckpointStatus):
            raise TypeError("status must be CheckpointStatus")
        if not isinstance(self.checkpoint_kind, CheckpointKind):
            raise TypeError("checkpoint_kind must be CheckpointKind")
        if type(self.quiescent) is not bool:
            raise TypeError("quiescent must be bool")
        if self.journal_sequence is not None:
            _require_count(self.journal_sequence, "journal_sequence")
        if self.activity_summary is not None and not isinstance(
            self.activity_summary, RuntimeActivitySnapshot
        ):
            raise TypeError("activity_summary must be RuntimeActivitySnapshot")
        if self.snapshot_publication_evidence is not None and not isinstance(
            self.snapshot_publication_evidence, SnapshotPublicationEvidence
        ):
            raise TypeError(
                "snapshot_publication_evidence must be SnapshotPublicationEvidence"
            )

    @property
    def persisted(self) -> bool:
        evidence = self.snapshot_publication_evidence
        return evidence.persisted if evidence is not None else False

    @property
    def partially_persisted(self) -> bool:
        evidence = self.snapshot_publication_evidence
        return evidence.partially_persisted if evidence is not None else False

    @property
    def retry_allowed(self) -> bool:
        evidence = self.snapshot_publication_evidence
        return evidence.retry_allowed if evidence is not None else False


def _require_count(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def _require_utc(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{field_name} must be a timezone-aware UTC datetime")


__all__ = [
    "CheckpointBarrierState",
    "CheckpointKind",
    "CheckpointMode",
    "CheckpointResult",
    "CheckpointStatus",
    "RuntimeActivitySnapshot",
    "SnapshotPublicationEvidence",
    "SchedulerClaimGateSnapshot",
    "SchedulerClaimGateState",
]
