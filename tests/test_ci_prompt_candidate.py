"""候选工件与离线构造检查；不表示实际 Runtime/Provider 已执行。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import pytest

from core.agent_platform.ci_diagnosis import definition
from core.agent_platform.ci_prompt_candidate import prepare, select, freeze, validate, request_binding


def candidate():
    return prepare(definition().instructions + '\n对传播错误保留不确定性，不把下游空结果当成独立根因。',
        feedback_refs=['SYNTHETIC:controlled-feedback'], review_refs=['SYNTHETIC:controlled-review'],
        change_reason='受控示例优化假设，不声明真实审核已发生', created_at='2026-10-29T00:00:00+08:00')


def test_candidate_identity_freeze_and_isolation(tmp_path):
    asset = candidate()
    assert candidate() == asset
    assert asset['manifest']['base_asset_digest'] != asset['manifest']['candidate_asset_digest']
    path = tmp_path / 'candidate.json'
    freeze(path, asset)
    with pytest.raises(FileExistsError):
        freeze(path, asset)
    bad = {**asset, 'instructions': 'tampered'}
    with pytest.raises(ValueError):
        validate(bad)
    def resolve(baseline):
        return select(asset, approved_manifest_digest=asset['manifest_digest'], baseline=baseline)
    with ThreadPoolExecutor(max_workers=2) as pool:
        base, changed = list(pool.map(resolve, [True, False]))
    assert base.instructions == definition().instructions
    assert changed.instructions == asset['instructions']
    assert base.allowed_tools == changed.allowed_tools and base.business_options == changed.business_options
    with pytest.raises(ValueError):
        select(asset, approved_manifest_digest='unapproved')


def test_request_construction_checker_does_not_claim_send():
    from core.agent_router import AgentRouter
    asset = candidate()
    selected = select(asset, approved_manifest_digest=asset['manifest_digest'])
    system = AgentRouter._build_system_prompt(object.__new__(AgentRouter), selected.agent_id, business_definition=selected)
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': 'SYNTHETIC'}]
    receipt = request_binding(asset, messages, run_id='run', attempt_id='attempt',
        frozen_input_digest='input', model_request_digest='request')
    assert receipt['evidence']['evidence_scope'] == 'REQUEST_CONSTRUCTION_ONLY'
    with pytest.raises(ValueError):
        request_binding(asset, [{'role': 'system', 'content': definition().instructions}],
            run_id='run', attempt_id='attempt', frozen_input_digest='input', model_request_digest='request')


def test_startup_bundle_is_pinned_and_does_not_mutate_default(tmp_path):
    from core.agent_platform.registrations import BUSINESS_REGISTRATION_BUNDLE
    from core.agent_platform.ci_prompt_candidate import configured_bundle
    from core.runtime.agent_loop.quality_capture import capture_ci_prompt_request
    from core.runtime.agent_loop.context import _pin_value
    asset = candidate()
    path = tmp_path / 'quality.json'
    config = {'ci_prompt': {'asset': asset, 'approved_manifest_digest': asset['manifest_digest'], 'mode': 'CANDIDATE'}}
    path.write_text(json.dumps(config), encoding='utf-8')
    bundle = configured_bundle(BUSINESS_REGISTRATION_BUNDLE, path)
    selected = next(r.definition for r in bundle.registrations if r.definition.agent_id == definition().agent_id)
    assert selected.instructions == asset['instructions']
    assert next(r.definition for r in BUSINESS_REGISTRATION_BUNDLE.registrations if r.definition.agent_id == selected.agent_id).instructions == definition().instructions
    proof = capture_ci_prompt_request({'agent': _pin_value(selected)}, [{'role':'system','content':selected.instructions}], 'a'*64)
    assert proof['evidence_scope'] == 'DURABLE_REQUEST_PREPARED'
    with pytest.raises(ValueError):
        capture_ci_prompt_request({'agent': _pin_value(selected)}, [{'role':'system','content':definition().instructions}], 'a'*64)
    assert capture_ci_prompt_request({'old':'config'}, [], 'a'*64) is None
    config['ci_prompt']['approved_manifest_digest'] = 'wrong'
    path.write_text(json.dumps(config), encoding='utf-8')
    with pytest.raises(ValueError):
        configured_bundle(BUSINESS_REGISTRATION_BUNDLE, path)


@pytest.mark.parametrize("mode", ["BASELINE", "CANDIDATE"])
def test_startup_loads_pre_wp2_approved_prompt(tmp_path, mode):
    from core.agent_platform.ci_prompt_candidate import configured_bundle
    from core.agent_platform.quality_configuration import check_agent
    from core.agent_platform.registrations import BUSINESS_REGISTRATION_BUNDLE

    # 工件与 Definition 摘要来自 8e25f0e，测试运行时不重建旧批准。
    frozen = json.loads(Path(__file__).with_name("fixtures").joinpath("ci_prompt_pre_wp2_approval.json").read_bytes())
    asset = frozen["asset"]
    approval = frozen["approved_manifest_digest"]
    path = tmp_path / "quality.json"

    def load(candidate_asset, approved_digest):
        config = {"ci_prompt": dict(asset=candidate_asset, approved_manifest_digest=approved_digest, mode=mode)}
        path.write_text(json.dumps(config), encoding="utf-8")
        return configured_bundle(BUSINESS_REGISTRATION_BUNDLE, path)

    bundle = load(asset, approval)
    selected = next(r.definition for r in bundle.registrations if r.definition.agent_id == "ci_incident_investigator")
    pin = selected.business_options["ci_prompt_selection"]
    expected = frozen["selections"][mode]
    assert pin["prompt_digest"] == expected["prompt_digest"]
    assert pin["manifest_digest"] == approval and pin["candidate_id"] == asset["manifest"]["candidate_id"]
    assert selected.agent_version == "1.0.0"
    check_agent({"agents": {selected.agent_id: dict(agent_version="1.0.0", execution_policy="durable-agent-loop.v2",
        definition_digest=expected["definition_digest"])}}, selected)

    with pytest.raises(ValueError, match="CI_PROMPT_NOT_APPROVED"):
        load(asset, "0" * 64)
    tampered = deepcopy(asset)
    tampered["manifest"]["base_asset_digest"] = "0" * 64
    with pytest.raises(ValueError, match="CI_PROMPT_ASSET_PIN_INVALID"):
        load(tampered, approval)
    manifest = asset["manifest"]
    unapproved = prepare(asset["instructions"] + "\n增加一项未审批的检查。",
        feedback_refs=manifest["source_feedback_refs"], review_refs=manifest["source_review_refs"],
        change_reason=manifest["change_reason"], created_at=manifest["created_at"])
    with pytest.raises(ValueError, match="CI_PROMPT_NOT_APPROVED"):
        load(unapproved, approval)
    with pytest.raises(ValueError, match="CI_PROMPT_ASSET_PIN_INVALID"):
        load({**asset, "instructions": unapproved["instructions"]}, approval)


@pytest.mark.asyncio
async def test_evaluation_deployment_blocks_human_generic_entry(monkeypatch):
    import server
    from types import SimpleNamespace as NS
    from fastapi import Request, Response

    async def authenticate(self, authorization):
        return NS(principal_kind='HUMAN', authz_domain_id='synthetic-human', roles=frozenset({'USER'}))

    async def check(self, domain):
        return NS(allowed=True)

    async def next_handler(request):
        return Response(status_code=204)

    monkeypatch.setattr(server.AuthService, 'authenticate', authenticate)
    monkeypatch.setattr(server.RedisTokenBucketRateLimiter, 'check', check)
    state=NS(ci_evaluation_only=True,auth_service=object.__new__(server.AuthService),
        rate_limiter=object.__new__(server.RedisTokenBucketRateLimiter))
    for path in ('/api/ci/incidents', '/api/v1/chat'):
        request=Request({'type':'http','app':NS(state=state),'path':path,'headers':[]})
        assert (await server.request_id_and_auth_middleware(request,next_handler)).status_code==403
    state.ci_evaluation_only=False
    request=Request({'type':'http','app':NS(state=state),'path':'/api/v1/chat','headers':[]})
    assert (await server.request_id_and_auth_middleware(request,next_handler)).status_code==204


@pytest.mark.asyncio
async def test_fenced_model_start_persists_final_prompt_binding(clean_database):
    from dataclasses import replace
    from uuid import uuid4
    from core.persistence.repositories.agent_quality import AgentQualityReceiptRepository
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.runtime.run_control import DurableRunControlService
    from core.runtime.execution_aggregate import ExecutionRootInput
    from core.runtime.agent_loop.context import _pin_value
    from core.runtime.planning import create_single_step_plan, TaskCapabilityRequirements
    from tests.test_ci_incident_diagnosis import user
    from core.runtime.agent_loop.quality_contract import catalog_digest, quality_runtime_config_digest
    actor = replace(await user(clean_database), principal_kind='SERVICE')
    asset = candidate()
    selected = select(asset, approved_manifest_digest=asset['manifest_digest'])
    selected = replace(selected, business_options={**selected.business_options, 'ci_prompt_selection': {
        'schema_version':'ci-prompt-selection.v1', 'candidate_id':asset['manifest']['candidate_id'],
        'manifest_digest':asset['manifest_digest'], 'mode':'CANDIDATE',
        'prompt_digest':catalog_digest(selected.instructions)}})
    run = str(uuid4())
    receipts = AgentQualityReceiptRepository(clean_database)
    pinned = {'agent':_pin_value(selected)}
    await receipts.admit(correlation={'agent_id':selected.agent_id,'run_id':run,'attempt_id':run,'request_id':str(uuid4()),
        'evaluation_project_id':str(uuid4()),'evaluation_run_id':str(uuid4())}, principal=actor,
        manifest={'agents':{selected.agent_id:{'execution_policy':'durable-agent-loop.v1',
            'runtime_config_digest':quality_runtime_config_digest(pinned)}}}, profile={'fact_keys':[]})
    control = DurableRunControlService(clean_database)
    repository = DurableExecutionRepository(clean_database, control)
    lease = await control.claim(run, 'ci-prompt-test')
    from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
    await MemoryLifecycleRepository(clean_database).bind_run(lease, None, [], [])
    messages = [{'role':'system','content':selected.instructions}]
    await repository.initialize(ExecutionRootInput(run_id=run,
        resume_input={'agent_loop':{'context_seed':{'messages':messages},
            'pinned_config':{'agent':_pin_value(selected)},'loop_policy':{'max_iterations':3}}},
        plan=create_single_step_plan(selected.agent_id, TaskCapabilityRequirements()), absolute_deadline=None,
        budget_totals={'max_model_calls':3},budget_reserved={},budget_consumed={},
        execution_policy='durable-agent-loop.v1'),lease=lease)
    await repository.start_step(lease,step_id='answer',plan_version=1)
    frame,_=await repository.load_loop(lease,'answer')
    await repository.open_loop_iteration(lease,frame.frame_id,resume_generation=0,row_version=frame.row_version,
        context_input_digest='a'*64,context_cursor=0)
    kwargs=dict(step_id='answer',attempt=1,model_attempt=1,request_digest='a'*64,
        provider_kind='synthetic',profile_identity='synthetic',loop_frame_id=frame.frame_id,
        loop_iteration_no=1,resume_generation=0,invocation_group=f'{frame.frame_id}:1',
        operation_id='model:1',reserved_usage={'model_calls':1})
    with pytest.raises(ValueError, match='CI_PROMPT_ACTUAL_INSTRUCTIONS_MISMATCH'):
        await repository.start_model_attempt(lease,**kwargs,evaluation_messages=[{'role':'system','content':'wrong'}])
    assert not (await receipts.read(run)).evaluation_capture
    await repository.start_model_attempt(lease,**kwargs,evaluation_messages=messages)
    captured=(await receipts.read(run)).evaluation_capture['model:answer:1:1']['ci_prompt_binding']
    assert captured['prompt_digest']==catalog_digest(selected.instructions)
    assert captured['messages_digest']==catalog_digest(messages)
    assert captured['evidence_scope']=='DURABLE_REQUEST_PREPARED'
