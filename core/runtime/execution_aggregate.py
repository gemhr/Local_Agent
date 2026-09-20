"""Typed contracts for the Stage10 canonical execution aggregate.

This module deliberately contains no scheduler or provider behavior.  It owns
only stable payload conversion and digest rules shared by persistence callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from core.runtime.plan_fingerprint import PlanFingerprinter
from core.runtime.planning import ExecutionKind, OutputPolicy, Plan, PlanSource, PlanStep, PlanValidator, RiskLevel, TaskCapabilityRequirements
from core.runtime.snapshot_contract import PlanSnapshot
from core.runtime.snapshot_serialization import require_utc, sha256_digest, to_primitive


class ExecutionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"


class StepExecutionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


class ModelAttemptState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    UNKNOWN = "UNKNOWN"


def canonical_payload_digest(payload: object) -> str:
    """Return the project canonical JSON SHA-256; never use ``repr``."""
    return sha256_digest(payload)


def plan_payload(plan: Plan) -> dict[str, Any]:
    # Execution rehydration needs the complete immutable Plan, not the
    # payload-safe Snapshot (which intentionally stores only text digests).
    PlanSnapshot.from_plan(plan)  # retain the existing validation/fingerprint contract
    return to_primitive(plan)


def plan_digest(plan: Plan) -> str:
    return PlanFingerprinter.fingerprint(plan)


def plan_from_payload(payload: Mapping[str, Any]) -> Plan:
    """Rebuild an immutable Plan from the complete durable plan payload."""
    if not isinstance(payload, Mapping):
        raise TypeError("plan payload must be a mapping")
    steps = []
    if not isinstance(payload.get("steps"), list) or not payload["steps"]:
        raise ValueError("plan payload must contain non-empty steps")
    for item in payload["steps"]:
        if not isinstance(item, Mapping):
            raise TypeError("plan step payload must be a mapping")
        requirements = item["capability_requirements"]
        steps.append(PlanStep(
            step_id=item["step_id"], title=item["title"], description=item["description"],
            depends_on=tuple(item["depends_on"]), completion_criteria=item["completion_criteria"],
            preferred_agent=item["preferred_agent"],
            capability_requirements=TaskCapabilityRequirements(
                requires_planning=requirements["requires_planning"], requires_tools=requirements["requires_tools"],
                requires_rag=requirements["requires_rag"], requires_multi_agent=requirements["requires_multi_agent"],
                requires_code_reasoning=requirements["requires_code_reasoning"], requires_structured_output=requirements["requires_structured_output"],
                requires_long_reasoning=requirements["requires_long_reasoning"], risk_level=RiskLevel(requirements["risk_level"]),
                estimated_steps=requirements["estimated_steps"],
            ),
            execution_kind=ExecutionKind(item["execution_kind"]), output_policy=OutputPolicy(item["output_policy"]),
        ))
    plan = Plan(
        plan_id=payload["plan_id"], version=payload["version"], task_summary=payload["task_summary"],
        steps=tuple(steps), created_at=datetime.fromisoformat(payload["created_at"]),
        source=PlanSource(payload["source"]),
    )
    PlanValidator.validate(plan)
    return plan


@dataclass(frozen=True, slots=True)
class ExecutionRootInput:
    run_id: str
    resume_input: Mapping[str, Any]
    plan: Plan
    absolute_deadline: datetime | None
    budget_totals: Mapping[str, Any]
    budget_reserved: Mapping[str, Any]
    budget_consumed: Mapping[str, Any]
    recovery_supported: bool = True

    def as_row_values(self) -> dict[str, Any]:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id must be non-empty")
        if not isinstance(self.resume_input, Mapping):
            raise TypeError("resume_input must be a mapping")
        if not isinstance(self.budget_totals, Mapping) or not isinstance(self.budget_reserved, Mapping) or not isinstance(self.budget_consumed, Mapping):
            raise TypeError("budget snapshots must be mappings")
        if self.absolute_deadline is not None:
            require_utc(self.absolute_deadline, "absolute_deadline")
        if type(self.recovery_supported) is not bool:
            raise TypeError("recovery_supported must be bool")
        return {
            "run_id": self.run_id,
            "schema_version": 1,
            "execution_version": 1,
            "resume_input": to_primitive(dict(self.resume_input)),
            "plan_payload": plan_payload(self.plan),
            "plan_fingerprint": plan_digest(self.plan),
            "plan_version": self.plan.version,
            "absolute_deadline": self.absolute_deadline.astimezone(timezone.utc) if self.absolute_deadline is not None else None,
            "budget_totals": to_primitive(dict(self.budget_totals)),
            "budget_reserved": to_primitive(dict(self.budget_reserved)),
            "budget_consumed": to_primitive(dict(self.budget_consumed)),
            "recovery_supported": self.recovery_supported,
        }


__all__ = [
    "ExecutionRootInput",
    "ExecutionStatus",
    "ModelAttemptState",
    "StepExecutionStatus",
    "canonical_payload_digest",
    "plan_digest",
    "plan_from_payload",
    "plan_payload",
]
