"""基带因果、来源和人工追加回归的最小验证。"""
import json
from copy import deepcopy
from types import SimpleNamespace as NS
from uuid import uuid4
import pytest
from tests.test_ci_incident_diagnosis import user
from core.ci_simulation.execution import execute, initial_state
from core.ci_simulation.models import CIExecutionRow
from core.ci_simulation.baseband import AGENT_ID
from core.incidents.service import IncidentService
from core.incidents.contracts import ActionIntent, ConfirmAction, VerificationIntent, CloseIncident, CIError
from core.incidents.operations import create_action, confirm_action
from core.incidents.verification import create_verification, close_incident
from core.incidents.baseband_diagnosis import rule_report
from core.agent_platform.ci_diagnosis import validate_report
from core.runtime.snapshot_serialization import sha256_digest as digest, text_digest
from core.runtime.tool_contract import canonical_json_digest


def test_baseband_parameter_causality():
    state = initial_state("baseband_input_mismatch")
    failed = execute(state, "unlabelled")
    assert failed["outcome"] == "FAIL"
    assert failed["summary"]["stages"][1]["status"] == "SKIPPED"
    state["baseband"]["kdt"]["sample_rate"] = 30
    assert execute(state, "FAIL-in-name")["outcome"] == "PASS"
    state["baseband"]["kdt"]["revision"] = state["baseband"]["ddt"]["revision"]
    state["baseband"]["kdt"]["gain"] = 3
    comparison = execute(state, "same-revision")
    assert comparison["outcome"] == "FAIL" and comparison["summary"]["stages"][1]["status"] == "EXECUTED"
    assert comparison["summary"]["stages"][-1]["error"] == "AMPLITUDE_MISMATCH"
    assert execute(state, "repeat")["summary"]["stages"] == comparison["summary"]["stages"]
    missing = execute(initial_state("baseband_missing_receipt"), "missing")
    assert missing["outcome"] == failed["outcome"]
    assert next(e for e in missing["evidence_manifest"] if e["kind"] == "EXECUTION_RECEIPT")["availability"] == "MISSING"


def fixture_image(execution, report):
    raw = json.dumps(report, ensure_ascii=False)
    payload = dict(content=raw, producer_agent_id=AGENT_ID)
    row = NS(run_id="run", session_id="session", agent_id=AGENT_ID, agent_version="1.0.0", incident_id="incident")
    frame = NS(frame_id="frame", frame_role="ROOT", step_id="step", execution_agent_id=AGENT_ID,
        result_decision_id="final", final_delivery_receipt=dict(decision_id="final", status="DELIVERED", result_digest=text_digest(raw)))
    actions, tools, iterations = [], [], []
    for n, item in enumerate(execution.evidence_manifest, 1):
        output = json.dumps(dict(incident_id="incident", **item))
        committed = dict(status="SUCCEEDED", output={"content": output})
        projection = dict(outcome="SUCCEEDED", content=output)
        actions.append(NS(frame_id="frame", invocation_id=str(n), iteration_no=n, source_digest=canonical_json_digest(committed), result_projection=projection, result_digest=digest(projection)))
        tools.append(NS(invocation_id=str(n), tool_name="ci_read_execution_evidence", state="COMMITTED", committed_result=committed, committed_result_digest=canonical_json_digest(committed)))
        iterations.append(NS(frame_id="frame", decision_id=str(n), iteration_no=n, state="OBSERVED", observation={"results": [{"result": projection}]}))
    final = NS(frame_id="frame", decision_id="final", decision_kind="FINAL", iteration_no=5, state="CLOSED")
    image = NS(root=NS(run_id="run", execution_policy="durable-agent-loop.v2", resume_input=dict(resolved_agent_id=AGENT_ID, agent_version="1.0.0", session_id="session")),
        loop_frames=[frame], steps=[NS(status="SUCCEEDED", step_id="step", typed_result_payload=payload, result_digest=digest(payload))], loop_iterations=[*iterations, final], tool_invocations=tools)
    return raw, row, NS(incident_id="incident"), image, actions


def test_baseband_observed_final_binding():
    from core.agent_platform.baseband_investigator import definition
    from core.runtime.business_output_schema import schema_matches
    from core.incidents.baseband_quality import Evaluate
    from pydantic import ValidationError
    agent = definition()
    assert "adaptive_stop" not in agent.business_options and len(agent.allowed_tools) == 2
    for key in ("baseband_input_mismatch", "baseband_compatible", "baseband_missing_receipt"):
        result = execute(initial_state(key), "execution")
        execution = NS(execution_id="execution", **result)
        report = rule_report("incident", execution)
        from core.incidents.baseband_diagnosis import BasebandReport
        assert BasebandReport.model_validate(report)
        assert agent.agent_version == "1.1.0"
        assert not schema_matches(agent.business_options["final_report_schema"], report)
        assert not schema_matches(agent.business_options["final_report_schema"], {"schema_version": "baseband-diagnosis.v1"})
        raw, row, incident, image, actions = fixture_image(execution, report)
        assert validate_report(raw, row, incident, execution, image, actions) == report
        assert report["decidability"] == ("INSUFFICIENT_EVIDENCE" if key == "baseband_missing_receipt" else "SUPPORTED")
        with pytest.raises(CIError):
            validate_report(raw, row, incident, execution, image, [])
        bad = deepcopy(execution)
        bad.evidence_manifest[0]["execution_id"] = "other"
        with pytest.raises(CIError):
            validate_report(raw, row, incident, bad, image, actions)
    body = dict(protocol="baseband-quality-request.v1", project_id="p", run_id="r", attempt_id="a", request_id="q", case_id="c",
        frozen_input_digest=digest(execution.execution_snapshot["baseband"]), input=execution.execution_snapshot["baseband"],
        selector=dict(incident_id="incident", investigation_id=None, run_id=None, candidate_kind="RULE"))
    assert Evaluate.model_validate(body)
    with pytest.raises(ValidationError):
        Evaluate.model_validate({**body, "report": report})
    invalid = deepcopy(body)
    invalid["input"]["constraint"]["version"] = "unknown"
    with pytest.raises(ValidationError):
        Evaluate.model_validate(invalid)


@pytest.mark.asyncio
async def test_baseband_human_append_regression(clean_database):
    service = IncidentService(clean_database)
    principal = await user(clean_database)
    instance, _ = await service.create_scenario(principal, "baseband_input_mismatch", str(uuid4()))
    original, incident, _ = await service.execute(principal, instance.instance_id, str(uuid4()))
    frozen = deepcopy(original.evidence_manifest)
    action = await create_action(service, principal, incident.incident_id, ActionIntent(request_id=uuid4(),
        action_kind="ALIGN_BASEBAND_INPUT", parameters={"rule_version": "1"}))
    assert action["preview_payload"]["changes"] == [dict(path=["baseband", "kdt", "sample_rate"], before=15, after=30)]
    with pytest.raises(CIError):
        await confirm_action(service, principal, incident.incident_id, action["action_id"], ConfirmAction(confirmed=True, preview_digest="0" * 64))
    confirmed = await confirm_action(service, principal, incident.incident_id, action["action_id"], ConfirmAction(confirmed=True, preview_digest=action["preview_digest"]))
    assert confirmed["applied_state_revision"] == 2
    single = await create_verification(service, principal, incident.incident_id, VerificationIntent(request_id=uuid4(), mode="SINGLE", case_ids=[original.case_id], expected_state_revision=2))
    group = await create_verification(service, principal, incident.incident_id, VerificationIntent(request_id=uuid4(), mode="GROUP", case_ids=[original.case_id, "sampling-control"], expected_state_revision=2))
    assert single["outcome"] == group["outcome"] == "PASS"
    assert single["comparison"]["changes"] == action["preview_payload"]["changes"]
    assert single["items"][0]["execution_id"] != original.execution_id
    await close_incident(service, principal, incident.incident_id, CloseIncident(request_id=uuid4(), expected_state_revision=2,
        action_id=action["action_id"], single_verification_id=single["verification_id"], group_verification_id=group["verification_id"],
        root_cause="采样率不一致", fix_confirmation="人工确认 KDT 对齐 DDT", regression_confirmed=True))
    async with clean_database.session() as session:
        saved = await session.get(CIExecutionRow, original.execution_id)
        assert saved.outcome == "FAIL" and saved.evidence_manifest == frozen and saved.evidence_digest == original.evidence_digest
