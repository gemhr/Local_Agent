"""受部署管理的评测配置；调用方只能引用摘要，不能提交策略或授权。"""
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from core.runtime.agent_loop.quality_contract import canonical_bytes, catalog_digest, quality_runtime_config_digest
from core.runtime.agent_loop.context import _pin_value
from core.runtime.execution_aggregate import canonical_payload_digest


class AgentPin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_version: StrictStr
    execution_policy: StrictStr
    definition_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_config_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: StrictStr
    runtime_build_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    public_fixture_version: StrictStr
    memory_effect_policy: StrictStr
    agents: dict[str, AgentPin] = Field(min_length=1, max_length=16)


class CaptureProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(min_length=1, max_length=255)
    version: StrictInt = Field(ge=1)
    policy: StrictStr
    tool_aliases: dict[str, StrictStr] = Field(default_factory=dict, max_length=64)
    parameter_paths: dict[str, list[StrictStr]] = Field(default_factory=dict, max_length=64)
    fact_keys: list[StrictStr] = Field(default_factory=list, max_length=16)


def runtime_build_digest():
    root = Path(__file__).resolve().parents[2]
    # 仅代码，不读取本地配置、凭据、运行数据或 .ai 材料。
    import hashlib
    files = sorted((root / "core").rglob("*.py")) + [root / "server.py"]
    return catalog_digest([{ "path": p.relative_to(root).as_posix(),
                            "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files])


def load_configuration(path):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise ValueError("QUALITY_CONFIGURATION_DUPLICATE_KEY")
            result[k] = v
        return result
    raw = Path(path).read_bytes()
    if len(raw) > 80 * 1024:
        raise ValueError("QUALITY_CONFIGURATION_LIMIT")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    if type(data) is not dict or not {'manifest','capture_profile'} <= set(data) or set(data) - {'manifest','capture_profile','ci_prompt','ci_synthetic'}:
        raise ValueError("QUALITY_CONFIGURATION_INVALID")
    manifest = Manifest.model_validate(data["manifest"]).model_dump()
    profile = CaptureProfile.model_validate(data["capture_profile"]).model_dump()
    canonical_bytes(data)
    if (manifest["schema_version"] != "agent-quality-execution-manifest.v1"
            or manifest["memory_effect_policy"] != "SERVICE_PROJECT_READ_ONLY.v1"
            or profile["policy"] != "agent-quality-capture.v1"
            or manifest["runtime_build_digest"] != runtime_build_digest()):
        raise ValueError("QUALITY_CONFIGURATION_PIN_INVALID")
    import re
    if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", k) for k in profile["fact_keys"]):
        raise ValueError("QUALITY_CAPTURE_PROFILE_INVALID")
    if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", alias) for alias in profile["tool_aliases"].values()):
        raise ValueError("QUALITY_CAPTURE_PROFILE_INVALID")
    if any(len(paths) > 16 or any(not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*){0,3}", p) for p in paths)
           for paths in profile["parameter_paths"].values()):
        raise ValueError("QUALITY_CAPTURE_PROFILE_INVALID")
    return manifest, profile


def check_agent(manifest, definition):
    pin = manifest["agents"].get(definition.agent_id)
    if (pin is None or pin["agent_version"] != definition.agent_version
            or pin["execution_policy"] not in {"durable-agent-loop.v1", "durable-agent-loop.v2"}
            or definition.business_options.get("execution_policy") != pin["execution_policy"]
            or pin["definition_digest"] != canonical_payload_digest(_pin_value(definition))):
        raise ValueError("QUALITY_AGENT_PIN_INVALID")
    return pin
