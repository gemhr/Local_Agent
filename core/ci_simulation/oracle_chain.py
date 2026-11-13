"""有限整数 Oracle 与 DAAS 采样链；完整重放输入不等于工具可见投影。"""
from copy import deepcopy
from datetime import datetime, timezone
from typing import Literal
from uuid import NAMESPACE_URL, uuid5
from pydantic import BaseModel, ConfigDict, Field, model_validator
from core.ci_simulation.baseband import Manifest
from core.runtime.snapshot_serialization import sha256_digest

SCHEMA = "baseband-input.v2"
CONSTRAINT = dict(rule_id="ddt-kdt-sampling", version="2")
REFERENCES = {
    "1": dict(reference_id="integer-reference", version="1", software_versions=["baseband-1.0"],
              conditions=["nominal"], offset=0, comparison_rule="integer-equality.v1"),
    "2": dict(reference_id="integer-reference", version="2", software_versions=["baseband-2.0"],
              conditions=["nominal"], offset=1, comparison_rule="integer-equality.v1"),
}
TEMPLATES = [dict(scenario_key=k, title=t, description="参考适用性与配置生效联合调查", template_version="2")
    for k,t in (("oracle_reference_scope", "参考适用性调查"), ("oracle_application_delay", "配置生效调查"),
                ("oracle_output_divergence", "输出差异调查"), ("oracle_partial_capture", "缺失采集联合调查"))]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Configuration(Strict):
    config_id: str = Field(min_length=1)
    gain: int = Field(ge=1, le=4)
    sample_rate: Literal[15,30,60]


class Event(Strict):
    kind: Literal["REQUEST", "SENT", "ACK", "APPLIED"]
    tick: int = Field(ge=0, le=100)
    sequence: int = Field(ge=0, le=20)
    request_id: str
    config_id: str
    status: Literal["ACCEPTED", "REJECTED", "RECORDED"]


class DAAS(Strict):
    request_id: str = Field(min_length=1)
    requested: Configuration
    initial: Configuration
    events: list[Event] = Field(max_length=12)
    sample_tick: int = Field(ge=1, le=100)


class Reference(Strict):
    reference_id: Literal["integer-reference"]
    version: Literal["1", "2"]


class Implementation(Strict):
    version: Literal["integer-l1.v1"]
    offset: int = Field(ge=-10, le=10)


class Capture(Strict):
    applied_state: bool


class Inputs(Strict):
    schema_version: Literal["baseband-input.v2"]
    software_version: Literal["baseband-1.0", "baseband-2.0"]
    environment_condition: Literal["nominal", "degraded"]
    constraint: dict
    ddt: Manifest
    kdt: Manifest
    stimulus: int = Field(ge=1, le=100)
    reference: Reference
    daas: DAAS
    implementation: Implementation
    capture: Capture

    @model_validator(mode="after")
    def supported(self):
        d=self.daas
        if (self.constraint != CONSTRAINT or self.ddt.sample_rate != self.kdt.sample_rate
                or d.requested.gain != self.kdt.gain or d.requested.sample_rate != self.kdt.sample_rate
                or len({(e.tick,e.sequence) for e in d.events}) != len(d.events)):
            raise ValueError("ORACLE_INPUT_INVALID")
        return self


def validate(value):
    return Inputs.model_validate(value).model_dump()


def applicable(reference, value):
    return (value["software_version"] in reference["software_versions"]
        and value["environment_condition"] in reference["conditions"]
        and 1 <= value["stimulus"] <= 100 and value["ddt"]["sample_rate"] == value["kdt"]["sample_rate"])


def public_manifest(value):
    return {k:deepcopy(value[k]) for k in ("schema_version", "software_version", "environment_condition",
        "constraint", "ddt", "kdt", "stimulus")} | dict(daas_request=dict(request_id=value["daas"]["request_id"],
        requested=deepcopy(value["daas"]["requested"]), sample_tick=value["daas"]["sample_tick"]))


def simulate(value):
    v=validate(value); d=v["daas"]
    requested=sent=accepted=False
    effective=deepcopy(d["initial"]); applied=[]
    for e in sorted(d["events"],key=lambda e:(e["tick"],e["sequence"])):
        if (e["request_id"],e["config_id"]) != (d["request_id"],d["requested"]["config_id"]):
            continue
        if e["kind"] == "REQUEST": requested=True
        elif e["kind"] == "SENT" and requested: sent=True
        elif e["kind"] == "ACK" and sent: accepted=e["status"] == "ACCEPTED"
        elif e["kind"] == "APPLIED" and accepted:
            applied.append(e)
            if e["tick"] < d["sample_tick"]: effective=deepcopy(d["requested"])
    ref=REFERENCES[v["reference"]["version"]]
    expected=v["stimulus"]*d["requested"]["gain"]+ref["offset"]
    actual=v["stimulus"]*effective["gain"]+(v["software_version"] == "baseband-2.0")+v["implementation"]["offset"]
    return dict(actual=actual, expected=expected, outcome="PASS" if actual == expected else "FAIL",
                effective=effective, applied=applied, reference=ref, reference_applicable=applicable(ref,v))


def initial_state(key):
    index=[t["scenario_key"] for t in TEMPLATES].index(key)
    events=[dict(kind=k,tick=t,sequence=i,request_id="request-1",config_id="config-new",status=s)
        for i,(k,t,s) in enumerate((("REQUEST",1,"RECORDED"),("SENT",2,"RECORDED"),
            ("ACK",3,"ACCEPTED"),("APPLIED",12 if index in (1,3) else 4,"RECORDED")))]
    config=lambda name,gain:dict(config_id=name,gain=gain,sample_rate=30)
    value=dict(schema_version=SCHEMA,software_version="baseband-2.0",environment_condition="nominal",
        constraint=deepcopy(CONSTRAINT),ddt=dict(source="synthetic-ddt",revision="2",sample_rate=30,gain=2),
        kdt=dict(source="synthetic-kdt",revision="2",sample_rate=30,gain=2),stimulus=7,
        reference=dict(reference_id="integer-reference",version="1" if index in (0,3) else "2"),
        daas=dict(request_id="request-1",requested=config("config-new",2),initial=config("config-old",1),events=events,sample_tick=10),
        implementation=dict(version="integer-l1.v1",offset=2 if index==2 else 7 if index==3 else 0),
        capture=dict(applied_state=index!=3))
    return dict(schema_version=SCHEMA,environment=dict(environment_id="env-oracle",software_version=value["software_version"]),
        test_case=dict(case_id="integer-primary",feature_id="oracle-chain"),
        regression_companion=dict(case_id="integer-control",feature_id="oracle-chain"),baseband=validate(value))


def execute(state,execution_id,case_id=None):
    snapshot=deepcopy(state)
    selected=next((c for c in (state["test_case"],state["regression_companion"]) if c["case_id"]==(case_id or state["test_case"]["case_id"])),None)
    if selected is None: raise ValueError("CI_CASE_UNKNOWN")
    snapshot["test_case"]=deepcopy(selected)
    value=validate(snapshot["baseband"]); result=simulate(value); d=value["daas"]
    now=datetime.now(timezone.utc)
    stages=[dict(stage=s,status="EXECUTED",error="OUTPUT_REFERENCE_DIVERGENCE" if s=="COMPARISON" and result["outcome"]=="FAIL" else None) for s in ("PRECHECK","L1","COMPARISON")]
    summary=dict(schema_version="baseband-execution.v2",execution_id=execution_id,
        environment_id=state["environment"]["environment_id"],case_id=selected["case_id"],outcome=result["outcome"],
        input_digest=sha256_digest(value),completed_at=now.isoformat(),sample_tick=d["sample_tick"],stages=stages)
    contents=dict(CI_SUMMARY=summary,INPUT_MANIFEST=public_manifest(value),
        REFERENCE_RECORD=deepcopy(result["reference"]),
        DAAS_TIMELINE=dict(request_id=d["request_id"],config_id=d["requested"]["config_id"],
            events=[deepcopy(e) for e in d["events"] if e["kind"]!="APPLIED"],sample_tick=d["sample_tick"]),
        APPLIED_STATE=dict(sample_tick=d["sample_tick"],request_id=d["request_id"],effective=result["effective"],
            applied_events=result["applied"]) if value["capture"]["applied_state"] else None,
        EXECUTION_RECEIPT=dict(actual=result["actual"],expected=result["expected"],outcome=result["outcome"],
            reference=value["reference"],comparison_rule="integer-equality.v1",stages=stages))
    evidence=[dict(evidence_id=str(uuid5(NAMESPACE_URL,execution_id+":"+k)),execution_id=execution_id,
        kind=k,captured_at=now.isoformat(),availability="AVAILABLE" if c is not None else "MISSING",
        media_type="application/json",digest=sha256_digest(c) if c is not None else None,content=c,
        missing_reason="采样状态未采集" if c is None else None) for k,c in contents.items()]
    return dict(started_at=now,completed_at=now,outcome=result["outcome"],execution_snapshot=snapshot,
        summary=summary,evidence_manifest=evidence,evidence_digest=sha256_digest(sorted(evidence,key=lambda e:e["evidence_id"])))
