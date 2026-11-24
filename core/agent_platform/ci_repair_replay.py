"""从已批准的合成脚本计算回放结果并封存独立评测证据。"""

import json
from uuid import NAMESPACE_URL, uuid5

from core.agent_platform import ci_repair_artifacts as repair
from core.agent_platform.workflow_artifacts import WorkflowArtifactError
from core.ci_simulation import tlv_input
from core.ci_simulation.models import CIExecutionRow, ScenarioInstanceRow
from core.incidents.models import IncidentRow, InvestigationRow
from core.incidents.repair_input import admit_repair
from core.persistence.models import ExternalRunRow, ObjectOwnershipRow
from core.runtime.snapshot_serialization import canonical_json, sha256_digest, text_digest

SCHEMA = "ci-repair-replay.v1"
WIRE = "ci-repair-evidence.v1"
ORACLE = "tlv-oracle.v1"


def evaluate_case(interface, script, case_id):
    expected = tlv_input.EXPECTED_VALUES[case_id]
    revision = value = None
    try:
        revision, value = tlv_input.decode_request(interface, script, case_id)
    except (TypeError, ValueError):
        reason = "TLV_INPUT_INVALID"
    else:
        if revision != interface["required_revision"]:
            reason = tlv_input.STALE_ERROR
        elif value != expected:
            reason = "TLV_BUSINESS_VALUE_MISMATCH"
        else:
            reason = None
    return dict(case_id=case_id, input_digest=text_digest(script), expected_value=expected,
                actual_value=value, revision=revision, oracle_status="FAIL" if reason else "PASS", reason=reason)


async def seal(session, run, native, producer, replay, approval, approval_binding):
    artifact = producer.artifact_ref
    dto = repair.require_input(producer.bound_input, (native.result_ref or {}).get("repair_input_digest"))
    binding = canonical_json({key: artifact[key] for key in (
        "schema_version", "workflow_run_id", "artifact_digest", "diff_digest", "baseline_revision",
    )})
    if (replay.bound_input != binding or replay.platform_run_id is not None or replay.result_ref is not None
            or native.runtime_kind != "NATIVE" or native.status != "SUCCEEDED" or native.runtime_status != "SUCCEEDED"
            or producer.runtime_kind != "OPENCODE" or dto.run_id != native.platform_run_id
            or producer.input_digest != sha256_digest(producer.bound_input)
            or dto.incident_id != run.handoff_input["incident_id"] or dto.execution_id != run.handoff_input["execution_id"]):
        raise WorkflowArtifactError("CI_REPAIR_REPLAY_BINDING_INVALID")
    incident = await session.get(IncidentRow, dto.incident_id)
    execution = await session.get(CIExecutionRow, dto.execution_id)
    investigation = await session.get(InvestigationRow, dto.investigation_id)
    external = await session.get(ExternalRunRow, producer.platform_run_id)
    if (incident is None or execution is None or investigation is None or external is None
            or (incident.tenant_id, incident.owner_user_id) != (run.tenant_id, run.owner_user_id)
            or (investigation.run_id, investigation.session_id) != (dto.run_id, f"{run.workflow_run_id}:{native.step_id}")
            or (external.tenant_id, external.user_id, external.session_id, external.instance_id,
                external.agent_id, external.agent_version, external.observation_state, external.terminal_status,
                external.error_code, external.external_session_id, external.user_message_id, external.assistant_message_id) != (
                run.tenant_id, str(run.owner_user_id), f"{run.workflow_run_id}:{producer.step_id}", producer.runtime_instance_id,
                producer.agent_id, producer.agent_version, "TERMINAL", "SUCCEEDED", None,
                artifact["external_session_id"], artifact["user_message_id"], artifact["assistant_message_id"])):
        raise WorkflowArtifactError("CI_REPAIR_REPLAY_BINDING_INVALID")
    instance = await session.get(ScenarioInstanceRow, execution.instance_id)
    owner = await session.get(ObjectOwnershipRow, ("RUN", dto.run_id))
    if instance is None or admit_repair(investigation, incident, execution, instance, owner) != dto:
        raise WorkflowArtifactError("CI_REPAIR_REPLAY_BINDING_INVALID")

    sources = tlv_input.load_baselines()
    interface = sources[tlv_input.INTERFACE_PATH]
    control = sources[tlv_input.SCRIPT_PATHS[tlv_input.CONTROL_CASE]]
    definition = json.loads(interface)
    results = {role: [evaluate_case(definition, artifact[field], tlv_input.PRIMARY_CASE),
                      evaluate_case(definition, control, tlv_input.CONTROL_CASE)]
               for role, field in (("baseline", "before"), ("candidate", "after"))}
    receipt = dict(schema_version=SCHEMA, replay_id=str(uuid5(NAMESPACE_URL,
        f"{run.workflow_run_id}:{replay.step_id}:{replay.attempt}")), workflow_run_id=run.workflow_run_id,
        step_run_id=replay.step_id, attempt=replay.attempt, incident_id=incident.incident_id, execution_id=execution.execution_id,
        investigation_id=investigation.investigation_id, native_run_id=native.platform_run_id, opencode_run_id=external.run_id,
        approval_id=approval.approval_id, approval_binding_digest=approval.binding_digest, repair_input_digest=dto.digest(),
        baseline_revision=artifact["baseline_revision"], artifact_digest=artifact["artifact_digest"], diff_digest=artifact["diff_digest"],
        oracle_version=ORACLE, interface_definition_digest=text_digest(interface), before_sha256=artifact["before_sha256"],
        after_sha256=artifact["after_sha256"], control_sha256=text_digest(control), results=results, technical_status="COMPLETED")
    receipt["receipt_digest"] = sha256_digest(receipt)
    report = {key: investigation.report_payload[key] for key in ("schema_version", "kind", "incident_id", "diagnosis_status",
        "failure_stage", "failure_mechanism", "supporting_evidence", "recommended_owner_role")}
    evidence = canonical_json(dict(schema_version=WIRE, workflow=dict(workflow_run_id=run.workflow_run_id, workflow_id=run.workflow_id,
        workflow_version=run.workflow_version, tenant_id=run.tenant_id, space_id=str(run.space_id), owner_user_id=str(run.owner_user_id)),
        incident=dict(incident_id=incident.incident_id, execution_id=incident.execution_id, status=incident.status),
        execution=dict(execution_id=execution.execution_id, case_id=execution.case_id, outcome=execution.outcome,
            scenario=instance.scenario_key, scenario_version=instance.template_version, evidence_digest=execution.evidence_digest),
        native=dict(investigation_id=investigation.investigation_id, run_id=investigation.run_id, session_id=investigation.session_id,
            agent_id=investigation.agent_id, agent_version=investigation.agent_version, report=report,
            report_digest=dto.diagnosis_report_digest, projection_digest=sha256_digest(report),
            result_digest=investigation.runtime_result_digest), repair_input=producer.bound_input,
        approval=dict(approval_id=approval.approval_id, binding_digest=approval.binding_digest, status=approval.status,
            decided_by=str(approval.decided_by), decided_at=approval.decided_at.isoformat(), consumed_at=approval.consumed_at.isoformat(),
            replay_id=receipt["replay_id"], binding=canonical_json(approval_binding)),
        artifact=artifact, interface=interface, control=control, receipt=receipt))
    return dict(receipt=receipt, evidence=evidence, evidence_digest=text_digest(evidence))
