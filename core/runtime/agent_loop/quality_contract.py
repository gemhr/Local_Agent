"""质量评测的独立 Wire 合同；Catalog 摘要不替换 Runtime 内部摘要算法。"""
from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from uuid import UUID

PROTOCOL = "localagent-agent-quality-evaluation-execute.v1"
BUNDLE_SCHEMA = "agent-quality-evidence.v1"
OBSERVATIONS_SCHEMA = "agent-quality-observations.v1"
PRODUCER = "localagent-agent-quality-producer.v1"
CAPTURE_POLICY = "agent-quality-capture.v1"
COMPONENT_LIMIT = 256 * 1024
OBSERVATIONS_LIMIT = 512 * 1024
BUNDLE_LIMIT = 1024 * 1024
HTTP_LIMIT = 2 * 1024 * 1024


def canonical_bytes(value) -> bytes:
    """asset.ParseJSON 的整数子集：保留 null，拒绝浮点数和非法 Unicode。"""
    def check(item):
        if item is None or type(item) is bool:
            return
        if type(item) is int and -(2**63) <= item < 2**63:
            return
        if type(item) is str:
            item.encode("utf-8", errors="strict")
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(k) is str for k in item):
            for key, child in item.items():
                check(key)
                check(child)
            return
        raise ValueError("QUALITY_CANONICAL_TYPE_INVALID")
    check(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def catalog_digest(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def parse_quality_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("QUALITY_DUPLICATE_KEY")
            result[key] = value
        return result
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("QUALITY_CANONICAL_TYPE_INVALID")))
    canonical_bytes(value)
    return value


def quality_runtime_config_digest(config):
    """部署 pin 排除逐 Run 身份；完整 Frame pin 仍由原 Runtime 原样校验。"""
    from copy import deepcopy
    from core.runtime.execution_aggregate import canonical_payload_digest
    value = deepcopy(config)
    for target in value.get("delegation_targets", {}).values():
        target.pop("root_source_digest", None)
    return canonical_payload_digest(value)


class QualityEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: StrictStr = Field(min_length=1, max_length=255)
    query: StrictStr = Field(max_length=65536)
    run_id: UUID
    evaluation_project_id: UUID
    evaluation_run_id: UUID
    attempt_id: UUID
    request_id: UUID
    case_id: UUID
    case_version: StrictStr = Field(min_length=1, max_length=128)
    case_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    execution_manifest_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    capture_profile_id: StrictStr = Field(min_length=1, max_length=255)
    capture_profile_version: StrictInt = Field(ge=1)
    capture_profile_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    timeout_milliseconds: StrictInt = Field(ge=1, le=300000)

    def correlation(self) -> dict:
        data = self.model_dump(mode="json")
        data["query_digest"] = hashlib.sha256(data.pop("query").encode("utf-8")).hexdigest()
        return data
