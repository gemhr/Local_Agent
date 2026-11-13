"""联合诊断 v2 的公开证据合同与完整规则基线；不读取模拟隐藏控制。"""
from typing import Literal
from pydantic import Field, model_validator
from core.incidents.contracts import StrictDTO, EvidenceReference, CIError
from core.runtime.snapshot_serialization import sha256_digest


class Finding(StrictDTO):
    category: Literal["REFERENCE", "SIMULATION_CHAIN", "PRODUCT_L1_CANDIDATE", "INPUT_CONFIGURATION", "UNDETERMINED"]
    mechanism: Literal["REFERENCE_NOT_APPLICABLE", "CONFIG_APPLIED_AFTER_SAMPLE", "APPLICATION_UNCONFIRMED", "OUTPUT_REFERENCE_DIVERGENCE", "UNDETERMINED"]
    owner_role: Literal["TEST_OWNER", "REFERENCE_OWNER", "DAAS_OWNER", "PRODUCT_OWNER", "UNKNOWN"]
    finding_status: Literal["CONFIRMED", "CANDIDATE", "REFUTED"]
    failure_relation: Literal["SUPPORTED", "UNRESOLVED", "NOT_SUPPORTED"]
    supporting_evidence: list[EvidenceReference] = Field(min_length=1,max_length=6,
        description="每项正向支持均需 INPUT_MANIFEST、REFERENCE_RECORD、EXECUTION_RECEIPT；下发机制还需 DAAS_TIMELINE，已确认迟应用或产品候选还需 APPLIED_STATE。")
    counter_evidence: list[EvidenceReference] = Field(max_length=6)
    uncertainties: list[str] = Field(max_length=6)
    recommended_action: Literal["SELECT_BASEBAND_REFERENCE", "REVIEW_DAAS_APPLICATION", "COLLECT_APPLIED_STATE", "REVIEW_PRODUCT_OUTPUT", "NONE"]


class JointReport(StrictDTO):
    schema_version: Literal["baseband-diagnosis.v2"]
    incident_id: str
    execution_id: str
    input_digest: str = Field(pattern="^[0-9a-f]{64}$")
    evidence_summary: str = Field(min_length=1,max_length=1800)
    decidability: Literal["SUPPORTED", "INSUFFICIENT_EVIDENCE"]
    candidate_findings: list[Finding] = Field(min_length=1,max_length=4)
    uncertainties: list[str] = Field(max_length=6)

    @model_validator(mode="after")
    def uncertainty_required(self):
        if self.decidability == "INSUFFICIENT_EVIDENCE" and not self.uncertainties:
            raise ValueError("不可决报告必须说明证据缺口")
        return self


def facts(evidence):
    """只消费六类公开 bundle；不接收 recipe。"""
    c={e["kind"]:e["content"] for e in evidence if e["availability"]=="AVAILABLE"}
    manifest,ref,receipt=c["INPUT_MANIFEST"],c["REFERENCE_RECORD"],c["EXECUTION_RECEIPT"]
    applicable=(manifest["software_version"] in ref["software_versions"]
        and manifest["environment_condition"] in ref["conditions"])
    applied=c.get("APPLIED_STATE")
    consistent=applied is not None and applied["effective"]==manifest["daas_request"]["requested"]
    accepted=any(e["kind"]=="ACK" and e["status"]=="ACCEPTED" for e in c["DAAS_TIMELINE"]["events"])
    late=applied is not None and not consistent and accepted and any(e["tick"]>=applied["sample_tick"] for e in applied["applied_events"])
    return dict(applicable=applicable,applied=applied,consistent=consistent,late=late,failed=receipt["outcome"]=="FAIL")


def validate_sources(value,row,incident,execution,seen):
    report=JointReport.model_validate(value)
    if (row.agent_id!="baseband_test_investigator" or row.agent_version!="1.1.0"
            or report.incident_id!=incident.incident_id or report.execution_id!=execution.execution_id
            or report.input_digest!=sha256_digest(execution.execution_snapshot["baseband"])):
        raise CIError("CI_REPORT_IDENTITY_INVALID")
    originals={e["evidence_id"]:e for e in execution.evidence_manifest}
    f=facts(execution.evidence_manifest)
    for finding in report.candidate_findings:
        used=set()
        for ref in (*finding.supporting_evidence,*finding.counter_evidence):
            source=originals.get(ref.evidence_id)
            if (source is None or source["availability"]!="AVAILABLE" or source["digest"]!=ref.digest
                    or seen.get(ref.evidence_id)!=dict(incident_id=incident.incident_id,**source)):
                raise CIError("CI_REPORT_SOURCE_INVALID")
            used.add(source["kind"])
        required={"INPUT_MANIFEST","EXECUTION_RECEIPT"}
        if finding.category in {"REFERENCE","PRODUCT_L1_CANDIDATE"}: required.add("REFERENCE_RECORD")
        if finding.category=="SIMULATION_CHAIN": required.add("DAAS_TIMELINE")
        if finding.mechanism=="CONFIG_APPLIED_AFTER_SAMPLE" and (finding.finding_status=="CONFIRMED" or finding.failure_relation=="SUPPORTED"):
            required.add("APPLIED_STATE")
            if not f["late"]: raise CIError("CI_REPORT_UNSUPPORTED")
        if finding.category=="PRODUCT_L1_CANDIDATE" and finding.finding_status!="REFUTED":
            required.add("APPLIED_STATE")
            if finding.finding_status!="CANDIDATE" or not f["applicable"] or not f["consistent"]:
                raise CIError("CI_REPORT_UNSUPPORTED")
        if finding.mechanism=="APPLICATION_UNCONFIRMED" and finding.finding_status=="CONFIRMED":
            raise CIError("CI_REPORT_UNSUPPORTED")
        if not required<=used: raise CIError("CI_REPORT_UNSUPPORTED")
    return report.model_dump()


def rule_report(incident_id,execution):
    f=facts(execution.evidence_manifest)
    evidence={e["kind"]:e for e in execution.evidence_manifest if e["availability"]=="AVAILABLE"}
    refs=lambda kinds:[dict(evidence_id=evidence[k]["evidence_id"],digest=evidence[k]["digest"]) for k in kinds]
    common=["INPUT_MANIFEST","REFERENCE_RECORD","DAAS_TIMELINE","EXECUTION_RECEIPT"]
    findings=[]
    def add(category,mechanism,status,relation,owner,action,support,counter=()):
        findings.append(dict(category=category,mechanism=mechanism,finding_status=status,failure_relation=relation,
            owner_role=owner,recommended_action=action,supporting_evidence=refs(support),counter_evidence=refs(counter),
            uncertainties=["未定位真实产品代码缺陷或唯一根因"] if relation=="UNRESOLVED" else []))
    if not f["applicable"]:
        add("REFERENCE","REFERENCE_NOT_APPLICABLE","CONFIRMED","SUPPORTED" if f["applied"] else "UNRESOLVED",
            "REFERENCE_OWNER","SELECT_BASEBAND_REFERENCE",common,["APPLIED_STATE"] if f["applied"] else [])
    if f["applied"] is None:
        add("SIMULATION_CHAIN","APPLICATION_UNCONFIRMED","CANDIDATE","UNRESOLVED","DAAS_OWNER","COLLECT_APPLIED_STATE",common)
    elif f["late"]:
        add("SIMULATION_CHAIN","CONFIG_APPLIED_AFTER_SAMPLE","CONFIRMED","SUPPORTED","DAAS_OWNER",
            "REVIEW_DAAS_APPLICATION",common+["APPLIED_STATE"])
    elif f["applicable"] and f["consistent"] and f["failed"]:
        add("PRODUCT_L1_CANDIDATE","OUTPUT_REFERENCE_DIVERGENCE","CANDIDATE","SUPPORTED","PRODUCT_OWNER",
            "REVIEW_PRODUCT_OUTPUT",common+["APPLIED_STATE"])
    if not findings:
        add("UNDETERMINED","UNDETERMINED","REFUTED","NOT_SUPPORTED","UNKNOWN","NONE",common+["APPLIED_STATE"])
    return dict(schema_version="baseband-diagnosis.v2",incident_id=incident_id,execution_id=execution.execution_id,
        input_digest=execution.summary["input_digest"],evidence_summary="逐项核验参考适用范围、ACK 与采样状态及原始比较；不证明真实代码缺陷或唯一原因。",
        decidability="INSUFFICIENT_EVIDENCE" if f["applied"] is None else "SUPPORTED",candidate_findings=findings,
        uncertainties=["缺 APPLIED_STATE，输出可由多种内部机制产生，无法确认唯一原因"] if f["applied"] is None else [])
