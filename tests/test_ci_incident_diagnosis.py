"""验证 CI 业务事实、Native 来源封存及合成修复输入。"""
import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
from uuid import uuid4
import pytest
from core.auth import Principal
from core.persistence.models import UserRow, ObjectOwnershipRow
from core.ci_simulation.execution import execute, initial_state
from core.ci_simulation import tlv_input
from core.ci_simulation.models import CIExecutionRow
from core.incidents.service import IncidentService, AGENT_ID, AGENT_VERSION
from core.incidents.contracts import CIError, CreateScenario
from core.incidents.models import IncidentRow
from core.agent_platform.ci_diagnosis import CIInvestigationService, definition, validate_report
from core.agent_platform.ci_diagnosis_tools import CIDiagnosisTool, TOOL_NAMES
from core.persistence.sync_bridge import SyncPersistenceBridge
from core.runtime.snapshot_serialization import sha256_digest as digest, text_digest
from core.runtime.memory_scope import MemoryAccessContext
from core.runtime.tool_contract import ToolExecutionStatus, canonical_json_digest


async def user(database):
    uid = uuid4()
    async with database.transaction() as session:
        session.add(UserRow(id=uid, subject=str(uid)))
    return Principal(uid, str(uid), frozenset({"USER"}), "test", datetime.now(timezone.utc),
        datetime.now(timezone.utc) + timedelta(hours=1), tenant_id="00000000-0000-0000-0000-000000000001")


@pytest.mark.asyncio
async def test_evidence_scope_denial_is_a_confirmed_read_failure_across_bridge():
    async def deny(run_context, evidence_id):
        raise CIError("CI_EVIDENCE_SCOPE_DENIED", 404)

    adapter = CIDiagnosisTool(TOOL_NAMES[1], NS(bound_execution=deny), SyncPersistenceBridge(asyncio.get_running_loop()))
    context = NS(run_context=None, raise_if_cancelled=lambda: None, remaining_seconds=lambda: 10)
    invocation = adapter.build_invocation('{"evidence_id":"not-in-catalog"}')
    response = await asyncio.to_thread(adapter.invoke_once, invocation, context)
    assert response.status is ToolExecutionStatus.FAILED
    assert json.loads(response.content) == {"error": "CI_EVIDENCE_SCOPE_DENIED"}


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
    directory = await service.bound_execution(context)
    assert directory["execution_id"] == execution.execution_id
    assert len(directory["evidence"]) == 6 and "diagnosis_requirements" not in directory
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
    investigator = CIInvestigationService(service, None, None, None, None, real_model=False)
    with pytest.raises(CIError, match="CI_REPAIR_TARGET_NOT_ALLOWED"):
        await investigator.repair_input(principal, incident.incident_id, row.investigation_id)


def test_synthetic_tlv_files_determine_precheck_and_control():
    assert CreateScenario(scenario_key=tlv_input.SCENARIO_KEY, request_id=uuid4()).scenario_key == tlv_input.SCENARIO_KEY
    state = initial_state(tlv_input.SCENARIO_KEY)
    script_path = tlv_input.SCRIPT_PATHS[tlv_input.PRIMARY_CASE]
    script_bytes = (tlv_input.FIXTURE_ROOT / script_path).read_bytes()
    assert b'"request_tlv_hex": "100107"' in script_bytes
    first = execute(state, "original")
    evidence = {e["kind"]: e["content"] for e in first["evidence_manifest"]}
    assert evidence["SCRIPT_BASELINE"]["script_content"].encode("utf-8") == script_bytes
    assert evidence["SCRIPT_BASELINE"]["script_sha256"] == hashlib.sha256(script_bytes).hexdigest()
    assert evidence["SCRIPT_BASELINE"]["input_revision"] == 1
    assert evidence["TLV_INTERFACE_DEFINITION"]["required_revision"] == 2
    assert first["outcome"] == "FAIL"
    assert first["summary"]["stages"][0] == dict(stage="PRECHECK", status="EXECUTED", error=tlv_input.STALE_ERROR)
    assert all(stage["status"] == "SKIPPED" for stage in first["summary"]["stages"][1:])
    control = execute(state, "control", tlv_input.CONTROL_CASE)
    assert control["outcome"] == "PASS"
    assert next(e["content"]["input_revision"] for e in control["evidence_manifest"] if e["kind"] == "SCRIPT_BASELINE") == 2

    # 在内存副本中只替换文件正文，验证因果和大端值，不写受信基线。
    state["synthetic_tlv_baselines"][script_path] = script_bytes.decode().replace("100107", "11020007")
    assert execute(state, "candidate")["outcome"] == "PASS"
    state["synthetic_tlv_baselines"][script_path] = script_bytes.decode().replace("100107", "11020700")
    assert execute(state, "wrong-value")["summary"]["stages"][-1]["error"] == "GOLDEN_MISMATCH"
    legacy = execute(initial_state("stale_case_input"), "legacy")
    assert legacy["summary"]["stages"][0]["error"] == tlv_input.STALE_ERROR
    assert len(legacy["evidence_manifest"]) == 6


@pytest.mark.asyncio
async def test_legacy_native_run_rejects_file_diagnosis_field(clean_database, tmp_path, monkeypatch):
    from tests.test_ci_adaptive_stop import build, run

    q = await build(clean_database, tmp_path)
    original = q.model.report
    monkeypatch.setattr(q.model, "report", lambda insufficient=False: dict(original(insufficient),
        failure_mechanism=tlv_input.STALE_ERROR))
    result, image = await run(q)
    assert result.status.value == "FAILED" and result.error_code == "LOOP_DECISION_INVALID"
    assert image.steps[0].typed_result_payload == {"content": "", "complete": False}


@pytest.mark.asyncio
async def test_tlv_native_run_rejects_file_diagnosis_field_for_wrong_session(clean_database, tmp_path, monkeypatch):
    from core.auth import ObjectAuthorizationService
    from core.runtime.model_invocation import ModelAdapterResponse
    from tests.test_ci_adaptive_stop import build, run
    from tests.test_ci_incident_resolution import setup_incident

    incidents, principal, instance, execution, incident = await setup_incident(clean_database, tlv_input.SCENARIO_KEY)
    q = await build(clean_database, tmp_path, reuse=NS(incidents=incidents, principal=principal, instance=instance,
        execution=execution, incident=incident))
    wrong_session = str(uuid4())
    access = MemoryAccessContext.from_principal(principal, q.row.agent_id, wrong_session)
    await ObjectAuthorizationService(clean_database).bind_new(principal, "CONVERSATION", access.conversation_id)
    scope = await q.factory.create_run_scope(q.row.agent_id, "调查授权 CI 执行失败", run_id=q.row.run_id,
        session_id=wrong_session, memory_access_context=access, timeout_seconds=180, persist=True)

    report = dict(q.model.report(insufficient=True), supporting_evidence=[], failure_mechanism=tlv_input.STALE_ERROR)
    monkeypatch.setattr(q.model, "invoke_request", lambda *args, **kwargs:
        ModelAdapterResponse(json.dumps(report, ensure_ascii=False), finish_reason="stop", tool_call_count=0))
    result, image = await run(q, scope)
    assert image.root.run_id == q.row.run_id
    assert image.root.resume_input["resolved_agent_id"] == q.row.agent_id
    assert image.root.resume_input["session_id"] == wrong_session != q.row.session_id
    assert result.status.value == "FAILED" and result.error_code == "LOOP_DECISION_INVALID"
    assert image.steps[0].typed_result_payload == {"content": "", "complete": False}
    assert image.loop_frames[0].final_delivery_receipt is None

    investigator = CIInvestigationService(incidents, None, q.repo, None, None, real_model=False)
    row, status = await investigator.project_if_terminal(q.row.investigation_id)
    assert status == "FAILED" and row.projection_status == "FAILED" and row.safe_error_code == "CI_RUNTIME_FAILED"
    assert row.report_payload is None and row.runtime_result_digest is None
    with pytest.raises(CIError, match="CI_REPAIR_DIAGNOSIS_UNVERIFIED"):
        await investigator.repair_input(principal, incident.incident_id, q.row.investigation_id)


@pytest.mark.asyncio
async def test_synthetic_tlv_native_run_seals_only_admissible_repair_input(clean_database, tmp_path, monkeypatch):
    from core.agent_platform.ci_diagnosis_tools import TOOL_NAMES
    from core.agent_platform.ci_evidence import minimum_coverage
    from core.incidents.contracts import QualityExport
    from core.incidents.quality import export_facts
    from core.incidents.repair_input import RepairInput, admit_repair
    from core.runtime.model_invocation import ModelAdapterResponse
    from tests.test_ci_adaptive_stop import build, calls, run
    from tests.test_ci_incident_resolution import setup_incident

    incidents, principal, instance, execution, incident = await setup_incident(clean_database, tlv_input.SCENARIO_KEY)
    assert incident.execution_id == execution.execution_id and instance.template_version == tlv_input.TEMPLATE_VERSION
    with pytest.raises(CIError, match="CI_QUALITY_SOURCE_UNAVAILABLE"):
        await export_facts(incidents, principal, QualityExport(execution_ids=[execution.execution_id]))
    contrast, contrast_incident, _ = await incidents.execute(principal, instance.instance_id, str(uuid4()), case_id=tlv_input.CONTROL_CASE)
    assert contrast.outcome == "PASS" and contrast_incident is None
    q = await build(clean_database, tmp_path, reuse=NS(incidents=incidents, principal=principal, instance=instance,
        execution=execution, incident=incident), run_id=str(uuid4()), session_id=str(uuid4()),
        loop_limits={"total_tokens": definition().business_options["loop_policy"]["total_tokens"]})
    investigator = CIInvestigationService(incidents, None, q.repo, None, None, real_model=False)
    with pytest.raises(CIError, match="CI_REPAIR_DIAGNOSIS_UNVERIFIED"):
        await investigator.repair_input(principal, incident.incident_id, q.row.investigation_id)
    repeated, created = await incidents.admit(principal, incident.incident_id, q.row.request_id,
        run_id=q.row.run_id, session_id=q.row.session_id)
    assert not created and repeated.investigation_id == q.row.investigation_id
    with pytest.raises(CIError, match="CI_REQUEST_CONFLICT"):
        await incidents.admit(principal, incident.incident_id, q.row.request_id, run_id=str(uuid4()))

    def model(messages, *, generation_options, **_):
        observed = [json.loads(json.loads(message["content"])["content"]) for message in messages if message["role"] == "tool"]
        if not observed:
            return calls(("context", TOOL_NAMES[0], {}))
        context, *reads = observed
        requirements = context["diagnosis_requirements"]
        assert "stale_case_input_file@1" in requirements and "SCRIPT_BASELINE" in requirements
        assert 'failure_mechanism="TLV_INPUT_REVISION_STALE"' in requirements
        assert {e["kind"] for e in context["evidence"]} == tlv_input.REQUIRED_EVIDENCE_KINDS
        seen = {e["kind"]: e for e in reads}
        pending = [e for e in context["evidence"] if e["kind"] not in seen]
        if pending:
            assert generation_options.get("tool_choice") != "none"
            return calls(*[("read-" + e["kind"], TOOL_NAMES[1], {"evidence_id": e["evidence_id"]}) for e in pending[:2]])
        assert generation_options["tool_choice"] == "none"
        assert not minimum_coverage({e["evidence_id"]: e for e in reads if e["kind"] != "TLV_INTERFACE_DEFINITION"}, execution)
        script, interface = seen["SCRIPT_BASELINE"]["content"], seen["TLV_INTERFACE_DEFINITION"]["content"]
        parser = seen["PARSER_LOG"]["content"]
        assert script["script_content"] == (tlv_input.FIXTURE_ROOT / script["relative_path"]).read_text(encoding="utf-8")
        assert parser["status"] == "SKIPPED" and parser["skipped_reason"] == script["precheck_error"]
        report = dict(schema_version="ci-diagnosis.v1", kind="REPORT", incident_id=context["incident_id"],
            diagnosis_status="SUPPORTED", failure_stage="AUTOMATION", failure_mechanism=script["precheck_error"],
            root_cause_hypothesis=f"预检失败：脚本 TLV r{script['input_revision']}，当前接口和 Parser 支持 r{interface['required_revision']}。",
            supporting_evidence=[dict(evidence_id=e["evidence_id"], digest=e["digest"]) for e in reads],
            uncertainties=[], recommended_checks=["按接口定义更新 " + script["relative_path"]], recommended_owner_role="TEST_OWNER")
        return ModelAdapterResponse(json.dumps(report, ensure_ascii=False), finish_reason="stop", tool_call_count=0)

    monkeypatch.setattr(q.model, "invoke_request", model)
    result, image = await run(q)
    assert result.status.value == "SUCCEEDED", (result.error_code, image.root.budget_consumed, image.root.budget_totals)
    assert image.root.run_id == q.row.run_id and len(image.tool_invocations) == 5
    frame, = image.loop_frames
    assert frame.adaptive_decision["reason"] == "COVERAGE_SUFFICIENT"
    system = frame.context_seed["messages"][0]["content"]
    assert definition().instructions in system and "stale_case_input_file@1" not in system
    row, status = await investigator.project_if_terminal(q.row.investigation_id)
    assert status == "SUCCEEDED" and row.projection_status == "READY", row.safe_error_code

    repair = await investigator.repair_input(principal, incident.incident_id, row.investigation_id)
    assert (repair.incident_id, repair.execution_id, repair.investigation_id, repair.run_id) == (
        incident.incident_id, execution.execution_id, row.investigation_id, row.run_id)
    assert repair.diagnosis_report_digest == digest(row.report_payload)
    assert repair.native_result_digest == text_digest(image.steps[0].typed_result_payload["content"])
    assert repair.interface_revision == 2 and repair.software_version == "product-2.0"
    assert repair.interface_relative_path == "tlv-interface.json"
    assert repair.allowed_paths == ["cases/tlv-report.json"] and repair.allowed_change_fields == ["request_tlv_hex"]
    assert set(repair.evidence_refs) == tlv_input.REQUIRED_EVIDENCE_KINDS
    assert repair.script_baseline_digest == tlv_input.BASELINE_DIGESTS[repair.script_relative_path]
    path = tmp_path / "repair-input.json"
    path.write_bytes(repair.canonical_bytes())
    assert RepairInput.model_validate_json(path.read_bytes()) == repair
    assert repair.digest() == hashlib.sha256(path.read_bytes()).hexdigest()
    assert (await investigator.repair_input(principal, incident.incident_id, row.investigation_id)).digest() == repair.digest()
    other = await user(clean_database)
    with pytest.raises(CIError, match="CI_NOT_FOUND"):
        await investigator.repair_input(other, incident.incident_id, row.investigation_id)

    async with clean_database.session() as session:
        owner = await session.get(ObjectOwnershipRow, ("RUN", row.run_id))
    interface_id = repair.evidence_refs["TLV_INTERFACE_DEFINITION"].evidence_id
    rejected = (
        (dict(supporting_evidence=[ref for ref in row.report_payload["supporting_evidence"] if ref["evidence_id"] != interface_id]),
            "CI_REPAIR_EVIDENCE_MISSING"),
        (dict(supporting_evidence=[dict(evidence_id=contrast.evidence_manifest[0]["evidence_id"],
            digest=contrast.evidence_manifest[0]["digest"])]), "CI_REPAIR_EVIDENCE_SOURCE_INVALID"),
        (dict(failure_stage="PARSER"), "CI_REPAIR_DIAGNOSIS_MISMATCH"),
        (dict(diagnosis_status="INSUFFICIENT_EVIDENCE", uncertainties=["接口证据不足"]), "CI_REPAIR_INSUFFICIENT_EVIDENCE"),
    )
    for changed, error in rejected:
        bad_row = deepcopy(row)
        bad_row.report_payload = dict(row.report_payload, **changed)
        with pytest.raises(CIError, match=error):
            admit_repair(bad_row, incident, execution, instance, owner)
    for kind, field in (("TLV_INTERFACE_DEFINITION", "definition_content"), ("SCRIPT_BASELINE", "script_content")):
        bad_execution, bad_row = deepcopy(execution), deepcopy(row)
        item = next(e for e in bad_execution.evidence_manifest if e["kind"] == kind)
        item["content"][field] += "\n"
        item["digest"] = digest(item["content"])
        bad_execution.evidence_digest = digest(sorted(bad_execution.evidence_manifest, key=lambda e: e["evidence_id"]))
        next(r for r in bad_row.report_payload["supporting_evidence"] if r["evidence_id"] == item["evidence_id"])["digest"] = item["digest"]
        with pytest.raises(CIError, match="CI_REPAIR_BASELINE_MISMATCH"):
            admit_repair(bad_row, incident, bad_execution, instance, owner)
    bad_execution = deepcopy(execution)
    precheck, *remaining = bad_execution.summary["stages"]
    bad_execution.summary = dict(bad_execution.summary, stages=[dict(precheck, error="FEATURE_FLAG_CONFLICT"), *remaining])
    with pytest.raises(CIError, match="CI_REPAIR_FAILURE_MISMATCH"):
        admit_repair(row, incident, bad_execution, instance, owner)
