"""窄业务请求和严格诊断 DTO。"""
from typing import Literal
from datetime import date, datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from core.ci_simulation.service import CIError


class StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class NightlyEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment_id: Literal["env-parser-a", "env-parser-b", "env-config", "env-input", "env-buffer", "env-evidence"]
    profile_key: Literal["parser", "configuration", "input", "buffer", "missing"]
    owner_user_id: UUID
    duration_profile: Literal["normal", "slow"] = "normal"


class CreateNightly(Intent):
    business_date: date
    plan_key: Literal["small-nightly"] = "small-nightly"
    environments: list[NightlyEnvironment] = Field(min_length=4, max_length=6)

    @model_validator(mode="after")
    def unique_environments(self):
        if len({e.environment_id for e in self.environments}) != len(self.environments):
            raise ValueError("CI_NIGHTLY_DUPLICATE_ENVIRONMENT")
        if len({e.owner_user_id for e in self.environments}) != 2:
            raise ValueError("CI_NIGHTLY_TWO_OWNERS_REQUIRED")
        return self


class NightlyClock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_business_time: datetime

    @field_validator("target_business_time")
    @classmethod
    def explicit_offset(cls, value):
        if value.utcoffset() is None:
            raise ValueError("CI_NIGHTLY_OFFSET_REQUIRED")
        return value


class NightlyStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_execution_id: str = Field(min_length=1, max_length=100)
    kind: Literal["START", "COMPLETE_CASE"]
    case_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def concrete_target(self):
        if (self.kind == "COMPLETE_CASE") != bool(self.case_id):
            raise ValueError("CI_NIGHTLY_STEP_TARGET_REQUIRED")
        return self


class AdvanceNightly(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[NightlyStep] = Field(min_length=1, max_length=6)


class GroupDecision(Intent):
    expected_group_revision: int = Field(ge=1, strict=True)
    kind: Literal["CONFIRM", "EXCLUDE", "SPLIT"]
    incident_ids: list[str] = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def valid_selection(self):
        if not self.reason.strip() or len(set(self.incident_ids)) != len(self.incident_ids):
            raise ValueError("CI_GROUP_SELECTION_INVALID")
        return self


class CreateScenario(Intent):
    scenario_key: Literal["report_pipeline_failure", "limited_diagnostics", "environment_configuration_failure", "stale_case_input", "buffer_behavior_change"]


class ExecutionIntent(Intent):
    case_id: str | None = None
    expected_state_revision: int | None = Field(default=None, strict=True, ge=1)


class UpgradeParameters(StrictDTO):
    target_version: Literal["2.0"]


class FlagParameters(StrictDTO):
    compressed_transport: bool


class InputParameters(StrictDTO):
    target_revision: int


class DeliveryParameters(StrictDTO):
    target_version: Literal["product-2.1.1"]


class ActionIntent(Intent):
    action_kind: Literal["UPGRADE_PARSER_DECODER", "SET_FEATURE_FLAG", "REFRESH_CASE_INPUT", "DELIVER_PRODUCT_FIX"]
    target_case_id: str | None = None
    parameters: dict

    @model_validator(mode="after")
    def validate_parameters(self):
        types = dict(UPGRADE_PARSER_DECODER=UpgradeParameters, SET_FEATURE_FLAG=FlagParameters,
            REFRESH_CASE_INPUT=InputParameters, DELIVER_PRODUCT_FIX=DeliveryParameters)
        self.parameters = types[self.action_kind].model_validate(self.parameters).model_dump()
        return self


class ConfirmAction(StrictDTO):
    confirmed: bool
    preview_digest: str = Field(pattern="^[0-9a-f]{64}$")


class VerificationIntent(Intent):
    mode: Literal["SINGLE", "GROUP"]
    case_ids: list[str] = Field(min_length=1, max_length=2)
    expected_state_revision: int = Field(strict=True, ge=1)


class CloseIncident(Intent):
    expected_state_revision: int = Field(strict=True, ge=1)
    action_id: str
    single_verification_id: str
    group_verification_id: str
    root_cause: str = Field(min_length=1, max_length=1000)
    fix_confirmation: str = Field(min_length=1, max_length=1000)
    regression_confirmed: bool = Field(strict=True)

    @field_validator("root_cause", "fix_confirmation")
    @classmethod
    def nonempty_text(cls, value):
        if not value.strip():
            raise ValueError("人工确认文本不能为空")
        return value.strip()


class EvidenceReference(StrictDTO):
    evidence_id: str = Field(min_length=1, max_length=255)
    digest: str = Field(pattern="^[0-9a-f]{64}$")


class DiagnosisReport(StrictDTO):
    schema_version: Literal["ci-diagnosis.v1"]
    kind: Literal["REPORT"]
    incident_id: str
    diagnosis_status: Literal["SUPPORTED", "INSUFFICIENT_EVIDENCE"]
    failure_stage: Literal["PARSER", "COMPARISON", "AUTOMATION", "UNKNOWN"]
    root_cause_hypothesis: str = Field(min_length=1, max_length=500)
    supporting_evidence: list[EvidenceReference] = Field(max_length=6)
    uncertainties: list[str] = Field(max_length=4)
    recommended_checks: list[str] = Field(min_length=1, max_length=4)
    recommended_owner_role: Literal["TOOL_MAINTAINER", "TEST_OWNER", "UNKNOWN"]

    @model_validator(mode="after")
    def require_support(self):
        if self.diagnosis_status == "SUPPORTED" and not self.supporting_evidence:
            raise ValueError("缺少支持证据")
        if self.diagnosis_status == "INSUFFICIENT_EVIDENCE" and not self.uncertainties:
            raise ValueError("必须说明不确定性")
        return self


class EvidenceEntry(StrictDTO):
    evidence_id: str
    kind: str
    availability: Literal["AVAILABLE", "MISSING"]
    media_type: str
    digest: str | None


class EvidencePayload(EvidenceEntry):
    incident_id: str
    execution_id: str
    captured_at: str
    content: dict | str | None
    missing_reason: str | None


class ExecutionContext(StrictDTO):
    incident_id: str
    execution_id: str
    environment_id: str
    case_id: str
    outcome: Literal["PASS", "FAIL"]
    completed_at: str
    evidence: list[EvidenceEntry]
