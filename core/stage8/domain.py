"""Stage8 业务对象；不承载 AgentCore Runtime 状态。"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class MissionStatus(StrEnum):
    CREATED = "CREATED"
    CONTEXT_READY = "CONTEXT_READY"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    READY_FOR_EXECUTION = "READY_FOR_EXECUTION"
    EXECUTING = "EXECUTING"
    TRIAGING = "TRIAGING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


ALLOWED_TRANSITIONS = {
    MissionStatus.CREATED: {MissionStatus.CONTEXT_READY, MissionStatus.CANCELLED},
    MissionStatus.CONTEXT_READY: {MissionStatus.AWAITING_REVIEW, MissionStatus.CANCELLED},
    MissionStatus.AWAITING_REVIEW: {MissionStatus.READY_FOR_EXECUTION, MissionStatus.CONTEXT_READY, MissionStatus.CANCELLED},
    MissionStatus.READY_FOR_EXECUTION: {MissionStatus.EXECUTING, MissionStatus.CANCELLED},
    MissionStatus.EXECUTING: {MissionStatus.TRIAGING, MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED},
    MissionStatus.TRIAGING: {MissionStatus.EXECUTING, MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED},
    MissionStatus.COMPLETED: set(), MissionStatus.CANCELLED: set(), MissionStatus.FAILED: set(),
}


class ReviewStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ReviewType(StrEnum):
    TEST_PLAN = "TEST_PLAN"
    CASE_DESIGN = "CASE_DESIGN"
    EXPECTED_RESULT = "EXPECTED_RESULT"
    REQUIREMENT_MAPPING = "REQUIREMENT_MAPPING"


class ExternalExecutionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class FeatureTestMission:
    mission_id: str
    feature_id: str
    status: MissionStatus
    version: int
    created_at: datetime
    updated_at: datetime
    title: str | None = None
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class MissionRunReference:
    mission_id: str
    run_id: str
    run_purpose: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BusinessReview:
    review_id: str
    mission_id: str
    review_type: ReviewType
    subject_id: str | None
    subject_version: int | None
    subject_digest: str | None
    status: ReviewStatus
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_comment: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedCaseArtifact:
    artifact_id: str
    mission_id: str
    test_plan_subject_id: str
    test_plan_version: int
    test_plan_digest: str
    scenario_id: str
    provider_case_id: str
    case_path: str
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    plan_id: str
    mission_id: str
    mission_version: int
    test_plan_subject_id: str
    test_plan_version: int
    test_plan_digest: str
    case_id: str
    environment_id: str
    executor_id: str
    parameters: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExternalExecutionJob:
    job_id: str
    mission_id: str
    execution_id: str | None
    status: ExternalExecutionStatus
    version: int
    case_id: str
    environment_id: str
    executor_id: str
    plan_id: str
    attempt_no: int
    auto_repair_count: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict | None = None
    triage: dict | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    execution_id: str
    status: ExternalExecutionStatus
    actual_result: str
    expected_result: str | None = None
    failure_signature: str | None = None
    logs: list[str] = field(default_factory=list)
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FailureEvidencePackage:
    mission_id: str
    execution_id: str
    feature_id: str
    test_plan_subject_id: str
    test_plan_version: int
    test_plan_digest: str
    case_id: str
    environment_id: str
    executor_id: str
    expected_result: str | None
    actual_result: str
    logs: list[str]
    failure_signature: str | None
    evidence: dict[str, dict]
