#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Content-free contracts for deterministic, test-only runtime fault injection."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Final

FAULT_PLAN_SCHEMA_VERSION: Final[int] = 1
MAX_SAFE_TOKEN_LENGTH: Final[int] = 128
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FaultPoint(str, Enum):
    MODEL_BEFORE_INVOCATION = "MODEL_BEFORE_INVOCATION"
    MODEL_BEFORE_PROVIDER_CALL = "MODEL_BEFORE_PROVIDER_CALL"
    MODEL_AFTER_PROVIDER_SUCCESS = "MODEL_AFTER_PROVIDER_SUCCESS"
    MODEL_BEFORE_USAGE_COMMIT = "MODEL_BEFORE_USAGE_COMMIT"
    MODEL_AFTER_USAGE_COMMIT = "MODEL_AFTER_USAGE_COMMIT"

    TOOL_BEFORE_INVOCATION = "TOOL_BEFORE_INVOCATION"
    TOOL_BEFORE_ATTEMPT = "TOOL_BEFORE_ATTEMPT"
    TOOL_BEFORE_PROVIDER_CALL = "TOOL_BEFORE_PROVIDER_CALL"
    TOOL_AFTER_PROVIDER_RETURN = "TOOL_AFTER_PROVIDER_RETURN"
    TOOL_BEFORE_SIDE_EFFECT_COMMIT = "TOOL_BEFORE_SIDE_EFFECT_COMMIT"
    TOOL_AFTER_SIDE_EFFECT_COMMIT = "TOOL_AFTER_SIDE_EFFECT_COMMIT"
    TOOL_BEFORE_COMPLETION_EVENT = "TOOL_BEFORE_COMPLETION_EVENT"

    RETRIEVAL_BEFORE_REWRITE = "RETRIEVAL_BEFORE_REWRITE"
    RETRIEVAL_AFTER_REWRITE = "RETRIEVAL_AFTER_REWRITE"
    RETRIEVAL_BEFORE_SEARCH = "RETRIEVAL_BEFORE_SEARCH"
    RETRIEVAL_AFTER_SEARCH = "RETRIEVAL_AFTER_SEARCH"
    RETRIEVAL_BEFORE_RESULT_COMMIT = "RETRIEVAL_BEFORE_RESULT_COMMIT"

    EVENT_BEFORE_JOURNAL_APPEND = "EVENT_BEFORE_JOURNAL_APPEND"
    EVENT_AFTER_JOURNAL_APPEND = "EVENT_AFTER_JOURNAL_APPEND"
    EVENT_BEFORE_CHANNEL_ENQUEUE = "EVENT_BEFORE_CHANNEL_ENQUEUE"
    JOURNAL_BEFORE_READ = "JOURNAL_BEFORE_READ"
    JOURNAL_BEFORE_TERMINAL_APPEND = "JOURNAL_BEFORE_TERMINAL_APPEND"

    SNAPSHOT_BEFORE_SAVE = "SNAPSHOT_BEFORE_SAVE"
    SNAPSHOT_AFTER_SAVE = "SNAPSHOT_AFTER_SAVE"
    SNAPSHOT_BEFORE_READ = "SNAPSHOT_BEFORE_READ"
    RECOVERY_BEFORE_TAIL_READ = "RECOVERY_BEFORE_TAIL_READ"
    RECOVERY_AFTER_TAIL_READ = "RECOVERY_AFTER_TAIL_READ"

    EXECUTOR_BEFORE_SUBMIT = "EXECUTOR_BEFORE_SUBMIT"
    EXECUTOR_AFTER_SUBMIT = "EXECUTOR_AFTER_SUBMIT"
    CHANNEL_BEFORE_RECEIVE = "CHANNEL_BEFORE_RECEIVE"
    CHANNEL_BEFORE_DRAIN_HANDOFF = "CHANNEL_BEFORE_DRAIN_HANDOFF"

    OBSERVABILITY_BEFORE_RECORD = "OBSERVABILITY_BEFORE_RECORD"
    OBSERVABILITY_BEFORE_FLUSH = "OBSERVABILITY_BEFORE_FLUSH"
    TRACE_BEFORE_SPAN_START = "TRACE_BEFORE_SPAN_START"
    TRACE_BEFORE_SPAN_END = "TRACE_BEFORE_SPAN_END"
    TRACE_BEFORE_FLUSH = "TRACE_BEFORE_FLUSH"

    SHUTDOWN_BEFORE_RUN_CANCEL = "SHUTDOWN_BEFORE_RUN_CANCEL"
    SHUTDOWN_BEFORE_WORKER_DRAIN = "SHUTDOWN_BEFORE_WORKER_DRAIN"
    SHUTDOWN_BEFORE_JOURNAL_CLOSE = "SHUTDOWN_BEFORE_JOURNAL_CLOSE"
    SHUTDOWN_BEFORE_MODEL_CLOSE = "SHUTDOWN_BEFORE_MODEL_CLOSE"
    SHUTDOWN_COMPONENT_CLOSE = "SHUTDOWN_COMPONENT_CLOSE"


class FaultAction(str, Enum):
    RAISE_TYPED_ERROR = "RAISE_TYPED_ERROR"
    DELAY = "DELAY"
    BLOCK_UNTIL_RELEASED = "BLOCK_UNTIL_RELEASED"
    RETURN_TYPED_FAILURE = "RETURN_TYPED_FAILURE"
    CORRUPT_TEST_FIXTURE = "CORRUPT_TEST_FIXTURE"


class FaultTrigger(str, Enum):
    ALWAYS = "ALWAYS"
    FIRST_MATCH = "FIRST_MATCH"
    ON_NTH_MATCH = "ON_NTH_MATCH"
    AFTER_N_MATCHES = "AFTER_N_MATCHES"
    UNTIL_MAX_HITS = "UNTIL_MAX_HITS"


class FaultScope(str, Enum):
    GLOBAL_TEST_SCOPE = "GLOBAL_TEST_SCOPE"
    RUN_SCOPE = "RUN_SCOPE"
    STEP_SCOPE = "STEP_SCOPE"
    INVOCATION_SCOPE = "INVOCATION_SCOPE"
    ATTEMPT_SCOPE = "ATTEMPT_SCOPE"
    COMPONENT_SCOPE = "COMPONENT_SCOPE"


class InjectedFaultCode(str, Enum):
    INJECTED_TRANSIENT_FAILURE = "INJECTED_TRANSIENT_FAILURE"
    INJECTED_PERMANENT_FAILURE = "INJECTED_PERMANENT_FAILURE"
    INJECTED_TIMEOUT = "INJECTED_TIMEOUT"
    INJECTED_RATE_LIMIT = "INJECTED_RATE_LIMIT"
    INJECTED_JOURNAL_FAILURE = "INJECTED_JOURNAL_FAILURE"
    INJECTED_STORE_FAILURE = "INJECTED_STORE_FAILURE"
    INJECTED_ENCODING_FAILURE = "INJECTED_ENCODING_FAILURE"
    INJECTED_COMPONENT_CLOSE_FAILURE = "INJECTED_COMPONENT_CLOSE_FAILURE"


class FaultConfigurationCode(str, Enum):
    BLOCKER_REQUIRED = "FAULT_BLOCKER_REQUIRED"
    FIXTURE_MUTATOR_REQUIRED = "FAULT_FIXTURE_MUTATOR_REQUIRED"


class InjectedFaultError(RuntimeError):
    """Safe exception containing only a fixed injected-fault code."""

    def __init__(self, code: InjectedFaultCode) -> None:
        if not isinstance(code, InjectedFaultCode):
            raise TypeError("code must be InjectedFaultCode")
        self.code = code
        super().__init__(code.value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code.value})"


class FaultExecutionConfigurationError(RuntimeError):
    """Fail-closed error for a missing explicitly injected test dependency."""

    def __init__(self, code: FaultConfigurationCode) -> None:
        if not isinstance(code, FaultConfigurationCode):
            raise TypeError("code must be FaultConfigurationCode")
        self.code = code
        super().__init__(code.value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code.value})"


@dataclass(frozen=True, slots=True)
class InjectedFailureResult:
    code: InjectedFaultCode
    injected: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.code, InjectedFaultCode):
            raise TypeError("code must be InjectedFaultCode")
        if self.injected is not True:
            raise ValueError("injected must be true")


def _require_safe_token(
    value: str | None,
    field_name: str,
    *,
    required: bool = False,
) -> None:
    if value is None:
        if required:
            raise ValueError(f"{field_name} is required")
        return
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a safe token")
    if (
        len(value) > MAX_SAFE_TOKEN_LENGTH
        or _SAFE_TOKEN.fullmatch(value) is None
        or "secret" in value.casefold()
    ):
        raise ValueError(f"{field_name} must be a safe token")


def _require_digest(value: str | None, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_positive_int(value: int | None, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _require_utc(value: datetime, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{field_name} must be a timezone-aware UTC datetime")


@dataclass(frozen=True, slots=True)
class FaultMatchContext:
    fault_point: FaultPoint
    component: str | None = None
    run_id_digest: str | None = None
    step_id: str | None = None
    invocation_id_digest: str | None = None
    attempt_number: int | None = None
    runtime_mode: str | None = None
    event_type: str | None = None
    operation_kind: str | None = None
    side_effect_phase: str | None = None
    checkpoint_kind: str | None = None
    shutdown_component: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fault_point, FaultPoint):
            raise TypeError("fault_point must be FaultPoint")
        _require_digest(self.run_id_digest, "run_id_digest")
        _require_digest(self.invocation_id_digest, "invocation_id_digest")
        for value, name in (
            (self.component, "component"),
            (self.step_id, "step_id"),
            (self.runtime_mode, "runtime_mode"),
            (self.event_type, "event_type"),
            (self.operation_kind, "operation_kind"),
            (self.side_effect_phase, "side_effect_phase"),
            (self.checkpoint_kind, "checkpoint_kind"),
            (self.shutdown_component, "shutdown_component"),
        ):
            _require_safe_token(value, name)
        if self.attempt_number is not None:
            _require_positive_int(self.attempt_number, "attempt_number")


_MATCH_FIELDS: Final[tuple[str, ...]] = (
    "run_id_digest",
    "step_id",
    "invocation_id_digest",
    "attempt_number",
    "component",
    "event_type",
    "operation_kind",
    "side_effect_phase",
    "shutdown_component",
)

_DANGEROUS_FAULT_POINTS: Final[frozenset[FaultPoint]] = frozenset(
    {FaultPoint.TOOL_AFTER_SIDE_EFFECT_COMMIT}
)


@dataclass(frozen=True, slots=True, repr=False)
class FaultRule:
    rule_id: str
    fault_point: FaultPoint
    action: FaultAction
    trigger: FaultTrigger
    scope: FaultScope
    max_hits: int
    match_number: int | None = None
    run_id_digest: str | None = None
    step_id: str | None = None
    invocation_id_digest: str | None = None
    attempt_number: int | None = None
    component: str | None = None
    event_type: str | None = None
    operation_kind: str | None = None
    side_effect_phase: str | None = None
    shutdown_component: str | None = None
    safe_fault_code: InjectedFaultCode | None = None
    delay_seconds: float | None = None
    fixture_mutation: str | None = None
    enabled: bool = True
    dangerous_window: bool = False

    def __post_init__(self) -> None:
        _require_safe_token(self.rule_id, "rule_id", required=True)
        for value, kind, name in (
            (self.fault_point, FaultPoint, "fault_point"),
            (self.action, FaultAction, "action"),
            (self.trigger, FaultTrigger, "trigger"),
            (self.scope, FaultScope, "scope"),
        ):
            if not isinstance(value, kind):
                raise TypeError(f"{name} has an invalid enum value")
        _require_positive_int(self.max_hits, "max_hits")
        if self.match_number is not None:
            _require_positive_int(self.match_number, "match_number")
        if (
            self.trigger
            in {
                FaultTrigger.ON_NTH_MATCH,
                FaultTrigger.AFTER_N_MATCHES,
            }
            and self.match_number is None
        ):
            raise ValueError(f"{self.trigger.value} requires match_number")
        _require_digest(self.run_id_digest, "run_id_digest")
        _require_digest(self.invocation_id_digest, "invocation_id_digest")
        for value, name in (
            (self.step_id, "step_id"),
            (self.component, "component"),
            (self.event_type, "event_type"),
            (self.operation_kind, "operation_kind"),
            (self.side_effect_phase, "side_effect_phase"),
            (self.shutdown_component, "shutdown_component"),
        ):
            _require_safe_token(value, name)
        if self.attempt_number is not None:
            _require_positive_int(self.attempt_number, "attempt_number")
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be bool")
        if not isinstance(self.dangerous_window, bool):
            raise TypeError("dangerous_window must be bool")
        if self.fault_point in _DANGEROUS_FAULT_POINTS and not self.dangerous_window:
            raise ValueError("dangerous fault point requires dangerous_window=true")
        self._validate_action_parameters()

    def _validate_action_parameters(self) -> None:
        if self.safe_fault_code is not None and not isinstance(
            self.safe_fault_code, InjectedFaultCode
        ):
            raise TypeError("safe_fault_code must be InjectedFaultCode")
        if (
            self.action
            in {
                FaultAction.RAISE_TYPED_ERROR,
                FaultAction.RETURN_TYPED_FAILURE,
            }
            and self.safe_fault_code is None
        ):
            raise ValueError(f"{self.action.value} requires safe_fault_code")
        if self.action is FaultAction.DELAY:
            if (
                isinstance(self.delay_seconds, bool)
                or not isinstance(self.delay_seconds, (int, float))
                or not math.isfinite(float(self.delay_seconds))
                or self.delay_seconds < 0
            ):
                raise ValueError("delay_seconds must be a finite non-negative number")
        elif self.delay_seconds is not None:
            raise ValueError("delay_seconds is only valid for DELAY")
        if self.action is FaultAction.CORRUPT_TEST_FIXTURE:
            _require_safe_token(
                self.fixture_mutation,
                "fixture_mutation",
                required=True,
            )
        elif self.fixture_mutation is not None:
            raise ValueError("fixture_mutation is only valid for CORRUPT_TEST_FIXTURE")

    def matches(self, context: FaultMatchContext) -> bool:
        if self.fault_point is not context.fault_point:
            return False
        return all(
            expected is None or expected == getattr(context, name)
            for name in _MATCH_FIELDS
            if (expected := getattr(self, name)) is not None
        )

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "fault_point": self.fault_point.value,
            "action": self.action.value,
            "trigger": self.trigger.value,
            "scope": self.scope.value,
            "max_hits": self.max_hits,
            "match_number": self.match_number,
            "matches": {
                name: getattr(self, name)
                for name in _MATCH_FIELDS
                if getattr(self, name) is not None
            },
            "safe_fault_code": (
                self.safe_fault_code.value if self.safe_fault_code else None
            ),
            "delay_seconds": self.delay_seconds,
            "fixture_mutation": self.fixture_mutation,
            "enabled": self.enabled,
            "dangerous_window": self.dangerous_window,
        }

    def __repr__(self) -> str:
        return (
            "FaultRule("
            f"rule_id={self.rule_id}, "
            f"fault_point={self.fault_point.value}, "
            f"action={self.action.value}, "
            f"trigger={self.trigger.value}, "
            f"scope={self.scope.value}, "
            f"max_hits={self.max_hits}, "
            f"enabled={self.enabled}, "
            f"dangerous_window={self.dangerous_window}"
            ")"
        )


@dataclass(frozen=True, slots=True, repr=False)
class FaultPlan:
    plan_id: str
    rules: tuple[FaultRule, ...]
    schema_version: int = FAULT_PLAN_SCHEMA_VERSION
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _require_safe_token(self.plan_id, "plan_id", required=True)
        _require_positive_int(self.schema_version, "schema_version")
        _require_utc(self.created_at, "created_at")
        if not isinstance(self.rules, tuple):
            object.__setattr__(self, "rules", tuple(self.rules))
        if any(not isinstance(rule, FaultRule) for rule in self.rules):
            raise TypeError("rules must contain only FaultRule")
        normalized = tuple(sorted(self.rules, key=lambda rule: rule.rule_id))
        identifiers = tuple(rule.rule_id for rule in normalized)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("rule_id values must be unique")
        object.__setattr__(self, "rules", normalized)

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "schema_version": self.schema_version,
            "created_at": self.created_at.isoformat(),
            "rules": [rule.to_safe_dict() for rule in self.rules],
        }

    def to_safe_json(self) -> str:
        return json.dumps(
            self.to_safe_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_safe_json().encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        return (
            "FaultPlan("
            f"plan_id={self.plan_id}, "
            f"schema_version={self.schema_version}, "
            f"rule_count={len(self.rules)}, "
            f"digest={self.digest}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class FaultDecision:
    matched: bool
    rule_id: str | None = None
    fault_point: FaultPoint | None = None
    action: FaultAction | None = None
    match_ordinal: int | None = None
    hit_ordinal: int | None = None
    safe_fault_code: InjectedFaultCode | None = None
    triggered_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.matched, bool):
            raise TypeError("matched must be bool")
        fields = (
            self.rule_id,
            self.fault_point,
            self.action,
            self.match_ordinal,
            self.hit_ordinal,
            self.triggered_at,
        )
        if not self.matched:
            if any(value is not None for value in fields) or (
                self.safe_fault_code is not None
            ):
                raise ValueError("NO_FAULT decision cannot contain rule data")
            return
        _require_safe_token(self.rule_id, "rule_id", required=True)
        if not isinstance(self.fault_point, FaultPoint):
            raise TypeError("fault_point must be FaultPoint")
        if not isinstance(self.action, FaultAction):
            raise TypeError("action must be FaultAction")
        _require_positive_int(self.match_ordinal, "match_ordinal")
        _require_positive_int(self.hit_ordinal, "hit_ordinal")
        if self.safe_fault_code is not None and not isinstance(
            self.safe_fault_code, InjectedFaultCode
        ):
            raise TypeError("safe_fault_code must be InjectedFaultCode")
        _require_utc(self.triggered_at, "triggered_at")


NO_FAULT_DECISION: Final[FaultDecision] = FaultDecision(matched=False)


__all__ = [
    "FAULT_PLAN_SCHEMA_VERSION",
    "FaultAction",
    "FaultConfigurationCode",
    "FaultDecision",
    "FaultExecutionConfigurationError",
    "FaultMatchContext",
    "FaultPlan",
    "FaultPoint",
    "FaultRule",
    "FaultScope",
    "FaultTrigger",
    "InjectedFailureResult",
    "InjectedFaultCode",
    "InjectedFaultError",
    "MAX_SAFE_TOKEN_LENGTH",
    "NO_FAULT_DECISION",
]
