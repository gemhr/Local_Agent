"""有限的基带输入约束与整数采样比较，不模拟无线协议。"""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from core.runtime.snapshot_serialization import sha256_digest

SCHEMA = "baseband-input.v1"
AGENT_ID = "baseband_test_investigator"
RULE = dict(rule_id="ddt-kdt-sampling", version="1", software_version="baseband-1.0",
    sample_rates=[15, 30, 60], rule="DDT.sample_rate == KDT.sample_rate; gain 1..4; component READY")
TEMPLATES = [dict(scenario_key=k, title=t, description="参数驱动的 DDT/KDT 采样一致性检查", template_version="1")
    for k, t in (("baseband_input_mismatch", "基带输入不一致"), ("baseband_compatible", "基带正常对照"),
                 ("baseband_missing_receipt", "基带执行记录缺失"))]


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    sample_rate: Literal[15, 30, 60]
    gain: int = Field(ge=1, le=4)


class Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["baseband-input.v1"]
    software_version: Literal["baseband-1.0"]
    constraint: dict
    ddt: Manifest
    kdt: Manifest
    component: Literal["READY", "OFFLINE"]
    stimulus: int = Field(ge=1, le=100)
    receipt_available: bool


def initial_state(key):
    compatible = key == "baseband_compatible"
    return dict(schema_version=SCHEMA,
        environment=dict(environment_id="env-baseband", software_version="baseband-1.0"),
        test_case=dict(case_id="sampling-primary", feature_id="sampling"),
        regression_companion=dict(case_id="sampling-control", feature_id="sampling"),
        baseband=dict(schema_version=SCHEMA, software_version="baseband-1.0", constraint=deepcopy(RULE),
            ddt=dict(source="synthetic-ddt", revision="ddt-A", sample_rate=30, gain=2),
            kdt=dict(source="synthetic-kdt-aw", revision="kdt-B", sample_rate=30 if compatible else 15, gain=2),
            component="READY", stimulus=7, receipt_available=key != "baseband_missing_receipt"))


def validate(value):
    result = Inputs.model_validate(value).model_dump()
    if result["constraint"] != RULE:
        raise ValueError("BASEBAND_RULE_UNSUPPORTED")
    return result


def violations(value):
    value = validate(value)
    errors = []
    if value["component"] != "READY":
        errors.append("COMPONENT_NOT_READY")
    if value["ddt"]["sample_rate"] != value["kdt"]["sample_rate"]:
        errors.append("SAMPLE_RATE_MISMATCH")
    return errors


def execute(state, execution_id, case_id=None):
    snapshot = deepcopy(state)
    cases = [state["test_case"], state["regression_companion"]]
    selected = next((c for c in cases if c["case_id"] == (case_id or cases[0]["case_id"])), None)
    if selected is None:
        raise ValueError("CI_CASE_UNKNOWN")
    snapshot["test_case"] = deepcopy(selected)
    inputs = validate(snapshot["baseband"])
    errors = violations(inputs)
    expected = inputs["stimulus"] * inputs["ddt"]["gain"]
    actual = inputs["stimulus"] * inputs["kdt"]["gain"] if not errors else None
    outcome = "FAIL" if errors or actual != expected else "PASS"
    now = datetime.now(timezone.utc)
    stages = [dict(stage="PRECHECK", status="EXECUTED", error=errors[0] if errors else None),
        dict(stage="L1", status="SKIPPED" if errors else "EXECUTED", error=None),
        dict(stage="COMPARISON", status="SKIPPED" if errors else "EXECUTED",
             error="AMPLITUDE_MISMATCH" if not errors and actual != expected else None)]
    summary = dict(execution_id=execution_id, environment_id=state["environment"]["environment_id"],
        case_id=selected["case_id"], outcome=outcome, completed_at=now.isoformat(), stages=stages,
        schema_version="baseband-execution.v1", input_digest=sha256_digest(inputs))
    contents = dict(CI_SUMMARY=summary, INPUT_MANIFEST=inputs,
        CONSTRAINT_CHECK=dict(rule=RULE, rule_digest=sha256_digest(RULE), violations=errors),
        EXECUTION_RECEIPT=dict(used_ddt=inputs["ddt"], used_kdt=inputs["kdt"], stages=stages,
            actual=actual, expected=expected, oracle_version="integer-amplitude.v1", outcome=outcome)
            if inputs["receipt_available"] else None)
    evidence = [dict(evidence_id=str(uuid5(NAMESPACE_URL, execution_id + ":" + kind)), execution_id=execution_id,
        kind=kind, captured_at=now.isoformat(), availability="AVAILABLE" if content is not None else "MISSING",
        media_type="application/json", digest=sha256_digest(content) if content is not None else None,
        content=content, missing_reason="执行采集记录丢失" if content is None else None) for kind, content in contents.items()]
    return dict(started_at=now, completed_at=now, outcome=outcome, execution_snapshot=snapshot,
        summary=summary, evidence_manifest=evidence,
        evidence_digest=sha256_digest(sorted(evidence, key=lambda e: e["evidence_id"])))
