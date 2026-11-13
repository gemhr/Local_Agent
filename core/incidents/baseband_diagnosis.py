"""基带诊断形状与来源合同；业务正确性由独立评测侧判定。"""
from typing import Literal
from pydantic import Field, model_validator
from core.incidents.contracts import StrictDTO, EvidenceReference, CIError
from core.ci_simulation.baseband import AGENT_ID
from core.runtime.snapshot_serialization import sha256_digest


class Finding(StrictDTO):
    category: Literal["INPUT_CONFIGURATION", "UNDETERMINED"]
    mechanism: str = Field(min_length=1, max_length=100)
    owner_role: Literal["TEST_OWNER", "UNKNOWN"]
    supporting_evidence: list[EvidenceReference] = Field(min_length=1, max_length=4)
    counter_evidence: list[EvidenceReference] = Field(max_length=4)
    uncertainties: list[str] = Field(max_length=4)
    recommended_action: Literal["ALIGN_BASEBAND_INPUT", "COLLECT_EXECUTION_RECEIPT", "NONE"]


class BasebandReport(StrictDTO):
    schema_version: Literal["baseband-diagnosis.v1"]
    incident_id: str
    execution_id: str
    input_digest: str = Field(pattern="^[0-9a-f]{64}$")
    evidence_summary: str = Field(min_length=1, max_length=1000)
    decidability: Literal["SUPPORTED", "INSUFFICIENT_EVIDENCE"]
    candidate_findings: list[Finding] = Field(min_length=1, max_length=3)
    uncertainties: list[str] = Field(max_length=4)

    @model_validator(mode="after")
    def uncertainty_required(self):
        if self.decidability == "INSUFFICIENT_EVIDENCE" and not self.uncertainties:
            raise ValueError("不可决报告必须说明证据缺口")
        return self


def validate_sources(value, row, incident, execution, seen):
    report = BasebandReport.model_validate(value)
    if (row.agent_id != AGENT_ID or report.incident_id != incident.incident_id
            or report.execution_id != execution.execution_id
            or report.input_digest != sha256_digest(execution.execution_snapshot["baseband"])):
        raise CIError("CI_REPORT_IDENTITY_INVALID")
    originals = {e["evidence_id"]: e for e in execution.evidence_manifest}
    used = {}
    for finding in report.candidate_findings:
        for ref in (*finding.supporting_evidence, *finding.counter_evidence):
            source = originals.get(ref.evidence_id)
            if (source is None or source["availability"] != "AVAILABLE" or source["digest"] != ref.digest
                    or seen.get(ref.evidence_id) != dict(incident_id=incident.incident_id, **source)):
                raise CIError("CI_REPORT_SOURCE_INVALID")
            used[ref.evidence_id] = source
    if report.decidability == "SUPPORTED" and not {"INPUT_MANIFEST", "CONSTRAINT_CHECK", "EXECUTION_RECEIPT"} <= {e["kind"] for e in used.values()}:
        raise CIError("CI_REPORT_UNSUPPORTED")
    return report.model_dump()


def rule_report(incident_id, execution):
    """规则基线仅消费公开可用原文，绝不制造模型 Run。"""
    contents = {e["kind"]: e["content"] for e in execution.evidence_manifest if e["availability"] == "AVAILABLE"}
    enough = "EXECUTION_RECEIPT" in contents
    errors = contents["CONSTRAINT_CHECK"]["violations"]
    receipt = contents.get("EXECUTION_RECEIPT", {})
    mechanism = errors[0] if errors else "AMPLITUDE_MISMATCH" if receipt.get("outcome") == "FAIL" else "NO_INPUT_FAULT"
    return dict(schema_version="baseband-diagnosis.v1", incident_id=incident_id, execution_id=execution.execution_id,
        input_digest=execution.summary["input_digest"], evidence_summary="公开输入、约束检查与执行记录的确定性分析",
        decidability="SUPPORTED" if enough else "INSUFFICIENT_EVIDENCE", uncertainties=[] if enough else ["缺少实际执行记录"],
        candidate_findings=[dict(category="INPUT_CONFIGURATION" if enough and mechanism != "NO_INPUT_FAULT" else "UNDETERMINED",
            mechanism=mechanism if enough else "UNDETERMINED", owner_role="TEST_OWNER" if enough and mechanism != "NO_INPUT_FAULT" else "UNKNOWN",
            supporting_evidence=[dict(evidence_id=e["evidence_id"], digest=e["digest"]) for e in execution.evidence_manifest if e["availability"] == "AVAILABLE"],
            counter_evidence=[], uncertainties=[] if enough else ["实际使用输入不可确认"],
            recommended_action="COLLECT_EXECUTION_RECEIPT" if not enough else "ALIGN_BASEBAND_INPUT" if mechanism in {"SAMPLE_RATE_MISMATCH", "AMPLITUDE_MISMATCH"} else "NONE")])
