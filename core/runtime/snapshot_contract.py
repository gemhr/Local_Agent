#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Versioned, immutable and payload-safe runtime snapshot contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import Enum
import re
from types import MappingProxyType
from typing import Any

from core.runtime.budget import BudgetSnapshot as RuntimeBudgetSnapshot
from core.runtime.checkpoint_contract import CheckpointKind, RuntimeActivitySnapshot
from core.runtime.planning import Plan, PlanStep, PlanValidator
from core.runtime.snapshot_serialization import (
    parse_utc,
    require_finite_number,
    require_int,
    require_utc,
    sha256_digest,
    text_digest,
    to_primitive,
)
from core.runtime.state import AgentState, RunStatus, StepState, StepStatus, StopReason


SNAPSHOT_SCHEMA_VERSION = 1
PLAN_SNAPSHOT_SCHEMA_VERSION = 1
STATE_SNAPSHOT_SCHEMA_VERSION = 1
BUDGET_SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_DIGEST_ALGORITHM = "sha256-v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_CANCELLATION_STOP_REASONS = frozenset(
    {
        StopReason.USER_CANCELLED,
        StopReason.CLIENT_DISCONNECTED,
        StopReason.SYSTEM_SHUTDOWN,
    }
)


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_safe_token(value: object, field_name: str) -> str:
    text = _require_text(value, field_name)
    if _SAFE_TOKEN_RE.fullmatch(text) is None:
        raise ValueError(f"{field_name} must be a safe version token")
    return text


def _require_version_token(value: object, field_name: str) -> str | int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must not be bool")
    if isinstance(value, int):
        return require_int(value, field_name, minimum=1)
    return _require_safe_token(value, field_name)


def _safe_identifier(value: str) -> str:
    """Keep ordinary runtime identifiers, otherwise persist only their digest."""
    if isinstance(value, str) and _SAFE_TOKEN_RE.fullmatch(value):
        return value
    return f"sha256:{text_digest(str(value))}"


def _freeze_mapping(
    value: Mapping[str, Any], field_name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{field_name} keys must be strings")
        frozen[key] = _freeze_value(item, f"{field_name}.{key}")
    # Validate the complete value, including non-finite numbers and unsupported types.
    to_primitive(frozen)
    return MappingProxyType(frozen)


def _freeze_value(value: Any, field_name: str) -> Any:
    if isinstance(value, Enum):
        return _freeze_value(value.value, field_name)
    if isinstance(value, Mapping):
        return _freeze_mapping(value, field_name)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_value(item, f"{field_name}[{index}]")
            for index, item in enumerate(value)
        )
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        require_finite_number(value, field_name)
        return value
    raise ValueError(f"{field_name} contains an unsupported mutable value")


def _require_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _activity_snapshot_from_payload(
    payload: object,
) -> RuntimeActivitySnapshot | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise ValueError("activity_snapshot must be an object or null")
    expected = {item.name for item in fields(RuntimeActivitySnapshot)}
    if set(payload) != expected:
        raise ValueError("activity_snapshot fields do not match the v1 schema")
    return RuntimeActivitySnapshot(
        claim_in_progress=payload["claim_in_progress"],
        running_step_count=payload["running_step_count"],
        budget_reservation_count=payload["budget_reservation_count"],
        model_attempts_active=payload["model_attempts_active"],
        tool_attempts_active=payload["tool_attempts_active"],
        retrievals_active=payload["retrievals_active"],
        detached_tool_workers=payload["detached_tool_workers"],
        detached_retrieval_workers=payload["detached_retrieval_workers"],
        event_publications_in_flight=payload["event_publications_in_flight"],
        step_workers_active=payload["step_workers_active"],
        activity_unknown=payload["activity_unknown"],
        captured_at=parse_utc(
            payload["captured_at"], "activity_snapshot.captured_at"
        ),
        state_event_transitions_in_flight=payload[
            "state_event_transitions_in_flight"
        ],
        state_event_transition_epoch=payload[
            "state_event_transition_epoch"
        ],
        state_event_transition_observed=payload[
            "state_event_transition_observed"
        ],
    )


@dataclass(frozen=True, slots=True)
class TextSummary:
    """A content-free summary; length is Unicode code-point length."""

    present: bool
    length: int
    digest: str | None

    def __post_init__(self) -> None:
        if type(self.present) is not bool:
            raise ValueError("present must be bool")
        require_int(self.length, "length", minimum=0)
        if self.present:
            if self.digest is None:
                raise ValueError("present text must include a digest")
            _require_digest(self.digest, "digest")
        elif self.length != 0 or self.digest is not None:
            raise ValueError("absent text must use length=0 and digest=None")

    @classmethod
    def from_text(cls, value: str | None) -> "TextSummary":
        if value is None:
            return cls(False, 0, None)
        if not isinstance(value, str):
            raise TypeError("summarized content must be str or None")
        return cls(True, len(value), text_digest(value))

    @classmethod
    def from_payload(cls, payload: object) -> "TextSummary":
        if not isinstance(payload, Mapping):
            raise ValueError("text summary must be an object")
        return cls(
            present=payload.get("present"),
            length=payload.get("length"),
            digest=payload.get("digest"),
        )


@dataclass(frozen=True, slots=True)
class PlanStepSnapshot:
    step_id: str
    agent: str
    dependency_step_ids: tuple[str, ...]
    static_execution_kind: str
    capability_requirements: Mapping[str, Any]
    completion_criteria: TextSummary
    static_inputs: Mapping[str, TextSummary]

    def __post_init__(self) -> None:
        _require_safe_token(self.step_id, "step_id")
        _require_safe_token(self.agent, "agent")
        _require_safe_token(self.static_execution_kind, "static_execution_kind")
        if not isinstance(self.dependency_step_ids, tuple):
            raise ValueError("dependency_step_ids must be a tuple")
        if len(set(self.dependency_step_ids)) != len(self.dependency_step_ids):
            raise ValueError("dependency_step_ids must be unique")
        for item in self.dependency_step_ids:
            _require_safe_token(item, "dependency_step_id")
        object.__setattr__(
            self,
            "capability_requirements",
            _freeze_mapping(self.capability_requirements, "capability_requirements"),
        )
        if not isinstance(self.completion_criteria, TextSummary):
            raise ValueError("completion_criteria must be a TextSummary")
        if not isinstance(self.static_inputs, Mapping):
            raise ValueError("static_inputs must be a mapping")
        inputs: dict[str, TextSummary] = {}
        for key, value in self.static_inputs.items():
            _require_safe_token(key, "static input name")
            if not isinstance(value, TextSummary):
                raise ValueError("static input values must be TextSummary")
            inputs[key] = value
        object.__setattr__(self, "static_inputs", MappingProxyType(inputs))

    @classmethod
    def from_plan_step(cls, step: PlanStep) -> "PlanStepSnapshot":
        capability = {
            item.name: (
                getattr(step.capability_requirements, item.name).value
                if isinstance(getattr(step.capability_requirements, item.name), Enum)
                else getattr(step.capability_requirements, item.name)
            )
            for item in fields(step.capability_requirements)
        }
        return cls(
            step_id=_safe_identifier(step.step_id),
            agent=_safe_identifier(step.preferred_agent),
            dependency_step_ids=tuple(
                sorted(_safe_identifier(item) for item in step.depends_on)
            ),
            static_execution_kind="AGENT",
            capability_requirements=capability,
            completion_criteria=TextSummary.from_text(step.completion_criteria),
            static_inputs=MappingProxyType(
                {
                    "description": TextSummary.from_text(step.description),
                    "title": TextSummary.from_text(step.title),
                }
            ),
        )

    @classmethod
    def from_payload(cls, payload: object) -> "PlanStepSnapshot":
        if not isinstance(payload, Mapping):
            raise ValueError("plan step snapshot must be an object")
        dependencies = payload.get("dependency_step_ids")
        static_inputs = payload.get("static_inputs")
        if not isinstance(dependencies, list) or not isinstance(static_inputs, Mapping):
            raise ValueError("plan step snapshot has invalid collection fields")
        return cls(
            step_id=payload.get("step_id"),
            agent=payload.get("agent"),
            dependency_step_ids=tuple(dependencies),
            static_execution_kind=payload.get("static_execution_kind"),
            capability_requirements=payload.get("capability_requirements"),
            completion_criteria=TextSummary.from_payload(
                payload.get("completion_criteria")
            ),
            static_inputs={
                str(key): TextSummary.from_payload(value)
                for key, value in static_inputs.items()
            },
        )


@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    plan_schema_version: int
    plan_id: str
    plan_version: int
    source: str
    task_summary: TextSummary
    steps: tuple[PlanStepSnapshot, ...]

    def __post_init__(self) -> None:
        require_int(self.plan_schema_version, "plan_schema_version", minimum=1)
        _require_safe_token(self.plan_id, "plan_id")
        require_int(self.plan_version, "plan_version", minimum=1)
        _require_safe_token(self.source, "source")
        if not isinstance(self.task_summary, TextSummary):
            raise ValueError("task_summary must be a TextSummary")
        if not isinstance(self.steps, tuple) or not self.steps:
            raise ValueError("steps must be a non-empty tuple")
        if not all(isinstance(step, PlanStepSnapshot) for step in self.steps):
            raise ValueError("steps must contain PlanStepSnapshot values")
        step_ids = tuple(step.step_id for step in self.steps)
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("snapshot step IDs must be unique")
        if step_ids != tuple(sorted(step_ids)):
            raise ValueError("snapshot steps must be sorted by step_id")
        known = set(step_ids)
        if any(
            dependency not in known
            for step in self.steps
            for dependency in step.dependency_step_ids
        ):
            raise ValueError("snapshot dependency must reference a known step")

    @classmethod
    def from_plan(cls, plan: Plan) -> "PlanSnapshot":
        PlanValidator.validate(plan)
        return cls(
            plan_schema_version=PLAN_SNAPSHOT_SCHEMA_VERSION,
            plan_id=_safe_identifier(plan.plan_id),
            plan_version=plan.version,
            source=plan.source.value,
            task_summary=TextSummary.from_text(plan.task_summary),
            steps=tuple(
                sorted(
                    (PlanStepSnapshot.from_plan_step(step) for step in plan.steps),
                    key=lambda item: item.step_id,
                )
            ),
        )

    @classmethod
    def from_payload(cls, payload: object) -> "PlanSnapshot":
        if not isinstance(payload, Mapping) or not isinstance(payload.get("steps"), list):
            raise ValueError("plan snapshot must be an object with steps")
        return cls(
            plan_schema_version=payload.get("plan_schema_version"),
            plan_id=payload.get("plan_id"),
            plan_version=payload.get("plan_version"),
            source=payload.get("source"),
            task_summary=TextSummary.from_payload(payload.get("task_summary")),
            steps=tuple(PlanStepSnapshot.from_payload(item) for item in payload["steps"]),
        )


@dataclass(frozen=True, slots=True)
class StepStateSnapshot:
    step_id: str
    status: str
    in_flight: bool
    execution_started: bool
    attempt_count: int | None
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    safe_error_code: str | None
    result: TextSummary

    def __post_init__(self) -> None:
        _require_safe_token(self.step_id, "step_id")
        status = StepStatus(self.status)
        if type(self.in_flight) is not bool:
            raise ValueError("in_flight must be bool")
        if self.in_flight != (status is StepStatus.RUNNING):
            raise ValueError("only RUNNING is an in-flight step status")
        if type(self.execution_started) is not bool:
            raise ValueError("execution_started must be bool")
        if self.execution_started != (self.started_at is not None):
            raise ValueError("execution_started must match started_at presence")
        if status in {
            StepStatus.PENDING,
            StepStatus.BLOCKED,
            StepStatus.SKIPPED,
        } and self.execution_started:
            raise ValueError("unstarted step status cannot have execution_started")
        if status in {
            StepStatus.RUNNING,
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
        } and not self.execution_started:
            raise ValueError("started step status requires execution_started")
        if self.attempt_count is not None:
            require_int(self.attempt_count, "attempt_count", minimum=0)
            if self.execution_started != (self.attempt_count > 0):
                raise ValueError(
                    "exact attempt_count must agree with execution_started"
                )
        if self.started_at is not None:
            require_utc(self.started_at, "started_at")
        if self.completed_at is not None:
            require_utc(self.completed_at, "completed_at")
        if self.duration_ms is not None:
            require_int(self.duration_ms, "duration_ms", minimum=0)
        if self.safe_error_code is not None:
            if _SAFE_ERROR_CODE_RE.fullmatch(self.safe_error_code) is None:
                raise ValueError("safe_error_code must use the safe code grammar")
        if not isinstance(self.result, TextSummary):
            raise ValueError("result must be a TextSummary")
        if status is StepStatus.BLOCKED and self.in_flight:
            raise ValueError("BLOCKED must never be treated as in-flight")

    @classmethod
    def from_step_state(
        cls,
        step: StepState,
        *,
        attempt_count: int | None = None,
        result: str | None = None,
    ) -> "StepStateSnapshot":
        step.validate()
        duration_ms = None
        if step.started_at is not None and step.ended_at is not None:
            duration_ms = max(
                0, int((step.ended_at - step.started_at).total_seconds() * 1000)
            )
        safe_error_code = (
            step.error_code
            if step.error_code is not None
            and _SAFE_ERROR_CODE_RE.fullmatch(step.error_code)
            else ("UNSAFE_ERROR_CODE" if step.error_code else None)
        )
        return cls(
            step_id=_safe_identifier(step.step_id),
            status=step.status.value,
            in_flight=step.status is StepStatus.RUNNING,
            execution_started=step.started_at is not None,
            attempt_count=attempt_count,
            started_at=step.started_at,
            completed_at=step.ended_at,
            duration_ms=duration_ms,
            safe_error_code=safe_error_code,
            result=TextSummary.from_text(result),
        )

    @classmethod
    def from_payload(cls, payload: object) -> "StepStateSnapshot":
        if not isinstance(payload, Mapping):
            raise ValueError("step state snapshot must be an object")
        return cls(
            step_id=payload.get("step_id"),
            status=payload.get("status"),
            in_flight=payload.get("in_flight"),
            execution_started=payload.get("execution_started"),
            attempt_count=payload.get("attempt_count"),
            started_at=(
                parse_utc(payload["started_at"], "started_at")
                if payload.get("started_at") is not None
                else None
            ),
            completed_at=(
                parse_utc(payload["completed_at"], "completed_at")
                if payload.get("completed_at") is not None
                else None
            ),
            duration_ms=payload.get("duration_ms"),
            safe_error_code=payload.get("safe_error_code"),
            result=TextSummary.from_payload(payload.get("result")),
        )


@dataclass(frozen=True, slots=True)
class AgentStateSnapshot:
    state_schema_version: int
    run_status: str
    stop_reason: str | None
    cancellation_reason: str | None
    step_states: tuple[StepStateSnapshot, ...]
    final_output: TextSummary
    safe_error_code: str | None
    state_version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        require_int(self.state_schema_version, "state_schema_version", minimum=1)
        RunStatus(self.run_status)
        if self.stop_reason is not None:
            StopReason(self.stop_reason)
        if self.cancellation_reason is not None:
            StopReason(self.cancellation_reason)
        if not isinstance(self.step_states, tuple):
            raise ValueError("step_states must be a tuple")
        if not all(isinstance(item, StepStateSnapshot) for item in self.step_states):
            raise ValueError("step_states must contain StepStateSnapshot values")
        step_ids = tuple(item.step_id for item in self.step_states)
        if step_ids != tuple(sorted(step_ids)) or len(step_ids) != len(set(step_ids)):
            raise ValueError("step_states must have unique, sorted step IDs")
        if not isinstance(self.final_output, TextSummary):
            raise ValueError("final_output must be a TextSummary")
        if self.safe_error_code is not None and _SAFE_ERROR_CODE_RE.fullmatch(
            self.safe_error_code
        ) is None:
            raise ValueError("safe_error_code must use the safe code grammar")
        require_int(self.state_version, "state_version", minimum=1)
        require_utc(self.updated_at, "updated_at")

    @classmethod
    def from_agent_state(
        cls,
        state: AgentState,
        *,
        attempt_counts: Mapping[str, int] | None = None,
        step_results: Mapping[str, str | None] | None = None,
    ) -> "AgentStateSnapshot":
        state.validate()
        attempt_counts = attempt_counts or {}
        step_results = step_results or {}
        steps = tuple(
            StepStateSnapshot.from_step_state(
                state.steps[step_id],
                attempt_count=attempt_counts.get(step_id),
                result=step_results.get(step_id),
            )
            for step_id in sorted(state.steps)
        )
        cancellation_reason = (
            state.stop_reason.value
            if state.stop_reason in _CANCELLATION_STOP_REASONS
            else None
        )
        safe_error_code = (
            state.error_code
            if state.error_code is not None
            and _SAFE_ERROR_CODE_RE.fullmatch(state.error_code)
            else ("UNSAFE_ERROR_CODE" if state.error_code else None)
        )
        return cls(
            state_schema_version=STATE_SNAPSHOT_SCHEMA_VERSION,
            run_status=state.status.value,
            stop_reason=state.stop_reason.value if state.stop_reason else None,
            cancellation_reason=cancellation_reason,
            step_states=steps,
            final_output=TextSummary.from_text(state.final_output),
            safe_error_code=safe_error_code,
            state_version=state.schema_version,
            updated_at=state.updated_at,
        )

    @classmethod
    def from_payload(cls, payload: object) -> "AgentStateSnapshot":
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("step_states"), list
        ):
            raise ValueError("agent state snapshot must be an object with step_states")
        return cls(
            state_schema_version=payload.get("state_schema_version"),
            run_status=payload.get("run_status"),
            stop_reason=payload.get("stop_reason"),
            cancellation_reason=payload.get("cancellation_reason"),
            step_states=tuple(
                StepStateSnapshot.from_payload(item) for item in payload["step_states"]
            ),
            final_output=TextSummary.from_payload(payload.get("final_output")),
            safe_error_code=payload.get("safe_error_code"),
            state_version=payload.get("state_version"),
            updated_at=parse_utc(payload.get("updated_at"), "updated_at"),
        )


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Safe ledger projection; None explicitly means an unlimited dimension."""

    budget_schema_version: int
    limits: Mapping[str, int | float | None]
    used: Mapping[str, int | float]
    reserved: Mapping[str, int | float]
    remaining: Mapping[str, int | float | None]
    ledger_version: int
    reservation_count: int
    generated_at: datetime

    def __post_init__(self) -> None:
        require_int(self.budget_schema_version, "budget_schema_version", minimum=1)
        object.__setattr__(self, "limits", _freeze_mapping(self.limits, "limits"))
        object.__setattr__(self, "used", _freeze_mapping(self.used, "used"))
        object.__setattr__(self, "reserved", _freeze_mapping(self.reserved, "reserved"))
        object.__setattr__(
            self, "remaining", _freeze_mapping(self.remaining, "remaining")
        )
        keys = set(self.limits)
        if not keys or keys != set(self.used) or keys != set(self.reserved) or keys != set(
            self.remaining
        ):
            raise ValueError("budget mappings must use the same non-empty dimensions")
        for key in keys:
            limit = self.limits[key]
            used = require_finite_number(self.used[key], f"used.{key}", minimum=0)
            reserved = require_finite_number(
                self.reserved[key], f"reserved.{key}", minimum=0
            )
            remaining = self.remaining[key]
            if limit is None:
                if remaining is not None:
                    raise ValueError("unlimited dimensions must use remaining=None")
                continue
            limit_number = require_finite_number(limit, f"limits.{key}", minimum=0)
            remaining_number = require_finite_number(
                remaining, f"remaining.{key}", minimum=0
            )
            if used + reserved > limit_number:
                raise ValueError("used + reserved must not exceed limit")
            if remaining_number != limit_number - used - reserved:
                raise ValueError("remaining must equal limit - used - reserved")
        require_int(self.ledger_version, "ledger_version", minimum=1)
        require_int(self.reservation_count, "reservation_count", minimum=0)
        require_utc(self.generated_at, "generated_at")

    @classmethod
    def from_runtime_snapshot(
        cls, snapshot: RuntimeBudgetSnapshot, *, ledger_version: int = 1
    ) -> "BudgetSnapshot":
        dimensions = {
            "step_starts": "max_step_starts",
            "model_calls": "max_model_calls",
            "remote_model_calls": "max_remote_model_calls",
            "tool_calls": "max_tool_calls",
            "input_tokens": "max_input_tokens",
            "output_tokens": "max_output_tokens",
            "total_tokens": "max_total_tokens",
            "cost_units": "max_cost_units",
            "retries": "max_retries",
            "retrieval_calls": "max_retrieval_calls",
            "embedding_calls": "max_embedding_calls",
            "vector_queries": "max_vector_queries",
            "keyword_queries": "max_keyword_queries",
            "document_reads": "max_document_reads",
            "context_chars": "max_context_chars",
        }
        limits: dict[str, int | float | None] = {}
        used: dict[str, int | float] = {}
        reserved: dict[str, int | float] = {}
        remaining: dict[str, int | float | None] = {}
        for dimension, limit_name in dimensions.items():
            limit = getattr(snapshot.run_budget, limit_name)
            limits[dimension] = limit
            used[dimension] = getattr(snapshot.committed_usage, dimension)
            reserved[dimension] = getattr(snapshot.reserved_usage, dimension)
            remaining[dimension] = (
                getattr(snapshot.remaining, dimension) if limit is not None else None
            )
        limits["elapsed_seconds"] = snapshot.run_budget.max_elapsed_seconds
        used["elapsed_seconds"] = (
            min(snapshot.elapsed_seconds, snapshot.run_budget.max_elapsed_seconds)
            if snapshot.run_budget.max_elapsed_seconds is not None
            else snapshot.elapsed_seconds
        )
        reserved["elapsed_seconds"] = 0.0
        remaining["elapsed_seconds"] = (
            snapshot.run_budget.max_elapsed_seconds - used["elapsed_seconds"]
            if snapshot.run_budget.max_elapsed_seconds is not None
            else None
        )
        return cls(
            budget_schema_version=BUDGET_SNAPSHOT_SCHEMA_VERSION,
            limits=limits,
            used=used,
            reserved=reserved,
            remaining=remaining,
            ledger_version=ledger_version,
            reservation_count=snapshot.active_reservation_count,
            generated_at=snapshot.generated_at,
        )

    @classmethod
    def from_payload(cls, payload: object) -> "BudgetSnapshot":
        if not isinstance(payload, Mapping):
            raise ValueError("budget snapshot must be an object")
        return cls(
            budget_schema_version=payload.get("budget_schema_version"),
            limits=payload.get("limits"),
            used=payload.get("used"),
            reserved=payload.get("reserved"),
            remaining=payload.get("remaining"),
            ledger_version=payload.get("ledger_version"),
            reservation_count=payload.get("reservation_count"),
            generated_at=parse_utc(payload.get("generated_at"), "generated_at"),
        )


@dataclass(frozen=True, slots=True)
class RuntimeMetadata:
    runtime_schema_version: int
    runtime_mode: str
    planner_version: str | int
    scheduler_version: str | int
    model_routing_policy_version: str | int
    tool_contract_version: str | int
    retrieval_contract_version: str | int
    event_schema_version: str | int
    journal_schema_version: str | int

    def __post_init__(self) -> None:
        require_int(self.runtime_schema_version, "runtime_schema_version", minimum=1)
        _require_safe_token(self.runtime_mode, "runtime_mode")
        for item in fields(self):
            if item.name not in {"runtime_schema_version", "runtime_mode"}:
                _require_version_token(getattr(self, item.name), item.name)

    @classmethod
    def from_payload(cls, payload: object) -> "RuntimeMetadata":
        if not isinstance(payload, Mapping):
            raise ValueError("runtime metadata must be an object")
        allowed = {item.name for item in fields(cls)}
        if set(payload) != allowed:
            raise ValueError("runtime metadata must contain only allowlisted fields")
        return cls(**{name: payload[name] for name in allowed})


@dataclass(frozen=True, slots=True, repr=False)
class RunSnapshot:
    snapshot_schema_version: int
    snapshot_id: str
    run_id: str
    trace_id: str
    plan_snapshot: PlanSnapshot
    plan_fingerprint: str
    state_snapshot: AgentStateSnapshot
    budget_snapshot: BudgetSnapshot
    last_journal_sequence: int
    run_status: str
    stop_reason: str | None
    cancellation_reason: str | None
    step_states: tuple[StepStateSnapshot, ...]
    runtime_metadata: RuntimeMetadata
    checkpoint_kind: str
    quiescent: bool
    activity_snapshot: RuntimeActivitySnapshot | None
    created_at: datetime
    payload_digest: str

    def __post_init__(self) -> None:
        if self.snapshot_schema_version != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("unsupported snapshot schema version")
        require_int(
            self.snapshot_schema_version, "snapshot_schema_version", minimum=1
        )
        _require_safe_token(self.snapshot_id, "snapshot_id")
        _require_safe_token(self.run_id, "run_id")
        _require_safe_token(self.trace_id, "trace_id")
        if not isinstance(self.plan_snapshot, PlanSnapshot):
            raise ValueError("plan_snapshot must be a PlanSnapshot")
        _require_digest(self.plan_fingerprint, "plan_fingerprint")
        if not isinstance(self.state_snapshot, AgentStateSnapshot):
            raise ValueError("state_snapshot must be an AgentStateSnapshot")
        if not isinstance(self.budget_snapshot, BudgetSnapshot):
            raise ValueError("budget_snapshot must be a BudgetSnapshot")
        require_int(
            self.last_journal_sequence, "last_journal_sequence", minimum=0
        )
        RunStatus(self.run_status)
        if self.stop_reason is not None:
            StopReason(self.stop_reason)
        if self.cancellation_reason is not None:
            StopReason(self.cancellation_reason)
        if not isinstance(self.runtime_metadata, RuntimeMetadata):
            raise ValueError("runtime_metadata must be RuntimeMetadata")
        checkpoint_kind = CheckpointKind(self.checkpoint_kind)
        if type(self.quiescent) is not bool:
            raise ValueError("quiescent must be bool")
        if self.activity_snapshot is not None and not isinstance(
            self.activity_snapshot, RuntimeActivitySnapshot
        ):
            raise ValueError("activity_snapshot must be RuntimeActivitySnapshot")
        if (
            checkpoint_kind is not CheckpointKind.OBSERVATION
            and self.activity_snapshot is None
        ):
            raise ValueError("checkpoint snapshot requires activity_snapshot")
        if self.quiescent and (
            self.activity_snapshot is None or not self.activity_snapshot.quiescent
        ):
            raise ValueError("quiescent snapshot requires quiescent activity")
        if (
            checkpoint_kind is CheckpointKind.NON_QUIESCENT_AUDIT
            and self.quiescent
        ):
            raise ValueError("audit checkpoint must be non-quiescent")
        require_utc(self.created_at, "created_at")
        _require_digest(self.payload_digest, "payload_digest")
        self.validate_consistency()

    @classmethod
    def create(
        cls,
        *,
        snapshot_id: str,
        run_id: str,
        trace_id: str,
        plan_snapshot: PlanSnapshot,
        plan_fingerprint: str,
        state_snapshot: AgentStateSnapshot,
        budget_snapshot: BudgetSnapshot,
        last_journal_sequence: int,
        runtime_metadata: RuntimeMetadata,
        checkpoint_kind: str | CheckpointKind,
        quiescent: bool,
        activity_snapshot: RuntimeActivitySnapshot | None = None,
        created_at: datetime | None = None,
    ) -> "RunSnapshot":
        created = created_at if created_at is not None else datetime.now(timezone.utc)
        source = cls._digest_source_values(
            snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
            snapshot_id=snapshot_id,
            run_id=run_id,
            trace_id=trace_id,
            plan_snapshot=plan_snapshot,
            plan_fingerprint=plan_fingerprint,
            state_snapshot=state_snapshot,
            budget_snapshot=budget_snapshot,
            last_journal_sequence=last_journal_sequence,
            run_status=state_snapshot.run_status,
            stop_reason=state_snapshot.stop_reason,
            cancellation_reason=state_snapshot.cancellation_reason,
            step_states=state_snapshot.step_states,
            runtime_metadata=runtime_metadata,
            checkpoint_kind=(
                checkpoint_kind.value
                if isinstance(checkpoint_kind, CheckpointKind)
                else checkpoint_kind
            ),
            quiescent=quiescent,
            activity_snapshot=activity_snapshot,
            created_at=created,
        )
        return cls(
            **source,
            payload_digest=sha256_digest(source),
        )

    def digest_source(self) -> dict[str, object]:
        return self._digest_source_values(
            snapshot_schema_version=self.snapshot_schema_version,
            snapshot_id=self.snapshot_id,
            run_id=self.run_id,
            trace_id=self.trace_id,
            plan_snapshot=self.plan_snapshot,
            plan_fingerprint=self.plan_fingerprint,
            state_snapshot=self.state_snapshot,
            budget_snapshot=self.budget_snapshot,
            last_journal_sequence=self.last_journal_sequence,
            run_status=self.run_status,
            stop_reason=self.stop_reason,
            cancellation_reason=self.cancellation_reason,
            step_states=self.step_states,
            runtime_metadata=self.runtime_metadata,
            checkpoint_kind=self.checkpoint_kind,
            quiescent=self.quiescent,
            activity_snapshot=self.activity_snapshot,
            created_at=self.created_at,
        )

    @staticmethod
    def _digest_source_values(**values: object) -> dict[str, object]:
        return dict(values)

    def verify_digest(self) -> None:
        self.validate_consistency()
        if sha256_digest(self.digest_source()) != self.payload_digest:
            raise ValueError("snapshot digest verification failed")

    def validate_consistency(self) -> None:
        """Fail closed on semantic cross-field corruption without payload details."""
        if not isinstance(self.step_states, tuple) or self.step_states != (
            self.state_snapshot.step_states
        ):
            raise ValueError("snapshot step states are inconsistent")
        if self.run_status != self.state_snapshot.run_status:
            raise ValueError("snapshot run status is inconsistent")
        if self.stop_reason != self.state_snapshot.stop_reason:
            raise ValueError("snapshot stop reason is inconsistent")
        if self.cancellation_reason != self.state_snapshot.cancellation_reason:
            raise ValueError("snapshot cancellation reason is inconsistent")
        plan_ids = tuple(item.step_id for item in self.plan_snapshot.steps)
        state_ids = tuple(item.step_id for item in self.state_snapshot.step_states)
        top_ids = tuple(item.step_id for item in self.step_states)
        if plan_ids != state_ids or state_ids != top_ids:
            raise ValueError("snapshot step ID sets are inconsistent")
        from core.runtime.plan_fingerprint import PlanFingerprinter

        if (
            PlanFingerprinter.fingerprint_snapshot(self.plan_snapshot)
            != self.plan_fingerprint
        ):
            raise ValueError("snapshot plan fingerprint is inconsistent")
        if self.quiescent and any(item.in_flight for item in self.step_states):
            raise ValueError("quiescent snapshot cannot contain running steps")
        if self.quiescent and (
            self.budget_snapshot.reservation_count != 0
            or any(value != 0 for value in self.budget_snapshot.reserved.values())
        ):
            raise ValueError("quiescent snapshot cannot contain budget reservations")

    def to_payload(self) -> dict[str, object]:
        payload = to_primitive(self.digest_source())
        payload["payload_digest"] = self.payload_digest
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> "RunSnapshot":
        if not isinstance(payload, Mapping):
            raise ValueError("snapshot payload must be an object")
        expected = {item.name for item in fields(cls)}
        if set(payload) != expected:
            raise ValueError("snapshot payload fields do not match the v1 schema")
        version = payload.get("snapshot_schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("snapshot_schema_version must be an integer")
        if version != SNAPSHOT_SCHEMA_VERSION:
            raise UnsupportedSnapshotSchemaError()
        raw_steps = payload.get("step_states")
        if not isinstance(raw_steps, list):
            raise ValueError("step_states must be an array")
        snapshot = cls(
            snapshot_schema_version=version,
            snapshot_id=payload.get("snapshot_id"),
            run_id=payload.get("run_id"),
            trace_id=payload.get("trace_id"),
            plan_snapshot=PlanSnapshot.from_payload(payload.get("plan_snapshot")),
            plan_fingerprint=payload.get("plan_fingerprint"),
            state_snapshot=AgentStateSnapshot.from_payload(
                payload.get("state_snapshot")
            ),
            budget_snapshot=BudgetSnapshot.from_payload(
                payload.get("budget_snapshot")
            ),
            last_journal_sequence=payload.get("last_journal_sequence"),
            run_status=payload.get("run_status"),
            stop_reason=payload.get("stop_reason"),
            cancellation_reason=payload.get("cancellation_reason"),
            step_states=tuple(StepStateSnapshot.from_payload(item) for item in raw_steps),
            runtime_metadata=RuntimeMetadata.from_payload(
                payload.get("runtime_metadata")
            ),
            checkpoint_kind=payload.get("checkpoint_kind"),
            quiescent=payload.get("quiescent"),
            activity_snapshot=_activity_snapshot_from_payload(
                payload.get("activity_snapshot")
            ),
            created_at=parse_utc(payload.get("created_at"), "created_at"),
            payload_digest=payload.get("payload_digest"),
        )
        snapshot.verify_digest()
        return snapshot

    def __repr__(self) -> str:
        return (
            "RunSnapshot("
            f"schema_version={self.snapshot_schema_version!r}, "
            f"snapshot_id={self.snapshot_id!r}, run_id={self.run_id!r}, "
            f"trace_id={self.trace_id!r}, "
            f"last_journal_sequence={self.last_journal_sequence!r}, "
            f"run_status={self.run_status!r}, checkpoint_kind={self.checkpoint_kind!r}, "
            f"quiescent={self.quiescent!r}, created_at={self.created_at!r}, "
            f"payload_digest={self.payload_digest!r})"
        )


class UnsupportedSnapshotSchemaError(ValueError):
    """Internal typed marker mapped to SNAPSHOT_SCHEMA_UNSUPPORTED by stores."""

    def __init__(self) -> None:
        super().__init__("snapshot schema is unsupported")


__all__ = [
    "AgentStateSnapshot",
    "BUDGET_SNAPSHOT_SCHEMA_VERSION",
    "BudgetSnapshot",
    "PLAN_SNAPSHOT_SCHEMA_VERSION",
    "PlanSnapshot",
    "PlanStepSnapshot",
    "RunSnapshot",
    "RuntimeMetadata",
    "SNAPSHOT_DIGEST_ALGORITHM",
    "SNAPSHOT_SCHEMA_VERSION",
    "STATE_SNAPSHOT_SCHEMA_VERSION",
    "StepStateSnapshot",
    "TextSummary",
    "UnsupportedSnapshotSchemaError",
]
