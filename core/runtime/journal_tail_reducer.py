#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Strict journal-tail validation and a side-effect-free limited reducer."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re

from core.runtime.event_journal import (
    SUPPORTED_JOURNAL_SCHEMA_VERSIONS,
    JournalError,
    JournalRecord,
)
from core.runtime.events import RUNTIME_EVENT_SCHEMA_VERSION, RuntimeEventType
from core.runtime.recovery_contract import (
    RecoveryProjection,
    RecoveryReason,
    RecoveryStatus,
    ToolRecoveryEvidence,
)
from core.runtime.snapshot_contract import RunSnapshot, StepStateSnapshot
from core.runtime.snapshot_serialization import text_digest
from core.runtime.state import RunStatus, StepStatus, StopReason


SUPPORTED_EVENT_SCHEMA_VERSIONS = frozenset({1, RUNTIME_EVENT_SCHEMA_VERSION})
REDUCED_EVENT_TYPES = frozenset(
    {
        RuntimeEventType.RUN_STARTED,
        RuntimeEventType.STEP_STARTED,
        RuntimeEventType.STEP_COMPLETED,
        RuntimeEventType.CANCELLATION,
        RuntimeEventType.TIMEOUT,
        RuntimeEventType.BUDGET_EXHAUSTED,
        RuntimeEventType.RUN_COMPLETED,
    }
)
IGNORED_EVENT_TYPES = frozenset(
    {
        RuntimeEventType.OUTPUT_DELTA,
        RuntimeEventType.MODEL_STARTED,
        RuntimeEventType.MODEL_COMPLETED,
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
        RuntimeEventType.RETRIEVAL_STARTED,
        RuntimeEventType.RETRIEVAL_STAGE_COMPLETED,
        RuntimeEventType.RETRIEVAL_COMPLETED,
        RuntimeEventType.ERROR,
    }
)
SUPPORTED_RECOVERY_EVENT_TYPES = REDUCED_EVENT_TYPES | IGNORED_EVENT_TYPES

_SAFE_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
)
_TERMINAL_STEP_STATUSES = frozenset(
    {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.CANCELLED}
)
_CANCELLATION_REASONS = frozenset(
    {
        StopReason.USER_CANCELLED,
        StopReason.CLIENT_DISCONNECTED,
        StopReason.SYSTEM_SHUTDOWN,
    }
)


class JournalTailValidationError(RuntimeError):
    """Fixed-code validation failure which never embeds journal content."""

    def __init__(
        self, status: RecoveryStatus, reason: RecoveryReason
    ) -> None:
        self.status = status
        self.reason = reason
        super().__init__(reason.value)


class JournalTailReductionError(RuntimeError):
    """Fixed-code reduction failure which never embeds journal content."""

    def __init__(
        self,
        status: RecoveryStatus,
        reason: RecoveryReason,
    ) -> None:
        self.status = status
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class JournalTailValidation:
    records: tuple[JournalRecord, ...]
    last_sequence: int
    terminal_event_seen: bool


@dataclass(frozen=True, slots=True)
class JournalTailReduction:
    projection: RecoveryProjection
    tool_evidence: tuple[ToolRecoveryEvidence, ...]
    reconciliation_reasons: tuple[RecoveryReason, ...]


class JournalTailValidator:
    """Validate ordering, ownership, schemas and integrity without replay."""

    @staticmethod
    def validate(
        *,
        run_id: str,
        snapshot_sequence: int,
        records: tuple[JournalRecord, ...],
    ) -> JournalTailValidation:
        previous = snapshot_sequence
        terminal_seen = False
        validated: list[JournalRecord] = []
        for record in records:
            if not isinstance(record, JournalRecord):
                raise JournalTailValidationError(
                    RecoveryStatus.CORRUPTED,
                    RecoveryReason.JOURNAL_RECORD_CORRUPTED,
                )
            if record.run_id != run_id:
                raise JournalTailValidationError(
                    RecoveryStatus.JOURNAL_GAP_OR_CONFLICT,
                    RecoveryReason.JOURNAL_SEQUENCE_CONFLICT,
                )
            if record.sequence <= snapshot_sequence or record.sequence <= previous:
                raise JournalTailValidationError(
                    RecoveryStatus.JOURNAL_GAP_OR_CONFLICT,
                    RecoveryReason.JOURNAL_SEQUENCE_CONFLICT,
                )
            if (
                record.journal_schema_version
                not in SUPPORTED_JOURNAL_SCHEMA_VERSIONS
            ):
                raise JournalTailValidationError(
                    RecoveryStatus.UNSUPPORTED,
                    RecoveryReason.JOURNAL_SCHEMA_UNSUPPORTED,
                )
            if record.event_schema_version not in SUPPORTED_EVENT_SCHEMA_VERSIONS:
                raise JournalTailValidationError(
                    RecoveryStatus.UNSUPPORTED,
                    RecoveryReason.EVENT_SCHEMA_UNSUPPORTED,
                )
            try:
                record.verify()
            except (JournalError, ValueError, TypeError):
                raise JournalTailValidationError(
                    RecoveryStatus.CORRUPTED,
                    RecoveryReason.JOURNAL_RECORD_CORRUPTED,
                ) from None
            if not isinstance(record.event_type, RuntimeEventType):
                raise JournalTailValidationError(
                    RecoveryStatus.UNSUPPORTED,
                    RecoveryReason.UNSUPPORTED_EVENT_TYPE,
                )
            if record.event_type not in SUPPORTED_RECOVERY_EVENT_TYPES:
                raise JournalTailValidationError(
                    RecoveryStatus.UNSUPPORTED,
                    RecoveryReason.UNSUPPORTED_EVENT_TYPE,
                )
            if terminal_seen:
                raise JournalTailValidationError(
                    RecoveryStatus.JOURNAL_GAP_OR_CONFLICT,
                    RecoveryReason.JOURNAL_TERMINAL_CONFLICT,
                )
            terminal_seen = record.event_type is RuntimeEventType.RUN_COMPLETED
            previous = record.sequence
            validated.append(record)
        return JournalTailValidation(
            records=tuple(validated),
            last_sequence=previous,
            terminal_event_seen=terminal_seen,
        )


class LimitedJournalTailReducer:
    """Reduce safe metadata only; it has no adapter or mutable state dependency."""

    @classmethod
    def reduce(
        cls,
        snapshot: RunSnapshot,
        records: tuple[JournalRecord, ...],
    ) -> JournalTailReduction:
        if not isinstance(snapshot, RunSnapshot):
            raise TypeError("snapshot must be RunSnapshot")
        steps = {
            item.step_id: item for item in snapshot.state_snapshot.step_states
        }
        run_status = snapshot.run_status
        stop_reason = snapshot.stop_reason
        cancellation_reason = snapshot.cancellation_reason
        last_applied = snapshot.last_journal_sequence
        terminal_seen = RunStatus(run_status) in _TERMINAL_RUN_STATUSES
        output_available = snapshot.state_snapshot.final_output.present
        budget_exhausted = stop_reason == StopReason.BUDGET_EXHAUSTED.value
        evidence: list[ToolRecoveryEvidence] = []
        reasons: list[RecoveryReason] = []

        model_started: dict[tuple[object, ...], int] = {}
        retrieval_started: dict[str, int] = {}
        tool_started: dict[tuple[object, ...], int] = {}

        for record in records:
            event_type = record.event_type
            payload = record.safe_payload
            if event_type is RuntimeEventType.RUN_STARTED:
                status = _run_status(payload.get("status"))
                if status is not RunStatus.RUNNING:
                    _corrupted()
                run_status = status.value
                stop_reason = None
                cancellation_reason = None
            elif event_type is RuntimeEventType.STEP_STARTED:
                step = _known_step(steps, record.step_id)
                if payload.get("status") != StepStatus.RUNNING.value:
                    _corrupted()
                steps[step.step_id] = replace(
                    step,
                    status=StepStatus.RUNNING.value,
                    in_flight=True,
                    execution_started=True,
                    # The event has no authoritative cumulative attempt count.
                    # Recovery never increments a prior count or maps null to 0/1.
                    attempt_count=None,
                    started_at=record.emitted_at,
                    completed_at=None,
                    duration_ms=None,
                    safe_error_code=None,
                )
            elif event_type is RuntimeEventType.STEP_COMPLETED:
                step = _known_step(steps, record.step_id)
                status = _step_status(payload.get("status"))
                if status not in _TERMINAL_STEP_STATUSES:
                    _corrupted()
                if not step.execution_started:
                    _corrupted()
                steps[step.step_id] = replace(
                    step,
                    status=status.value,
                    in_flight=False,
                    completed_at=record.emitted_at,
                    duration_ms=_nonnegative_int(
                        payload.get("duration_ms", 0)
                    ),
                    safe_error_code=_safe_error_code(
                        payload.get("safe_error_code")
                    ),
                )
            elif event_type is RuntimeEventType.CANCELLATION:
                reason = _stop_reason(payload.get("reason"))
                if reason not in _CANCELLATION_REASONS:
                    _corrupted()
                cancellation_reason = reason.value
            elif event_type is RuntimeEventType.TIMEOUT:
                stop_reason = StopReason.DEADLINE_EXCEEDED.value
            elif event_type is RuntimeEventType.BUDGET_EXHAUSTED:
                # The event is a fact, not an authoritative cumulative ledger.
                budget_exhausted = True
                stop_reason = StopReason.BUDGET_EXHAUSTED.value
            elif event_type is RuntimeEventType.RUN_COMPLETED:
                status = _run_status(payload.get("status"))
                reason = _stop_reason(payload.get("stop_reason"))
                _validate_terminal(status, reason)
                run_status = status.value
                stop_reason = reason.value
                cancellation_reason = (
                    reason.value if reason in _CANCELLATION_REASONS else None
                )
                terminal_seen = True
            elif event_type is RuntimeEventType.OUTPUT_DELTA:
                # Only the presence of journal metadata is projected.
                output_available = True
            elif event_type is RuntimeEventType.MODEL_STARTED:
                key = _model_key(payload)
                model_started[key] = model_started.get(key, 0) + 1
            elif event_type is RuntimeEventType.MODEL_COMPLETED:
                if not _consume(model_started, _model_key(payload)):
                    _append_reason(reasons, RecoveryReason.MODEL_OUTCOME_UNKNOWN)
            elif event_type is RuntimeEventType.RETRIEVAL_STARTED:
                retrieval_id = _required_text(payload.get("retrieval_id"))
                retrieval_started[retrieval_id] = (
                    retrieval_started.get(retrieval_id, 0) + 1
                )
            elif event_type is RuntimeEventType.RETRIEVAL_COMPLETED:
                retrieval_id = _required_text(payload.get("retrieval_id"))
                if not _consume(retrieval_started, retrieval_id):
                    _append_reason(
                        reasons, RecoveryReason.RETRIEVAL_OUTCOME_UNKNOWN
                    )
                if payload.get("execution_detached") is True and (
                    payload.get("worker_terminated") is not True
                ):
                    _append_reason(
                        reasons, RecoveryReason.DETACHED_WORKER_PRESENT
                    )
            elif event_type is RuntimeEventType.RETRIEVAL_STAGE_COMPLETED:
                if payload.get("execution_detached") is True and (
                    payload.get("worker_terminated") is not True
                ):
                    _append_reason(
                        reasons, RecoveryReason.DETACHED_WORKER_PRESENT
                    )
            elif event_type is RuntimeEventType.TOOL_STARTED:
                item = _tool_evidence(record, started=True)
                evidence.append(item)
                key = _tool_key(item)
                tool_started[key] = tool_started.get(key, 0) + 1
            elif event_type is RuntimeEventType.TOOL_COMPLETED:
                item = _tool_evidence(record, started=False)
                evidence.append(item)
                if not _consume(tool_started, _tool_key(item)):
                    _append_reason(reasons, RecoveryReason.TOOL_OUTCOME_UNKNOWN)
                _collect_tool_risk(item, reasons)
            elif event_type is RuntimeEventType.ERROR:
                # Safe diagnostic fact only. RUN_COMPLETED is authoritative.
                pass
            else:  # pragma: no cover - validator closes this set first
                raise JournalTailReductionError(
                    RecoveryStatus.UNSUPPORTED,
                    RecoveryReason.UNSUPPORTED_EVENT_TYPE,
                )
            last_applied = record.sequence

        if any(model_started.values()):
            _append_reason(reasons, RecoveryReason.MODEL_OUTCOME_UNKNOWN)
        if any(retrieval_started.values()):
            _append_reason(reasons, RecoveryReason.RETRIEVAL_OUTCOME_UNKNOWN)
        if any(tool_started.values()):
            _append_reason(reasons, RecoveryReason.TOOL_OUTCOME_UNKNOWN)

        projection = RecoveryProjection(
            run_status=run_status,
            stop_reason=stop_reason,
            cancellation_reason=cancellation_reason,
            step_states=steps,
            budget_snapshot=snapshot.budget_snapshot,
            last_applied_sequence=last_applied,
            terminal_event_seen=terminal_seen,
            output_available=output_available,
            budget_exhausted=budget_exhausted,
        )
        return JournalTailReduction(
            projection=projection,
            tool_evidence=tuple(evidence),
            reconciliation_reasons=tuple(reasons),
        )


def _tool_evidence(
    record: JournalRecord, *, started: bool
) -> ToolRecoveryEvidence:
    payload = record.safe_payload
    evidence_version = payload.get("tool_evidence_schema_version")
    versioned = isinstance(evidence_version, int) and not isinstance(
        evidence_version, bool
    )
    return ToolRecoveryEvidence(
        tool_name=_required_text(payload.get("tool_name")),
        invocation_identity_digest=_evidence_identity_digest(
            payload,
            digest_name="invocation_identity_digest",
            legacy_name="invocation_id",
            versioned=versioned,
        ),
        attempt_identity_digest=_evidence_identity_digest(
            payload,
            digest_name="attempt_identity_digest",
            legacy_name="attempt_id",
            versioned=versioned,
        ),
        side_effect_kind=_optional_text(payload.get("side_effect_kind")),
        side_effect_state=(
            (
                _optional_text(payload.get("side_effect_state"))
                if versioned
                else "STARTED"
            )
            if started
            else _optional_text(payload.get("side_effect_state"))
        ),
        retry_disposition=(
            (
                _optional_text(payload.get("retry_disposition"))
                if versioned
                else None
            )
            if started
            else _optional_text(payload.get("retry_disposition"))
        ),
        execution_detached=(
            payload.get("execution_detached") is True
        ),
        worker_terminated=(
            payload.get("worker_terminated") is True
        ),
        safe_error_code=(
            _safe_error_code(payload.get("safe_error_code"))
        ),
        sequence=record.sequence,
        event_kind="STARTED" if started else "COMPLETED",
        step_id=record.step_id,
        attempt_sequence=(
            _nonnegative_int(payload.get("retry_index"))
            if payload.get("retry_index") is not None
            else None
        ),
        tool_evidence_schema_version=(
            evidence_version if versioned else None
        ),
        idempotency_kind=_optional_text(payload.get("idempotency_kind")),
        idempotency_key_digest=_optional_digest(
            payload.get("idempotency_key_digest")
        ),
        replay_supported=(
            payload.get("replay_supported")
            if type(payload.get("replay_supported")) is bool
            else None
        ),
        compensation_state=_optional_text(
            payload.get("compensation_state")
        ),
        outcome_classification=_optional_text(
            payload.get("outcome_classification")
        ),
        provider_started=(
            payload.get("provider_started")
            if type(payload.get("provider_started")) is bool
            else None
        ),
        succeeded=(
            payload.get("succeeded")
            if type(payload.get("succeeded")) is bool
            else None
        ),
    )


def _collect_tool_risk(
    item: ToolRecoveryEvidence, reasons: list[RecoveryReason]
) -> None:
    if item.side_effect_state == "COMMITTED":
        _append_reason(reasons, RecoveryReason.TOOL_SIDE_EFFECT_EVIDENCE)
    if (
        item.side_effect_state in {"UNKNOWN", "OUTCOME_UNKNOWN"}
        or item.retry_disposition == "OUTCOME_UNKNOWN"
    ):
        _append_reason(reasons, RecoveryReason.TOOL_OUTCOME_UNKNOWN)
    if item.execution_detached and not item.worker_terminated:
        _append_reason(reasons, RecoveryReason.DETACHED_WORKER_PRESENT)
    if item.safe_error_code == "COMPENSATION_FAILED":
        _append_reason(reasons, RecoveryReason.TOOL_COMPENSATION_FAILED)
    if item.safe_error_code == "POST_COMMIT_RESPONSE_FAILURE":
        _append_reason(reasons, RecoveryReason.TOOL_SIDE_EFFECT_EVIDENCE)


def _tool_key(
    item: ToolRecoveryEvidence,
) -> tuple[object, ...]:
    if (
        item.invocation_identity_digest is None
        or item.attempt_identity_digest is None
    ):
        return ("UNIDENTIFIED", item.sequence)
    return (
        item.invocation_identity_digest,
        item.attempt_identity_digest,
    )


def _model_key(payload: dict[str, object]) -> tuple[object, ...]:
    return (
        payload.get("profile_id"),
        payload.get("candidate_index"),
        payload.get("retry_index"),
    )


def _consume(mapping: dict, key: object) -> bool:
    count = mapping.get(key, 0)
    if count <= 0:
        return False
    if count == 1:
        del mapping[key]
    else:
        mapping[key] = count - 1
    return True


def _known_step(
    steps: dict[str, StepStateSnapshot], step_id: str | None
) -> StepStateSnapshot:
    if step_id is None or step_id not in steps:
        _corrupted()
    return steps[step_id]


def _run_status(value: object) -> RunStatus:
    try:
        return RunStatus(value)
    except (TypeError, ValueError):
        _corrupted()


def _step_status(value: object) -> StepStatus:
    try:
        return StepStatus(value)
    except (TypeError, ValueError):
        _corrupted()


def _stop_reason(value: object) -> StopReason:
    try:
        return StopReason(value)
    except (TypeError, ValueError):
        _corrupted()


def _validate_terminal(status: RunStatus, reason: StopReason) -> None:
    if status not in _TERMINAL_RUN_STATUSES:
        _corrupted()
    if status is RunStatus.SUCCEEDED and reason is not StopReason.COMPLETED:
        _corrupted()
    if status is RunStatus.CANCELLED and reason not in _CANCELLATION_REASONS:
        _corrupted()
    if status is RunStatus.FAILED and reason in (
        _CANCELLATION_REASONS | {StopReason.COMPLETED}
    ):
        _corrupted()


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _corrupted()
    return value


def _safe_error_code(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and _SAFE_ERROR_CODE_RE.fullmatch(value):
        return value
    return "UNSAFE_ERROR_CODE"


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _corrupted()
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _required_text(value)


def _identity_digest(value: object) -> str | None:
    if value is None:
        return None
    return text_digest(_required_text(value))


def _evidence_identity_digest(
    payload: dict[str, object],
    *,
    digest_name: str,
    legacy_name: str,
    versioned: bool,
) -> str | None:
    if versioned:
        return _optional_digest(payload.get(digest_name))
    return _identity_digest(payload.get(legacy_name))


def _optional_digest(value: object) -> str | None:
    if value is None:
        return None
    text = _required_text(value)
    if len(text) != 64 or any(
        char not in "0123456789abcdef" for char in text
    ):
        _corrupted()
    return text


def _append_reason(
    reasons: list[RecoveryReason], reason: RecoveryReason
) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _corrupted() -> None:
    raise JournalTailReductionError(
        RecoveryStatus.CORRUPTED,
        RecoveryReason.JOURNAL_RECORD_CORRUPTED,
    )


__all__ = [
    "IGNORED_EVENT_TYPES",
    "JournalTailReduction",
    "JournalTailReductionError",
    "JournalTailValidation",
    "JournalTailValidationError",
    "JournalTailValidator",
    "LimitedJournalTailReducer",
    "REDUCED_EVENT_TYPES",
    "SUPPORTED_RECOVERY_EVENT_TYPES",
]
