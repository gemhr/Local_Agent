"""消费 WP00 C1/C3/C5/C9 的受控 Provider wire 合同。"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

SafeID = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$")
]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RemoteState = Literal["QUEUED", "RUNNING", "COMPLETED", "INFRA_FAILED", "CANCELLED"]
CaseOutcome = Literal["PASS", "FAILED", "ERROR", "SKIPPED"]
TERMINAL_STATES = frozenset({"COMPLETED", "INFRA_FAILED", "CANCELLED"})


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_bytes(value: object) -> bytes:
    """catalog-json-v1 的整数资产子集；不接受浮点值或孤立 surrogate。"""

    def validate(item):
        if item is None or isinstance(item, (bool, int)):
            return
        if isinstance(item, str):
            item.encode("utf-8", errors="strict")
            return
        if isinstance(item, (tuple, list)):
            for child in item:
                validate(child)
            return
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            for key, child in item.items():
                validate(key)
                validate(child)
            return
        raise ValueError("受控资产仅允许 JSON 整数、字符串、布尔、null、数组及对象")

    validate(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def business_key(domain: str, *parts) -> str:
    """WP00 §3：有序 compact JSON array 的完整 SHA256。"""

    def validate(item):
        if item is None or isinstance(item, str) or (type(item) is int):
            return
        if isinstance(item, (tuple, list)):
            for child in item:
                validate(child)
            return
        raise ValueError("业务 key 仅允许字符串、整数、null 及数组")

    values = ["stage13.v1", domain, *parts]
    validate(values)
    return sha256(canonical_bytes(values))


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkloadConfig(WireModel):
    provider_namespace_id: SafeID
    profile_id: Literal[
        "normal", "storm-10", "storm-30", "storm-50", "insufficient-evidence"
    ] = "normal"
    seed: Annotated[int, Field(strict=True, ge=0)] = 1301
    environment_count: Annotated[int, Field(strict=True, ge=100, le=3000)] = 3000
    channel_group_count: Annotated[int, Field(strict=True, ge=2, le=12)] = 12
    owner_scope_id: SafeID = "stage13-controlled"
    automation_project_id: SafeID = "automation-demo"
    suite_id: SafeID = "nightly-suite"
    business_date: str = "2026-10-06"
    product_versions: tuple[SafeID, SafeID, SafeID] = ("V1", "V2", "V3")
    generator_version: Literal["stage13.generator.v1"] = "stage13.generator.v1"

    @model_validator(mode="after")
    def check_config(self):
        if self.environment_count % 100:
            raise ValueError("受控 profile 环境数必须为 100 的倍数")
        if len(set(self.product_versions)) != 3:
            raise ValueError("版本计划需要三个不同版本")
        if date.fromisoformat(self.business_date).isoformat() != self.business_date:
            raise ValueError("业务日期必须为 YYYY-MM-DD")
        return self


class SubmitRequest(WireModel):
    provider_namespace_id: SafeID
    owner_scope_id: SafeID
    automation_project_id: SafeID
    suite_id: SafeID
    environment_id: SafeID
    channel_group: SafeID
    cycle_key: Digest
    version_execution_key: Digest
    ordinal: Annotated[int, Field(strict=True, ge=1, le=3)]
    product_version: SafeID
    remote_execution_business_key: Digest
    parameters: dict[SafeID, SafeID] = Field(default_factory=dict, max_length=16)
    request_digest: Digest

    def intent(self) -> dict:
        return self.model_dump(mode="json", exclude={"request_digest"})

    @model_validator(mode="after")
    def validate_binding(self):
        expected = business_key(
            "remote",
            self.provider_namespace_id,
            self.owner_scope_id,
            self.automation_project_id,
            self.suite_id,
            self.environment_id,
            self.cycle_key,
            self.ordinal,
            self.product_version,
        )
        if expected != self.remote_execution_business_key:
            raise ValueError("remote business key 与意图不匹配")
        if (
            business_key("version", self.cycle_key, self.ordinal, self.product_version)
            != self.version_execution_key
        ):
            raise ValueError("version key 与意图不匹配")
        if sha256(canonical_bytes(self.intent())) != self.request_digest:
            raise ValueError("request digest 与实际请求不匹配")
        return self


class SubmitReceipt(WireModel):
    provider_namespace_id: SafeID
    remote_execution_business_key: Digest
    request_digest: Digest
    remote_execution_id: UUID
    accepted_at: datetime
    status_revision: Annotated[int, Field(strict=True, ge=1)]


class SealReceipt(WireModel):
    provider_namespace_id: SafeID
    remote_execution_business_key: Digest
    request_digest: Digest
    sealed_at: datetime


class LookupRequest(WireModel):
    remote_execution_business_key: Digest
    request_digest: Digest


class RemoteRequest(LookupRequest):
    remote_execution_id: UUID


class LookupResult(WireModel):
    result: Literal["FOUND", "NOT_CREATED", "NOT_CREATED_FINAL"]
    receipt: SubmitReceipt | None = None
    seal_receipt: SealReceipt | None = None

    @model_validator(mode="after")
    def check_result(self):
        if (self.result == "FOUND") != (self.receipt is not None):
            raise ValueError("FOUND 必须携带原 submit receipt")
        if (self.result == "NOT_CREATED_FINAL") != (self.seal_receipt is not None):
            raise ValueError("NOT_CREATED_FINAL 必须携带 seal receipt")
        return self


class DetailRequest(RemoteRequest):
    offset: Annotated[int, Field(strict=True, ge=0, le=200)] = 0
    page_size: Annotated[int, Field(strict=True, ge=1, le=50)] = 50


class ArtifactRequest(RemoteRequest):
    artifact_ref: SafeID


class CaseCounts(WireModel):
    PASS: Annotated[int, Field(strict=True, ge=0)]
    FAILED: Annotated[int, Field(strict=True, ge=0)]
    ERROR: Annotated[int, Field(strict=True, ge=0)]
    SKIPPED: Annotated[int, Field(strict=True, ge=0)]


class FailureIndex(WireModel):
    provider_case_id: SafeID
    outcome: Literal["FAILED", "ERROR"]


class CISummary(WireModel):
    schema_version: Literal["stage13.ci-summary.v1"] = "stage13.ci-summary.v1"
    remote_state: RemoteState
    remote_execution_id: UUID
    environment_id: SafeID
    channel_group: SafeID
    automation_project_id: SafeID
    suite_id: SafeID
    ordinal: int
    product_version: SafeID
    status_revision: int
    result_revision: int
    result_available: bool
    case_counts: CaseCounts | None
    failure_index: tuple[FailureIndex, ...]
    visible_component_inventory: tuple[SafeID, ...]
    visible_change_inventory: tuple[SafeID, ...]
    environment_metadata: dict[str, str]
    visible_semantic_digest: Digest


class FailureDetail(WireModel):
    provider_case_id: SafeID
    outcome: Literal["FAILED", "ERROR"]
    title: str
    error_code: SafeID
    error_excerpt: Annotated[str, Field(max_length=4096)]
    component: SafeID
    visible_change_refs: tuple[SafeID, ...]
    artifact_refs: tuple[SafeID, ...]


class FailurePage(WireModel):
    schema_version: Literal["stage13.failure-page.v1"] = "stage13.failure-page.v1"
    remote_execution_id: UUID
    cases: tuple[FailureDetail, ...] = Field(max_length=50)
    total_failure_cases: int
    next_offset: int | None


class ArtifactContent(WireModel):
    schema_version: Literal["stage13.artifact.v1"] = "stage13.artifact.v1"
    artifact_ref: SafeID
    snippet: str | None


class EvidencePacket(WireModel):
    evidence_id: SafeID
    type: Literal["CI_SUMMARY", "FAILURE_DETAIL", "AUTHORIZED_ARTIFACT"]
    digest: Digest
    owner_scope_id: SafeID
    remote_execution_id: UUID
    provider_case_id: SafeID | None = None
    schema_version: SafeID
    availability: Literal["AVAILABLE", "DELAYED", "UNAVAILABLE", "EXPIRED"]
    content: str
    observed_at: datetime

    @model_validator(mode="after")
    def validate_bytes(self):
        if sha256(self.content.encode("utf-8")) != self.digest:
            raise ValueError("证据 digest 必须对应实际交付 UTF-8 正文")
        limit = 64 * 1024 if self.type == "CI_SUMMARY" else 256 * 1024
        # envelope 也受同一上限约束，避免转义后的工具输出突破预算。
        if len(self.model_dump_json().encode("utf-8")) > limit:
            raise ValueError("证据响应超过合同字节上限")
        return self


def evidence_packet(
    kind,
    body: WireModel,
    request: SubmitRequest,
    receipt: SubmitReceipt,
    availability="AVAILABLE",
    provider_case_id=None,
) -> EvidencePacket:
    content = canonical_bytes(body.model_dump(mode="json")).decode("utf-8")
    digest = sha256(content.encode("utf-8"))
    identity = uuid5(
        receipt.remote_execution_id,
        f"{kind}:{provider_case_id}:{digest}:{availability}",
    )
    return EvidencePacket(
        evidence_id=f"evidence:{identity}",
        type=kind,
        digest=digest,
        owner_scope_id=request.owner_scope_id,
        remote_execution_id=receipt.remote_execution_id,
        provider_case_id=provider_case_id,
        schema_version=body.schema_version,
        availability=availability,
        content=content,
        observed_at=receipt.accepted_at,
    )


class ProviderError(RuntimeError):
    """固定安全错误码；不携带请求正文或连接串。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
