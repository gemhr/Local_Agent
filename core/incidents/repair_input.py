"""合成 CI 修复准入。输入为服务端已封存的调查和原始执行事实。"""
import re
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from core.ci_simulation import tlv_input
from core.ci_simulation.execution import evidence_id_for
from core.incidents.contracts import CIError, TLVDiagnosisReport, EvidenceReference, StrictDTO
from core.incidents.service import AGENT_ID, AGENT_VERSION
from core.runtime.snapshot_serialization import canonical_json, sha256_digest, text_digest

REPAIR_INSTRUCTION = "更新指定脚本的 request_tlv_hex 以符合当前 TLV 接口定义，保留原 TLV 的数值和 case_id。"
SHA256 = r"^[0-9a-f]{64}$"
RepairEvidenceKind = Literal["PARSER_LOG", "ENVIRONMENT_RECORD", "TLV_INTERFACE_DEFINITION", "SCRIPT_BASELINE"]


class RepairInput(StrictDTO):
    schema_version: Literal["repair-input.v1"]
    incident_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    investigation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    case_id: Literal["case-tlv-report"]
    diagnosis_report_digest: str = Field(pattern=SHA256)
    native_result_digest: str = Field(pattern=SHA256)
    execution_evidence_digest: str = Field(pattern=SHA256)
    software_version: Literal["product-2.0"]
    interface_definition_version: Literal["tlv-interface.v1"]
    interface_revision: Literal[2]
    interface_relative_path: Literal["tlv-interface.json"]
    interface_definition_digest: str = Field(pattern=SHA256)
    script_relative_path: Literal["cases/tlv-report.json"]
    script_baseline_digest: str = Field(pattern=SHA256)
    allowed_paths: list[Literal["cases/tlv-report.json"]] = Field(min_length=1, max_length=1)
    allowed_change_fields: list[Literal["request_tlv_hex"]] = Field(min_length=1, max_length=1)
    evidence_refs: dict[RepairEvidenceKind, EvidenceReference] = Field(min_length=4, max_length=4)
    failure_mechanism: Literal["TLV_INPUT_REVISION_STALE"]
    repair_instruction: Literal[REPAIR_INSTRUCTION]
    admission_result: Literal["ADMITTED"]

    @model_validator(mode="after")
    def validate_evidence_identity(self):
        if any(ref.evidence_id != evidence_id_for(self.execution_id, kind) for kind, ref in self.evidence_refs.items()):
            raise ValueError("CI_REPAIR_EVIDENCE_SOURCE_INVALID")
        if (self.interface_definition_digest != tlv_input.BASELINE_DIGESTS[self.interface_relative_path]
                or self.script_baseline_digest != tlv_input.BASELINE_DIGESTS[self.script_relative_path]):
            raise ValueError("CI_REPAIR_BASELINE_MISMATCH")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json")).encode("utf-8")

    def digest(self) -> str:
        return sha256_digest(self.model_dump(mode="json"))


def admit_repair(investigation, incident, execution, instance, run_owner) -> RepairInput:
    if (investigation.incident_id != incident.incident_id or incident.execution_id != execution.execution_id
            or execution.instance_id != instance.instance_id or instance.owner_user_id != incident.owner_user_id
            or instance.tenant_id != incident.tenant_id or run_owner is None
            or run_owner.object_id != investigation.run_id or run_owner.object_type != "RUN"
            or run_owner.owner_user_id != incident.owner_user_id or run_owner.tenant_id != incident.tenant_id):
        raise CIError("CI_REPAIR_IDENTITY_INVALID", 409)
    if (instance.scenario_key != tlv_input.SCENARIO_KEY or instance.template_version != tlv_input.TEMPLATE_VERSION
            or execution.execution_snapshot.get("synthetic_tlv_version") != tlv_input.TEMPLATE_VERSION
            or execution.case_id != tlv_input.PRIMARY_CASE):
        raise CIError("CI_REPAIR_TARGET_NOT_ALLOWED", 422)
    if incident.status != "OPEN":
        raise CIError("CI_REPAIR_INCIDENT_NOT_OPEN", 409)
    if (investigation.projection_status != "READY" or investigation.projected_at is None
            or investigation.safe_error_code is not None or not re.fullmatch(SHA256, investigation.runtime_result_digest or "")
            or (investigation.agent_id, investigation.agent_version) != (AGENT_ID, AGENT_VERSION)):
        raise CIError("CI_REPAIR_DIAGNOSIS_UNVERIFIED", 409)
    try:
        report = TLVDiagnosisReport.model_validate(investigation.report_payload)
    except ValidationError:
        raise CIError("CI_REPAIR_DIAGNOSIS_UNVERIFIED", 409) from None
    if report.incident_id != incident.incident_id:
        raise CIError("CI_REPAIR_IDENTITY_INVALID", 409)
    if report.diagnosis_status != "SUPPORTED":
        raise CIError("CI_REPAIR_INSUFFICIENT_EVIDENCE", 422)
    if (report.failure_stage, report.failure_mechanism, report.recommended_owner_role) != (
            "AUTOMATION", tlv_input.STALE_ERROR, "TEST_OWNER"):
        raise CIError("CI_REPAIR_DIAGNOSIS_MISMATCH", 422)

    evidence = _verified_evidence(execution, report.supporting_evidence)
    interface, script = _verify_tlv_failure(execution, evidence)
    return RepairInput(
        schema_version="repair-input.v1", incident_id=incident.incident_id, execution_id=execution.execution_id,
        investigation_id=investigation.investigation_id, run_id=investigation.run_id, case_id=execution.case_id,
        diagnosis_report_digest=sha256_digest(report.model_dump(exclude_none=True)),
        native_result_digest=investigation.runtime_result_digest, execution_evidence_digest=execution.evidence_digest,
        software_version=interface["software_version"], interface_definition_version=interface["definition_version"],
        interface_revision=interface["required_revision"], interface_relative_path=tlv_input.INTERFACE_PATH,
        interface_definition_digest=interface["definition_sha256"],
        script_relative_path=tlv_input.SCRIPT_PATHS[execution.case_id], script_baseline_digest=script["script_sha256"],
        allowed_paths=[tlv_input.SCRIPT_PATHS[execution.case_id]], allowed_change_fields=[tlv_input.INPUT_FIELD],
        evidence_refs={kind: EvidenceReference(evidence_id=item["evidence_id"], digest=item["digest"])
            for kind, item in evidence.items()},
        failure_mechanism=tlv_input.STALE_ERROR, repair_instruction=REPAIR_INSTRUCTION, admission_result="ADMITTED",
    )


def _verified_evidence(execution, refs):
    manifest = execution.evidence_manifest
    entries = {item["evidence_id"]: item for item in manifest if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)}
    if len(entries) != len(manifest) or sha256_digest([entries[key] for key in sorted(entries)]) != execution.evidence_digest:
        raise CIError("CI_REPAIR_EVIDENCE_DIGEST_MISMATCH", 422)
    evidence = {}
    for ref in refs:
        item = entries.get(ref.evidence_id, {})
        kind, content = item.get("kind"), item.get("content")
        if (not isinstance(kind, str) or kind in evidence or item.get("execution_id") != execution.execution_id
                or ref.evidence_id != evidence_id_for(execution.execution_id, kind)
                or item.get("availability") != "AVAILABLE" or not isinstance(content, dict)
                or content.get("execution_id") != execution.execution_id):
            raise CIError("CI_REPAIR_EVIDENCE_SOURCE_INVALID", 422)
        if ref.digest != item.get("digest") or ref.digest != sha256_digest(content):
            raise CIError("CI_REPAIR_EVIDENCE_DIGEST_MISMATCH", 422)
        evidence[kind] = item
    if not tlv_input.REQUIRED_EVIDENCE_KINDS <= evidence.keys():
        raise CIError("CI_REPAIR_EVIDENCE_MISSING", 422)
    return {kind: evidence[kind] for kind in sorted(tlv_input.REQUIRED_EVIDENCE_KINDS)}


def _verify_tlv_failure(execution, evidence):
    body = {kind: item["content"] for kind, item in evidence.items()}
    interface, script = body["TLV_INTERFACE_DEFINITION"], body["SCRIPT_BASELINE"]
    case_id, execution_id = execution.case_id, execution.execution_id
    script_path = tlv_input.SCRIPT_PATHS[case_id]
    sources = {tlv_input.INTERFACE_PATH: interface.get("definition_content"), script_path: script.get("script_content")}
    if any(not isinstance(source, str) or text_digest(source) != tlv_input.BASELINE_DIGESTS[path]
            for path, source in sources.items()):
        raise CIError("CI_REPAIR_BASELINE_MISMATCH", 422)
    expected = tlv_input.evidence_contents(sources, case_id, execution_id, tlv_input.STALE_ERROR)
    if any(body[kind] != content for kind, content in expected.items()):
        raise CIError("CI_REPAIR_BASELINE_MISMATCH", 422)
    revision, required = script["input_revision"], interface["required_revision"]
    stages = [dict(stage="PRECHECK", status="EXECUTED", error=tlv_input.STALE_ERROR)]
    stages.extend(dict(stage=name, status="SKIPPED", error=None) for name in ("PRODUCT", "PARSER", "COMPARISON"))
    summary = execution.summary
    env, parser = body["ENVIRONMENT_RECORD"], body["PARSER_LOG"]
    case = execution.execution_snapshot.get("test_case", {})
    tool_capabilities = dict(parser_version="parser-2.0", decoder_table_version="decoder-2.0",
        parser_supported_field_revision=required, decoder_supported_field_revision=required)
    if (execution.outcome != "FAIL" or summary.get("outcome") != "FAIL" or summary.get("execution_id") != execution_id
            or summary.get("case_id") != case_id or summary.get("stages") != stages
            or env.get("software_version") != interface["software_version"] or revision >= required
            or any(env.get(key) != value or parser.get(key) != value for key, value in tool_capabilities.items())
            or parser.get("status") != "SKIPPED" or parser.get("skipped_reason") != tlv_input.STALE_ERROR
            or parser.get("field_revision") != revision or case.get("case_id") != case_id
            or case.get("input_contract") != "tlv" or case.get("input_revision") != revision
            or case.get("required_field_revision") != required):
        raise CIError("CI_REPAIR_FAILURE_MISMATCH", 422)
    return interface, script
