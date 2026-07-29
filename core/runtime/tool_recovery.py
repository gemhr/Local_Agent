#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pure, metadata-only Tool recovery decisions for day 22."""

from __future__ import annotations

from collections import defaultdict

from core.runtime.recovery_contract import (
    RecoveryProjection,
    ToolRecoveryDecision,
    ToolRecoveryDecisionStatus,
    ToolRecoveryEvidence,
)
from core.runtime.state import StepStatus


class ToolRecoveryDecisionEngine:
    """Classify persisted evidence without invoking or mutating runtime state."""

    @classmethod
    def decide(
        cls,
        evidence: tuple[ToolRecoveryEvidence, ...],
        projection: RecoveryProjection,
    ) -> tuple[ToolRecoveryDecision, ...]:
        if not isinstance(evidence, tuple):
            raise TypeError("evidence must be a tuple")
        if not isinstance(projection, RecoveryProjection):
            raise TypeError("projection must be RecoveryProjection")

        grouped: dict[str, list[ToolRecoveryEvidence]] = defaultdict(list)
        unidentified: list[ToolRecoveryEvidence] = []
        for item in sorted(evidence, key=lambda value: value.sequence):
            if not isinstance(item, ToolRecoveryEvidence):
                raise TypeError("evidence must contain ToolRecoveryEvidence")
            if item.invocation_identity_digest is None:
                unidentified.append(item)
            else:
                grouped[item.invocation_identity_digest].append(item)

        decisions = [
            cls._decide_group(tuple(items), projection)
            for _, items in sorted(
                grouped.items(), key=lambda pair: pair[1][0].sequence
            )
        ]
        decisions.extend(
            cls._insufficient((item,), "INVOCATION_IDENTITY_MISSING")
            for item in unidentified
        )
        return tuple(decisions)

    @classmethod
    def _decide_group(
        cls,
        items: tuple[ToolRecoveryEvidence, ...],
        projection: RecoveryProjection,
    ) -> ToolRecoveryDecision:
        names = {item.tool_name for item in items}
        if len(names) != 1:
            return cls._manual(items, "INVOCATION_TOOL_NAME_CONFLICT")
        if any(item.attempt_identity_digest is None for item in items):
            return cls._insufficient(items, "ATTEMPT_IDENTITY_MISSING")

        attempts: dict[str, list[ToolRecoveryEvidence]] = defaultdict(list)
        for item in items:
            assert item.attempt_identity_digest is not None
            attempts[item.attempt_identity_digest].append(item)

        for attempt_items in attempts.values():
            starts = [
                item for item in attempt_items if item.event_kind == "STARTED"
            ]
            completions = [
                item
                for item in attempt_items
                if item.event_kind == "COMPLETED"
            ]
            if len(starts) != 1 or len(completions) > 1:
                return cls._manual(items, "ATTEMPT_EVENT_PAIRING_INVALID")

        latest = max(items, key=lambda item: item.sequence)
        latest_attempt = max(
            attempts.values(),
            key=lambda values: min(item.sequence for item in values),
        )
        completed = [
            item
            for item in latest_attempt
            if item.event_kind == "COMPLETED"
        ]
        authoritative_success = any(
            item.succeeded is True
            and item.step_id is not None
            and item.step_id in projection.step_states
            and projection.step_states[item.step_id].status
            == StepStatus.SUCCEEDED.value
            for item in completed
        )

        if any(item.compensation_state == "FAILED" for item in items):
            return cls._manual(items, "COMPENSATION_FAILED")
        if any(
            item.retry_disposition == "OUTCOME_UNKNOWN"
            or item.outcome_classification
            in {"OUTCOME_UNKNOWN", "POST_COMMIT_RESPONSE_FAILURE"}
            or item.side_effect_state == "UNKNOWN"
            or (
                item.execution_detached and not item.worker_terminated
            )
            for item in items
        ):
            return cls._manual(items, "OUTCOME_UNKNOWN")

        if authoritative_success:
            if (
                latest.idempotency_kind == "NON_IDEMPOTENT"
                and latest.side_effect_state == "COMMITTED"
            ):
                return cls._decision(
                    items,
                    ToolRecoveryDecisionStatus.DO_NOT_RETRY,
                    "NON_IDEMPOTENT_COMMITTED_AND_COMPLETED",
                )
            return cls._decision(
                items,
                ToolRecoveryDecisionStatus.NO_ACTION_REQUIRED,
                "INVOCATION_AUTHORITATIVELY_COMPLETED",
            )

        if any(
            item.tool_evidence_schema_version is None
            or item.side_effect_kind is None
            or item.idempotency_kind is None
            for item in items
        ):
            return cls._insufficient(items, "HISTORICAL_EVIDENCE_INCOMPLETE")

        if (
            latest.idempotency_kind == "NON_IDEMPOTENT"
            and latest.side_effect_state == "COMMITTED"
        ):
            return cls._manual(
                items, "NON_IDEMPOTENT_COMMITTED_WITHOUT_COMPLETION"
            )
        if latest.side_effect_kind == "NONE" and latest.side_effect_state not in {
            "COMMITTED",
            "UNKNOWN",
        }:
            return cls._decision(
                items,
                ToolRecoveryDecisionStatus.SAFE_RETRY_CANDIDATE,
                "NO_SIDE_EFFECT_AND_INVOCATION_INCOMPLETE",
            )
        if (
            latest.idempotency_kind == "IDEMPOTENT_WITH_KEY"
            and latest.idempotency_key_digest is not None
            and latest.replay_supported is True
        ):
            return cls._decision(
                items,
                ToolRecoveryDecisionStatus.SAFE_RETRY_CANDIDATE,
                "IDEMPOTENT_KEYED_RETRY_CANDIDATE",
            )
        return cls._manual(items, "INCOMPLETE_INVOCATION_NOT_SAFE_TO_RETRY")

    @classmethod
    def _manual(
        cls, items: tuple[ToolRecoveryEvidence, ...], reason: str
    ) -> ToolRecoveryDecision:
        return cls._decision(
            items, ToolRecoveryDecisionStatus.MANUAL_RECONCILIATION, reason
        )

    @classmethod
    def _insufficient(
        cls, items: tuple[ToolRecoveryEvidence, ...], reason: str
    ) -> ToolRecoveryDecision:
        return cls._decision(
            items, ToolRecoveryDecisionStatus.INSUFFICIENT_EVIDENCE, reason
        )

    @staticmethod
    def _decision(
        items: tuple[ToolRecoveryEvidence, ...],
        status: ToolRecoveryDecisionStatus,
        reason: str,
    ) -> ToolRecoveryDecision:
        latest = max(items, key=lambda item: item.sequence)
        sequences = tuple(
            sorted(
                {
                    item.attempt_sequence
                    if item.attempt_sequence is not None
                    else item.sequence
                    for item in items
                    if item.event_kind == "STARTED"
                }
                or {item.sequence for item in items}
            )
        )
        return ToolRecoveryDecision(
            invocation_identity_digest=latest.invocation_identity_digest,
            tool_name=latest.tool_name,
            status=status,
            reasons=(reason,),
            attempt_sequences=sequences,
            side_effect_kind=latest.side_effect_kind,
            idempotency_kind=latest.idempotency_kind,
            side_effect_state=latest.side_effect_state,
            compensation_state=latest.compensation_state,
            replay_supported=latest.replay_supported,
            idempotency_key_available=(
                latest.idempotency_key_digest is not None
            ),
            execution_detached=latest.execution_detached,
            worker_terminated=latest.worker_terminated,
            retry_candidate=(
                status
                is ToolRecoveryDecisionStatus.SAFE_RETRY_CANDIDATE
            ),
            automatic_action_allowed=False,
        )


__all__ = ["ToolRecoveryDecisionEngine"]
