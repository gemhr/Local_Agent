#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Safe facts retained for Day 24B tool-completion recovery decisions."""

from __future__ import annotations

from dataclasses import dataclass


_SIDE_EFFECT_STATES = frozenset(
    {"NOT_STARTED", "STARTED", "COMMITTED", "COMPENSATED", "UNKNOWN"}
)
_RETRY_DISPOSITIONS = frozenset(
    {"SAFE", "SAFE_WITH_IDEMPOTENCY_KEY", "UNSAFE", "OUTCOME_UNKNOWN"}
)
_OUTCOME_CLASSIFICATIONS = frozenset(
    {
        "NOT_STARTED",
        "SUCCEEDED",
        "PARTIALLY_SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
        "VALIDATION",
        "NOT_FOUND",
        "PERMISSION_DENIED",
        "RESOURCE_CONFLICT",
        "TRANSIENT",
        "POST_COMMIT_RESPONSE_FAILURE",
        "TIMEOUT",
        "DEADLINE_EXCEEDED",
        "BUDGET_EXHAUSTED",
        "OUTPUT_INVALID",
        "OUTPUT_TOO_LARGE",
        "SIDE_EFFECT_UNKNOWN",
        "COMPENSATION_FAILED",
        "INTERNAL",
        "OUTCOME_UNKNOWN",
        "EVIDENCE_LOST",
        "STARTED_EVENT_CORRUPTED",
    }
)


@dataclass(frozen=True, slots=True)
class ToolCompletionGapFixture:
    """Text-free evidence input; it never validates recovery or reruns work."""

    started_event_present: bool
    completed_event_present: bool
    run_terminal_present: bool
    local_completion_evidence_present: bool
    provider_started: bool | None
    side_effect_state: str
    retry_disposition: str
    outcome_classification: str
    started_event_valid: bool = True

    def __post_init__(self) -> None:
        for name in (
            "started_event_present",
            "completed_event_present",
            "run_terminal_present",
            "local_completion_evidence_present",
            "started_event_valid",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")
        if (
            self.provider_started is not None
            and type(self.provider_started) is not bool
        ):
            raise TypeError("provider_started must be bool or None")
        if self.side_effect_state not in _SIDE_EFFECT_STATES:
            raise ValueError("unsupported side_effect_state")
        if self.retry_disposition not in _RETRY_DISPOSITIONS:
            raise ValueError("unsupported retry_disposition")
        if self.outcome_classification not in _OUTCOME_CLASSIFICATIONS:
            raise ValueError("unsupported outcome_classification")
        if not self.started_event_present and self.started_event_valid:
            raise ValueError("a missing started event cannot be valid")
        if not self.local_completion_evidence_present:
            if self.provider_started is not None:
                raise ValueError("lost local evidence requires provider_started=None")
            if self.side_effect_state != "UNKNOWN":
                raise ValueError("lost local evidence requires side_effect_state=UNKNOWN")
            if self.retry_disposition != "OUTCOME_UNKNOWN":
                raise ValueError(
                    "lost local evidence requires retry_disposition=OUTCOME_UNKNOWN"
                )
        if self.completed_event_present:
            raise ValueError("a completion-gap fixture cannot contain TOOL_COMPLETED")


__all__ = ["ToolCompletionGapFixture"]
