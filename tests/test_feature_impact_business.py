"""三组定向验证：因果生命周期、业务边界/真实 HTTP-PG、规则与评分输入。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4, UUID
import socket
import json
import threading
import time
import pytest
import httpx
import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, text
from core.feature_impact.resource_simulation import controls, execute
from core.feature_impact.knowledge import development_input, read, digest
from core.feature_impact.reports import rule_report, validate, RULE_VERSION
from core.feature_impact.service import FeatureImpactService, BusinessError
from core.feature_impact.models import ExecutionRow, InvestigationRow
from core.feature_impact.tools import FeatureEvidenceTool, TOOL_NAMES, AGENT_ID
from core.feature_impact.quality import evaluate
from core.persistence.models import UserRoleRow, RoleRow, UserRow, ObjectOwnershipRow
from core.runtime.memory_scope import MemoryAccessContext
from core.settings import Settings
from tests.test_ci_incident_diagnosis import user


def test_resource_lifecycle_controls():
    results = {}
    for key, value in controls().items():
        result, trace = execute(**value)
        results[key] = result["verdict"]
        assert trace and all("slot_id" not in r for r in result["reports"])
        assert result == execute(**value)[0]
    assert results == dict(C1="PASS", C2="PASS", C3="FAIL", C4="PASS", C5="PASS", C6="PASS")
    changed = controls()["C3"]
    changed["operations"][0]["payload"] = {"count": 91}
    assert execute(**changed)[0]["verdict"] == "PASS"
    with pytest.raises(ValueError):
        execute("NEW", [dict(action="CONSUME", request_id="missing")])


def test_resource_waiting_after_producer_completion():
    operations = [dict(action="SUBMIT", request_id="A", payload=17),
        dict(action="DELIVER", request_id="A", mode="SHARED"),
        dict(action="SUBMIT", request_id="B", payload=91),
        dict(action="CONSUME", request_id="A"), dict(action="COMPLETE", request_id="A")]
    before, _ = execute("OLD", operations[:-1])
    assert before["pending_requests"] == ["B"] and before["reports"] == [dict(request_id="A", payload=17)]
    released, trace = execute("OLD", operations)
    assert released["pending_requests"] == []
    assert [e["event"] for e in trace][-3:] == ["PRODUCER_DONE", "RELEASE", "ALLOCATE_WRITE"]
    completed, _ = execute("OLD", operations + [dict(action="DELIVER", request_id="B", mode="SHARED"),
        dict(action="COMPLETE", request_id="B"), dict(action="CONSUME", request_id="B")])
    assert completed["verdict"] == "PASS" and completed["reports"] == [dict(request_id="A", payload=17), dict(request_id="B", payload=91)]


def test_rule_existing_case_coverage():
    def finding(operations, observation="report equals input", coverage="FULL", remove=False):
        task = development_input()
        for doc in task["documents"]:
            if doc["kind"] == "CASE" and doc["facts"]["feature_id"] == "H1":
                doc["facts"].update(operations=operations, observation=observation, coverage=coverage)
                doc["content_digest"] = digest({k: doc[k] for k in ("title", "content", "facts")})
        if remove:
            task["documents"] = [d for d in task["documents"] if d["source_ref"] != "case:H1:1"]
        task["manifest"] = [{k: d[k] for k in ("source_ref", "version", "chunk", "content_digest")} for d in task["documents"]]
        task["corpus_digest"] = digest(task["manifest"])
        task["input_digest"] = digest({k: v for k, v in task.items() if k != "input_digest"})
        return next(f for f in rule_report(task)[0]["findings"] if f["historical_feature_id"] == "H1")

    full = finding(["submit", "complete", "other_write", "consume"])
    assert full["coverage_class"] == "MUST_TEST" and full["existing_coverage"] == "FULL"
    assert full["recommended_tests"] == [] and "可复用" in full["rationale"]
    assert any(r["source_ref"] == "case:H1:1" for r in full["supporting_evidence"])
    # 实际操作充分时不依赖标签；公共字典操作也可判断，不执行模拟器。
    assert finding(controls()["C3"]["operations"], coverage="PARTIAL")["existing_coverage"] == "FULL"
    mismatched = finding(controls()["C4"]["operations"])
    assert mismatched["existing_coverage"] == "PARTIAL" and "数据所有权" in mismatched["recommended_tests"][0]["coverage_gap"]
    partial = finding(["submit", "consume", "complete", "other_write"])
    assert partial["existing_coverage"] == "PARTIAL" and "关键时序" in partial["recommended_tests"][0]["coverage_gap"]
    missing_observation = finding(["submit", "complete", "other_write", "consume"], observation="")
    assert missing_observation["existing_coverage"] == "PARTIAL" and "观察点" in missing_observation["recommended_tests"][0]["coverage_gap"]
    none = finding([], remove=True)
    assert none["existing_coverage"] == "NONE" and none["recommended_tests"][0]["historical_cases"] == []
    unknown = finding(None)
    assert unknown["existing_coverage"] == "UNKNOWN" and "缺少可判断的操作资料" in unknown["recommended_tests"][0]["coverage_gap"]


@pytest.mark.asyncio
async def test_frozen_scope_review_and_real_http_pg(clean_database, tmp_path):
    service = FeatureImpactService(clean_database)
    owner, other = await user(clean_database), await user(clean_database)
    async with clean_database.transaction() as session:
        role = RoleRow(id=uuid4(), code="USER")
        session.add(role)
        await session.flush()
        session.add_all([UserRoleRow(user_id=p.user_id, role_id=role.id) for p in (owner, other)])
    task = await service.create(owner, str(uuid4()))
    tid = task["task_id"]
    with pytest.raises(BusinessError):
        await service.task(other, tid)
    with pytest.raises(BusinessError):
        await service.tasks(replace(owner, principal_kind="SERVICE"))
    future = deepcopy(task)
    future["documents"][0]["published_at"] = "2099-01-01T00:00:00Z"
    for ref in ("hidden:truth", "/etc/passwd", "other-task:doc"):
        with pytest.raises(ValueError):
            read(task, ref)
    with pytest.raises(ValueError):
        read(future, future["documents"][0]["source_ref"])
    for name in TOOL_NAMES:
        tool = FeatureEvidenceTool(name, service, None)
        key = "query" if name == TOOL_NAMES[0] else "source_ref"
        tool.build_invocation(json.dumps({key: "statistics"}))
        with pytest.raises(Exception):
            tool.build_invocation(json.dumps({key: "statistics", "task_id": tid}))
    # 真正数据库绑定 + Run owner；不调用模型，不伪造 Runtime 成功。
    run_id, sid = str(uuid4()), str(uuid4())
    async with clean_database.transaction() as session:
        session.add(InvestigationRow(run_id=run_id, task_id=tid, session_id=sid, request_id=str(uuid4())))
        session.add(ObjectOwnershipRow(object_type="RUN", object_id=run_id, owner_user_id=owner.user_id, tenant_id=owner.tenant_id))
    context = SimpleNamespace(run_id=run_id, session_id=sid, data=SimpleNamespace(entry_agent_id=AGENT_ID, agent_version="1.1.0"), memory_access_context=MemoryAccessContext.from_principal(owner, AGENT_ID, sid))
    assert "slot" not in json.dumps(await service.bound_evidence(context, {"query": "statistics"}))
    from core.feature_impact.tools import register_tools, register_policies
    from core.feature_impact.agent import definition
    from core.agent_platform.registry import AgentRegistrationBundle, compile_agent_catalog
    from core.agent_platform.contracts import AgentRegistration
    from core.runtime.tool_registry import ToolRegistry
    from core.runtime.tool_governance import ToolPolicyCatalog, ToolGovernanceService, ToolGovernanceContext
    from core.persistence.sync_bridge import SyncPersistenceBridge
    registry = ToolRegistry()
    register_tools(registry, service, SyncPersistenceBridge(asyncio.get_running_loop()))
    bundle = AgentRegistrationBundle(registrations=(AgentRegistration(definition()),), tool_grants={n: frozenset({AGENT_ID}) for n in TOOL_NAMES})
    compiled = compile_agent_catalog(bundle, actual_tool_names=registry.registered_names,
        actual_tool_registrations={r.descriptor.name: r for r in registry.startup_registrations})
    registry.freeze()
    catalog = ToolPolicyCatalog(tool_registry=registry, agent_registry=compiled.agent_registry)
    register_policies(catalog); catalog.freeze()
    governance = ToolGovernanceService(catalog, compiled.agent_registry)
    for name, arguments in zip(TOOL_NAMES, ({"query": "statistics"}, {"source_ref": "history:H1:1"})):
        registered = registry.require(name)
        invocation = registered.adapter.build_invocation(json.dumps(arguments))
        gc = ToolGovernanceContext(AGENT_ID, run_id, "step")
        assert governance.authorize_tool(gc, registered).outcome.value == "ALLOW"
        assert governance.evaluate_invocation(gc, registered, invocation, registered.adapter.spec).outcome.value == "ALLOW"
        output = await asyncio.to_thread(registered.adapter.invoke_once, invocation,
            SimpleNamespace(run_context=context, remaining_seconds=lambda: 10, raise_if_cancelled=lambda: None))
        assert "content_digest" in output.content and "internal_trace" not in output.content
    context.memory_access_context = MemoryAccessContext.from_principal(other, AGENT_ID, sid)
    with pytest.raises(BusinessError):
        await service.bound_evidence(context, {"query": ""})
    # 实际 TCP/HTTP，使用生产业务 app、JWT Owner 与真实 PG，非 ASGITransport。
    from core.feature_impact.app import create_app
    private = Ed25519PrivateKey.generate()
    settings = replace(Settings.load(), database_url=clean_database.config.url,
        jwt_public_key=private.public_key(), jwt_issuer="feature-test", jwt_audience="feature-test", rate_limit_enabled=False)
    app = create_app(settings)
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(.05)
    assert server.started, "真实业务应用未启动"
    def token(p):
        return jwt.encode(dict(iss="feature-test", aud="feature-test", sub=str(p.user_id), roles=["USER"], jti=uuid4().hex,
            iat=p.issued_at, nbf=p.issued_at, exp=p.expires_at), private, algorithm="EdDSA")
    evidence = dict(task_id=tid, transport="TCP_HTTP", database="POSTGRESQL", model_calls=0)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": "Bearer " + token(owner)}, trust_env=False) as client:
            async def post(path, body, status=200):
                response = await client.post("/api/feature-impact" + path, json=body)
                assert response.status_code == status, response.text
                return response.json()
            assert (await client.get("/api/feature-impact/tasks")).status_code == 200
            report = await post(f"/tasks/{tid}/rule-reports", dict(request_id=str(uuid4())))
            assert report["status"] == "SEALED" and report["candidate_kind"] == "RULE" and report["run_id"] is None
            invalid = await post(f"/tasks/{tid}/reports", dict(request_id=str(uuid4()), raw_output="not JSON"))
            assert invalid["status"] == "REJECTED" and invalid["raw_output"] == "not JSON" and invalid["rejection"]
            human_report = deepcopy(report["report"])
            for finding in human_report["findings"]:
                for ref in finding["supporting_evidence"] + finding["counter_evidence"]:
                    ref["basis"] = "TASK_INPUT"
            submitted = await post(f"/tasks/{tid}/reports", dict(request_id=str(uuid4()), raw_output=json.dumps(human_report)))
            assert submitted["status"] == "SEALED" and submitted["candidate_kind"] == "HUMAN" and submitted["run_id"] is None
            old_report = report
            report = await post(f"/tasks/{tid}/rule-reports", dict(request_id=str(uuid4())))
            await post(f'/reports/{old_report["report_id"]}/reviews', dict(request_id=str(uuid4()), report_version=old_report["report_version"], report_digest=old_report["report_digest"], decisions=[dict(finding_id=f["finding_id"], decision="ACCEPT", reason="旧报告") for f in old_report["report"]["findings"]]), 409)
            review = dict(request_id=str(uuid4()), report_version=report["report_version"], report_digest="wrong", decisions=[dict(finding_id=f["finding_id"], decision="ACCEPT" if f["recommended_tests"] else "NEEDS_CLARIFICATION", reason="人工审核资料与时序") for f in report["report"]["findings"]])
            await post(f'/reports/{report["report_id"]}/reviews', review, 409)
            review["report_digest"] = report["report_digest"]
            plan = await post(f'/reports/{report["report_id"]}/reviews', review)
            assert plan == await post(f'/reports/{report["report_id"]}/reviews', review)
            async with clean_database.session() as session:
                assert (await session.execute(select(ExecutionRow))).first() is None
            body = dict(request_id=str(uuid4()), plan_digest=plan["plan_digest"], test_id="test-H1")
            result = await post(f'/coverage-plans/{plan["plan_id"]}/executions', body)
            assert result == await post(f'/coverage-plans/{plan["plan_id"]}/executions', body)
            assert result["verdict"] == "FAIL" and result["reports"][0]["payload"] == {"count": 91}
            saved = (await client.get(f'/api/feature-impact/executions/{result["execution_id"]}')).json()
            assert saved == result and "internal_trace" not in saved
            reopened = (await client.get(f'/api/feature-impact/reports/{report["report_id"]}')).json()
            assert reopened["coverage_plan"] == plan and reopened["executions"][0]["execution_id"] == result["execution_id"]
            response = await client.get(f'/api/feature-impact/executions/{result["execution_id"]}', headers={"Authorization": "Bearer " + token(other)})
            assert response.status_code == 404
            await post(f"/tasks/{tid}/rule-reports", dict(request_id=str(uuid4())), 409)
            evidence.update(report_id=report["report_id"], report_digest=report["report_digest"], plan_id=plan["plan_id"], execution_id=result["execution_id"], verdict=result["verdict"])
        async with clean_database.session() as session:
            assert (await session.get(ExecutionRow, result["execution_id"])).internal_trace
        print("FEATURE_HTTP_PG_EVIDENCE=" + json.dumps(evidence))
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 10)
        sock.close()
        assert not thread.is_alive()


def test_rule_and_report_evidence_contract():
    task = development_input()
    raw, source = rule_report(task)
    assert raw == rule_report(task)[0]
    assert [f["coverage_class"] for f in raw["findings"]] == ["MUST_TEST", "MUST_TEST", "REASONABLE_CHECK", "UNSUPPORTED", "INSUFFICIENT_EVIDENCE"]
    changed = deepcopy(task)
    for d in changed["documents"]:
        if d["kind"] in {"HISTORY", "CASE"}:
            d["facts"]["feature_id"] = "renamed-" + d["facts"]["feature_id"]
            d["content_digest"] = digest({k: d[k] for k in ("title", "content", "facts")})
    changed["manifest"] = [{k: d[k] for k in ("source_ref", "version", "chunk", "content_digest")} for d in changed["documents"]]
    assert [f["coverage_class"] for f in rule_report(changed)[0]["findings"]] == [f["coverage_class"] for f in raw["findings"]]
    broken = deepcopy(raw); broken["findings"][0]["supporting_evidence"][0]["content_digest"] = "0" * 64
    with pytest.raises(ValueError):
        validate(broken, task, kind="RULE", reads=source["read_sources"])
    with pytest.raises(ValueError):
        validate(raw, task, kind="RULE", reads=[])
    body = dict(protocol="feature-impact-request.v1", project_id=str(uuid4()), run_id=str(uuid4()), attempt_id=str(uuid4()), request_id=str(uuid4()), case_id="development", frozen_input_digest="0" * 64,
        input=dict(task_id=str(uuid4()), snapshot=task, candidate_kind="RULE", candidate_version=RULE_VERSION, report=None))
    body["frozen_input_digest"] = digest(body["input"])
    result = evaluate(body)
    assert result["report"] == raw and result["envelope"]["run_id"] is None
    assert result["source_record"] == source
    # 仅构造 validator fixture，验证 FINAL receipt 与 TASK_INPUT/EVIDENCE_READ 的区别；不是 Agent 业务成果。
    from core.feature_impact.agent import validate_result, task_prompt
    from core.runtime.snapshot_serialization import sha256_digest, text_digest
    raw_task_input = deepcopy(raw)
    for finding in raw_task_input["findings"]:
        for ref in finding["supporting_evidence"] + finding["counter_evidence"]:
            ref["basis"] = "TASK_INPUT"
    text_output = json.dumps(raw_task_input)
    binding = SimpleNamespace(run_id="fixture-run", session_id="fixture-session")
    payload = dict(content=text_output, producer_agent_id=AGENT_ID)
    frame = SimpleNamespace(run_id=binding.run_id, frame_id="f", frame_role="ROOT", step_id="s", execution_agent_id=AGENT_ID, result_decision_id="final",
        context_seed=dict(messages=[dict(role="user", content=task_prompt(task))]),
        final_delivery_receipt=dict(status="DELIVERED", decision_id="final", result_digest=text_digest(text_output)))
    frame.seed_digest = sha256_digest(frame.context_seed)
    image = SimpleNamespace(root=SimpleNamespace(run_id=binding.run_id, status="SUCCEEDED", execution_policy="durable-agent-loop.v2",
        resume_input=dict(resolved_agent_id=AGENT_ID, agent_version="1.1.0", session_id=binding.session_id, user_query=task_prompt(task))),
        loop_frames=[frame], steps=[SimpleNamespace(run_id=binding.run_id, status="SUCCEEDED", step_id="s", typed_result_payload=payload, result_digest=sha256_digest(payload))],
        loop_iterations=[SimpleNamespace(run_id=binding.run_id, step_id='s', frame_id="f", decision_id="final", decision_kind="FINAL", iteration_no=1, state="CLOSED")], tool_invocations=[])
    final_raw, proofs = validate_result(binding, image, [], task)
    validate(json.loads(final_raw), task, kind="AGENT", reads=proofs["read_sources"], task_inputs=proofs["task_input_sources"])
    with pytest.raises(ValueError):
        validate(raw, task, kind="AGENT", reads=[], task_inputs=proofs["task_input_sources"])
    frame.final_delivery_receipt["result_digest"] = "forged"
    with pytest.raises(ValueError):
        validate_result(binding, image, [], task)
