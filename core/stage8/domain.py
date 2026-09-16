"""Stage8 业务对象；不承载 AgentCore Runtime 状态。"""

from dataclasses import dataclass
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
    subject_version: int | None
    subject_digest: str | None
    status: ReviewStatus
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_comment: str | None = None
