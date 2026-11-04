"""受信双模式评测使用真实 Run/Tool/Model/Receipt 的定向合同验证。"""
import json
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from fastapi import HTTPException
from core.agent_platform.adaptive_evaluation import Configuration, EvaluationRequest, AdaptiveEvaluationService, PROTOCOL
from core.agent_platform.ci_diagnosis import CIInvestigationService
from core.agent_platform.application import AgentApplicationService
from core.chat_service import ChatService
from core.runtime.adaptive_execution_evidence import strategy, build_evidence
from core.runtime.agent_loop.quality_contract import catalog_digest
from core.runtime.model_invocation import ModelAdapterResponse
from core.runtime.budget import BudgetUsage
from tests.test_ci_status_routing import setup_query
from tests.test_agent_loop_runtime import native
from dataclasses import replace


def configuration(q, *, slice="STATUS"):
    project, case = str(uuid4()), str(uuid4())
    detail = dict(execution_id=q.execution.execution_id, state_revision=q.execution.state_revision, summary=q.execution.summary)
    input = dict(slice=slice, incident_id=q.incident.incident_id, execution_id=q.execution.execution_id, execution_digest=catalog_digest(detail))
    frozen = dict(case_version="public-v1", case_digest=catalog_digest({"case": case}), dataset_digest=catalog_digest({"dataset": "public-v1"}), input=input)
    config = Configuration.model_validate(dict(version="adaptive-evaluation-config.v1", project_id=project,
        principal_id=str(q.principal.user_id), tenant_id=q.principal.tenant_id, cases={case: frozen}))
    investigator = CIInvestigationService(q.incidents, q.app, q.repo, None, q.authorization, real_model=False)
    service = AdaptiveEvaluationService(config, q.incidents, q.app, investigator, q.repo)
    return service, dict(protocol_version=PROTOCOL, project_id=project, case_id=case, **{k: v for k, v in frozen.items() if k != "input"},
        input=input, input_digest=catalog_digest(input), evaluation_run_id=str(uuid4()), request_id=str(uuid4()),
        attempt_id=str(uuid4()), group="BASELINE", policy_digest=strategy("BASELINE")["digest"], timeout_milliseconds=180000)


def status_responses(q):
    tool = native("ci_query_execution_status", "status-call")
    tool = replace(tool, native_tool_call=None, native_tool_calls=(tool.native_tool_call,))
    value = dict(schema_version="ci-status.v1", kind="STATUS", incident_id=q.incident.incident_id,
        execution_id=q.execution.execution_id, state_revision=q.execution.state_revision, summary=q.execution.summary)
    return [tool, ModelAdapterResponse(json.dumps(value), finish_reason="stop", tool_call_count=0)]


@pytest.mark.asyncio
async def test_adaptive_status_receipts_modes_failure_and_unknown_usage(clean_database, tmp_path):
    q = await setup_query(clean_database, tmp_path)
    q.model.responses[:] = [replace(r, actual_usage=BudgetUsage(input_tokens=7, output_tokens=3, total_tokens=10)) for r in status_responses(q)]
    service, raw = configuration(q)
    request = EvaluationRequest.model_validate(raw)
    response = await service.execute(q.principal, request)
    image = await q.repo.load(str(request.attempt_id))
    assert response is not None, (image.root if image else None)
    loop = json.loads(response)
    assert loop["mode_evidence"]["fast"] is None and len(loop["mode_evidence"]["loop"]["frames"]) == 1
    assert loop["metrics"]["model_attempts_started"] == 2 and loop["metrics"]["tool_invocations_started"] == 1
    assert loop["metrics"]["provider_usage"] == dict(input_tokens=14, output_tokens=6, total_tokens=20)
    assert loop["metrics"]["actual_cost"] is None
    raw.update(group="CANDIDATE_A", policy_digest=strategy("CANDIDATE_A")["digest"], attempt_id=str(uuid4()), request_id=str(uuid4()))
    request = EvaluationRequest.model_validate(raw)
    fast = await service.execute(q.principal, request)
    assert await service.execute(q.principal, request) == fast
    fast = json.loads(fast)
    assert fast["metrics"]["model_attempts_started"] == 0 and fast["metrics"]["tool_invocations_started"] == 1
    assert fast["mode_evidence"]["loop"] is None
    assert json.loads(fast["business"]["output"]) == json.loads(loop["business"]["output"])
    image = await q.repo.load(str(request.attempt_id))
    receipt = await service.receipts.read(str(request.attempt_id))
    replay = build_evidence(image, receipt.correlation, receipt.execution_manifest["strategy"], {**fast["business"]})
    assert replay["metrics"]["tool_invocations_started"] == 1
    with pytest.raises(HTTPException):
        await service.execute(replace(q.principal, user_id=uuid4()), request)
    with pytest.raises(HTTPException):
        await service.execute(q.principal, request.model_copy(update={"policy_digest": "0" * 64}))
    raw.update(group="BASELINE", policy_digest=strategy("BASELINE")["digest"], attempt_id=str(uuid4()), request_id=str(uuid4()))
    failed = json.loads(await service.execute(q.principal, EvaluationRequest.model_validate(raw)))
    assert failed["runtime"]["status"] == "FAILED" and failed["metrics"]["model_attempts_started"] >= 1
    assert failed["business"]["output"] is None and failed["metrics"]["provider_usage"] is None
    (tmp_path / "fast_evidence.json").write_text(json.dumps(fast), encoding="utf-8")
    (tmp_path / "failed_evidence.json").write_text(json.dumps(failed), encoding="utf-8")


@pytest.mark.asyncio
async def test_adaptive_diagnosis_exports_real_stop_and_controlled_baseline(clean_database, tmp_path):
    from tests.test_ci_adaptive_stop import build
    q = await build(clean_database, tmp_path)
    q.app = AgentApplicationService(ChatService(q.factory._router, coordinated_runtime_factory=q.factory), q.factory._agent_registry)
    from core.auth import ObjectAuthorizationService
    q.authorization = ObjectAuthorizationService(clean_database)
    service, raw = configuration(q, slice="DIAGNOSIS")
    raw.update(group="CANDIDATE_B", policy_digest=strategy("CANDIDATE_B")["digest"])
    evidence = json.loads(await service.execute(q.principal, EvaluationRequest.model_validate(raw)))
    assert evidence["business"]["outcome"] == "SUPPORTED", evidence
    frame = evidence["mode_evidence"]["loop"]["frames"][0]
    image = await q.repo.load(raw["attempt_id"])
    assert frame["stop_decision"] == image.loop_frames[0].adaptive_decision
    assert frame["stop_decision"]["reason"] == "COVERAGE_SUFFICIENT"
    q.model.requests.clear()
    raw.update(group="CANDIDATE_A", policy_digest=strategy("CANDIDATE_A")["digest"], attempt_id=str(uuid4()), request_id=str(uuid4()))
    baseline = json.loads(await service.execute(q.principal, EvaluationRequest.model_validate(raw)))
    assert baseline["runtime"]["status"] == "SUCCEEDED", baseline
    assert baseline["mode_evidence"]["loop"]["frames"][0]["stop_decision"] is None
    assert baseline["comparability"] == evidence["comparability"]
    (tmp_path / "diagnosis_stop.json").write_text(json.dumps(evidence), encoding="utf-8")
