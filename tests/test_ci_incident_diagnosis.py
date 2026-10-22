"""两项聚焦验证：真实 PG 业务事实与受观察来源封存。"""
import json
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
from uuid import uuid4
import pytest
from core.auth import Principal
from core.persistence.models import UserRow, ObjectOwnershipRow
from core.ci_simulation.execution import execute, initial_state
from core.ci_simulation.models import CIExecutionRow
from core.incidents.service import IncidentService, AGENT_ID, AGENT_VERSION
from core.incidents.contracts import CIError
from core.incidents.models import IncidentRow
from core.agent_platform.ci_diagnosis import validate_report
from core.runtime.snapshot_serialization import sha256_digest as digest, text_digest
from core.runtime.memory_scope import MemoryAccessContext
from core.runtime.tool_contract import canonical_json_digest


async def user(database):
    uid = uuid4()
    async with database.transaction() as session:
        session.add(UserRow(id=uid, subject=str(uid)))
    return Principal(uid, str(uid), frozenset({"USER"}), "test", datetime.now(timezone.utc),
        datetime.now(timezone.utc) + timedelta(hours=1), tenant_id="00000000-0000-0000-0000-000000000001")


@pytest.mark.asyncio
async def test_simulation_preserves_causal_execution_and_visible_evidence(clean_database):
    service = IncidentService(clean_database)
    principal = await user(clean_database)
    instance, _ = await service.create_scenario(principal, "report_pipeline_failure", str(uuid4()))
    intent = str(uuid4())
    first, incident, _ = await service.execute(principal, instance.instance_id, intent)
    assert first.outcome == "FAIL" and incident is not None
    kinds = {e["kind"]: e for e in first.evidence_manifest}
    assert kinds["PARSER_LOG"]["content"]["error"] == "UNSUPPORTED_FIELD_REVISION"
    assert kinds["COMPARISON_LOG"]["content"]["error"] == "PARSE_RESULT_EMPTY"
    again, same, created = await service.execute(principal, instance.instance_id, intent)
    assert not created and again.execution_id == first.execution_id and same.incident_id == incident.incident_id
    second, _, _ = await service.execute(principal, instance.instance_id, str(uuid4()))
    assert second.execution_id != first.execution_id
    async with clean_database.session() as session:
        saved = await session.get(CIExecutionRow, first.execution_id)
        assert saved.evidence_manifest == first.evidence_manifest
    state = initial_state("report_pipeline_failure")
    state["environment"].update(parser_supported_field_revision=2, decoder_supported_field_revision=2)
    assert execute(state, "compatible")["outcome"] == "PASS"
    state["report"]["payload_matches_golden"] = False
    assert execute(state, "mismatch")["outcome"] == "FAIL"
    state["environment"]["environment_id"] = "other-env"
    assert execute(state, "other-env")["summary"]["case_id"] == first.case_id
    limited = execute(initial_state("limited_diagnostics"), "limited")
    assert next(e for e in limited["evidence_manifest"] if e["kind"] == "PARSER_LOG")["content"] is None
    public = await service.detail(principal, incident.incident_id)
    assert "execution_snapshot" not in public and "current_state" not in public
    assert not any(key in json.dumps(public, default=str) for key in ("expected_root_cause", "hidden_root", "fault_label"))


@pytest.mark.asyncio
async def test_investigation_binds_scope_and_seals_only_observed_report(clean_database):
    service = IncidentService(clean_database)
    principal, other = await user(clean_database), await user(clean_database)
    instance, _ = await service.create_scenario(principal, "report_pipeline_failure", str(uuid4()))
    execution, incident, _ = await service.execute(principal, instance.instance_id, str(uuid4()))
    row, _ = await service.admit(principal, incident.incident_id, str(uuid4()))
    async with clean_database.transaction() as session:
        session.add(ObjectOwnershipRow(object_type="RUN", object_id=row.run_id, owner_user_id=principal.user_id, tenant_id=principal.tenant_id))
    context = NS(run_id=row.run_id, session_id=row.session_id, data=NS(entry_agent_id=AGENT_ID, agent_version=AGENT_VERSION),
        memory_access_context=MemoryAccessContext.from_principal(principal, AGENT_ID, row.session_id))
    assert (await service.bound_execution(context))["execution_id"] == execution.execution_id
    with pytest.raises(CIError):
        await service.read_evidence(other, incident.incident_id, execution.evidence_manifest[0]["evidence_id"])
    with pytest.raises(CIError):
        await service.bound_execution(context, str(uuid4()))
    with pytest.raises(CIError):
        await service.bound_execution(NS(run_id=str(uuid4())))
    another, _ = await service.create_scenario(principal, "limited_diagnostics", str(uuid4()))
    other_execution, _, _ = await service.execute(principal, another.instance_id, str(uuid4()))
    with pytest.raises(CIError):
        await service.bound_execution(context, other_execution.evidence_manifest[0]["evidence_id"])
    evidence = [e for e in execution.evidence_manifest if e["kind"] in {"PARSER_LOG", "ENVIRONMENT_RECORD", "CASE_REQUIREMENTS"}]
    report = dict(schema_version="ci-diagnosis.v1", kind="REPORT", incident_id=incident.incident_id, diagnosis_status="SUPPORTED",
        failure_stage="PARSER", root_cause_hypothesis="字段版本超出解析能力", supporting_evidence=[dict(evidence_id=e["evidence_id"], digest=e["digest"]) for e in evidence],
        uncertainties=[], recommended_checks=["核对安装版本"], recommended_owner_role="TOOL_MAINTAINER")
    raw = json.dumps(report)
    payload = dict(content=raw, producer_agent_id=AGENT_ID)
    frame = NS(frame_id="frame", frame_role="ROOT", step_id="step", execution_agent_id=AGENT_ID,
        result_decision_id="final", final_delivery_receipt=dict(decision_id="final", status="DELIVERED", result_digest=text_digest(raw)))
    final = NS(frame_id="frame", decision_id="final", decision_kind="FINAL", iteration_no=4, state="CLOSED")
    actions, tools, iterations = [], [], []
    for n, item in enumerate(evidence, 1):
        output = json.dumps(dict(incident_id=incident.incident_id, **item))
        committed = dict(status="SUCCEEDED", output={"content": output}, safe_summary="已读取授权执行证据")
        projection = dict(outcome="SUCCEEDED", content=output)
        actions.append(NS(frame_id="frame", invocation_id=str(n), iteration_no=n, source_digest=canonical_json_digest(committed),
            result_projection=projection, result_digest=digest(projection)))
        tools.append(NS(invocation_id=str(n), tool_name="ci_read_execution_evidence", state="COMMITTED", committed_result=committed, committed_result_digest=canonical_json_digest(committed)))
        iterations.append(NS(frame_id="frame", decision_id=str(n), iteration_no=n, state="OBSERVED", observation={"results": [{"result": projection}]}))
    image = NS(root=NS(run_id=row.run_id, execution_policy="durable-agent-loop.v2", resume_input=dict(resolved_agent_id=AGENT_ID,
        agent_version=AGENT_VERSION, session_id=row.session_id)), loop_frames=[frame],
        steps=[NS(status="SUCCEEDED", step_id="step", typed_result_payload=payload, result_digest=digest(payload))],
        loop_iterations=[*iterations, final], tool_invocations=tools)
    assert validate_report(raw, row, incident, execution, image, actions) == report
    with pytest.raises(CIError):
        validate_report(raw, row, incident, execution, image, [])
    bad = deepcopy(image)
    bad.tool_invocations[0].committed_result_digest = "0" * 64
    with pytest.raises(CIError):
        validate_report(raw, row, incident, execution, bad, actions)
    limited = deepcopy(execution)
    limited.evidence_manifest = execute(initial_state("limited_diagnostics"), execution.execution_id)["evidence_manifest"]
    with pytest.raises(CIError):
        validate_report(raw, row, incident, limited, image, actions)
    sealed = await service.seal(row.investigation_id, report, text_digest(raw))
    repeated = await service.seal(row.investigation_id, None, None, "INVALID")
    assert sealed.projection_status == repeated.projection_status == "READY" and repeated.report_payload == report
    assert (await service.investigation(principal, incident.incident_id, row.investigation_id)).report_payload == report
