"""Stage8 业务对象；不承载 AgentCore Runtime 状态。"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class MissionStatus(StrEnum):
    CREATED = "CREATED"
    CONTEXT_READY = "CONTEXT_READY"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    READY_FOR_EXECUTION = "READY_FOR_EXECUTION"
    WAITING_FOR_RESOURCE = "WAITING_FOR_RESOURCE"
    EXECUTING = "EXECUTING"
    TRIAGING = "TRIAGING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


ALLOWED_TRANSITIONS = {
    MissionStatus.CREATED: {MissionStatus.CONTEXT_READY, MissionStatus.CANCELLED},
    MissionStatus.CONTEXT_READY: {MissionStatus.AWAITING_REVIEW, MissionStatus.CANCELLED},
    MissionStatus.AWAITING_REVIEW: {MissionStatus.READY_FOR_EXECUTION, MissionStatus.CONTEXT_READY, MissionStatus.CANCELLED},
    MissionStatus.READY_FOR_EXECUTION: {MissionStatus.EXECUTING, MissionStatus.WAITING_FOR_RESOURCE, MissionStatus.CANCELLED},
    MissionStatus.WAITING_FOR_RESOURCE: {MissionStatus.READY_FOR_EXECUTION, MissionStatus.CANCELLED},
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
class EnvironmentRequirements:
    """TestPlan 可表达的最小、结构化环境要求。"""

    version: str | None = None
    network_type: str | None = None
    hardware_type: str | None = None
    required_capabilities: tuple[str, ...] = ()
    feature_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("version", "network_type", "hardware_type"):
            value = getattr(self, name)
            if value is not None:
                normalized = value.strip()
                if not normalized or len(normalized) > 128:
                    raise ValueError(f"{name} must be 1..128 characters")
                object.__setattr__(self, name, normalized)
        for name in ("required_capabilities", "feature_flags"):
            normalized = tuple(dict.fromkeys(item.strip() for item in getattr(self, name)))
            if any(not item or len(item) > 128 for item in normalized) or len(normalized) > 32:
                raise ValueError(f"{name} must contain at most 32 non-empty values")
            object.__setattr__(self, name, normalized)

    @property
    def is_empty(self) -> bool:
        return not any((
            self.version,
            self.network_type,
            self.hardware_type,
            self.required_capabilities,
            self.feature_flags,
        ))


@dataclass(frozen=True, slots=True)
class ResourceUnavailable:
    mission_id: str
    status: str
    required_capabilities: tuple[str, ...]
    matched_but_busy_count: int
    no_match_reason: str


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    plan_id: str
    mission_id: str
    mission_version: int
    test_plan_subject_id: str
    test_plan_version: int
    test_plan_digest: str
    generated_case_artifact_id: str
    provider_case_id: str
    case_path: str
    environment_id: str
    environment_ip: str
    execution_list_ref: str
    execution_request_digest: str
    executor_id: str = "EXECUTOR-001"
    environment_requirements: EnvironmentRequirements = field(default_factory=EnvironmentRequirements)
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
    error_code: str | None = None
    error_message: str | None = None
    failed_step: str | None = None
    result_location: str | None = None
    log_excerpt: str | None = None


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
