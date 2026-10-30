"""公开、无PG/模型的成对CI协议向量及原Owner构造验证。"""
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from uuid import uuid4
import pytest
from pydantic import ValidationError
from core.ci_simulation.execution import initial_state, execute
from core.incidents.evaluation_input import CaseInput, FrozenPair, same_intent
from core.incidents.evaluation_sources import build_source_binding, semantic_digest, KINDS
from core.runtime.agent_loop.quality_contract import catalog_digest, canonical_bytes
from core.runtime.execution_aggregate import canonical_payload_digest as digest
from core.runtime.tool_contract import (ToolExecutionResult, ToolOutput, ToolExecutionStatus,
    ToolSideEffectState, RetryDisposition, canonical_json_digest)
from core.runtime.tool_idempotency import _committed_result_payload
from core.agent_platform.ci_synthetic_evaluation import CIRequestV2,PROTOCOL_V2,QUERY_V2
from core.runtime.request_counting_admission import compile_exception, match_request, issue_record,validate_record,LIMITS
from core.agent_platform.ci_prompt_candidate import prepare,select as choose
from core.auth import EVALUATION_EXECUTE_SCOPE,CI_SYNTHETIC_EVALUATE_SCOPE


def public_input():
    state=initial_state('report_pipeline_failure')
    return dict(schema_version='ci-synthetic-case-input.v2',template_id='operator-frozen-ci',
        producer_version='ci-simulation.v2',initial_state=state,state_digest=catalog_digest(state))


def public_vector(*,include_owner=False):
    visible=public_input();typed=CaseInput.model_validate(visible)
    input_digest=catalog_digest(visible);review=str(uuid4());source_digest=catalog_digest('public independent review')
    # 在实际受评测执行之前，从公开独立审核材料构造稳定要求，标签不是模型结果。
    preview=execute(typed.state(),str(uuid4()))
    semantics={e['kind']:semantic_digest(e['kind'],e['content'],preview['summary']['execution_id'])
        for e in preview['evidence_manifest'] if e['kind'] in KINDS and e['availability']=='AVAILABLE'}
    gt=dict(schema_version='ci-diagnosis-ground-truth.v2',diagnosis_status='SUPPORTED',failure_stage='PARSER',
        required_owner_role='TOOL_MAINTAINER',input_digest=input_digest,review_id=review,reviewer='public-reviewer',
        required_sources=[dict(logical_source_key=k.lower(),kind=k,source_semantics_version='ci-diagnosis-source-semantics.v1',
            expected_semantic_digest=semantics[k],upstream_source_ref='review-item:'+review,upstream_source_digest=source_digest)
            for k in ('PARSER_LOG','ENVIRONMENT_RECORD','CASE_REQUIREMENTS')])
    metadata=dict(ci_diagnosis=dict(schema_version='ci-diagnosis-lineage.v1',purpose='GOLDEN',source_digest=source_digest,
        mechanism_family_digest=catalog_digest('public parser family'),input_digest=input_digest,ground_truth_digest=catalog_digest(gt),
        review_id=review,reviewer='public-reviewer',candidate_developer='public-developer',feedback_id='',projection_digest='',
        attribution_digest='',operator='',independence_audit_digest='',access_control_audit_digest='',exposure='PUBLIC',retired=False,developer_readable=True))
    samples=[]
    case_id,project,plan,dataset=map(lambda _:str(uuid4()),range(4))
    for mode in ('BASELINE','CANDIDATE'):
        run,frame,incident,execution_id=map(lambda _:str(uuid4()),range(4))
        frozen=dict(case_id=case_id,case_version='public-v2',case_digest='a'*64,input_digest=input_digest,dataset_id=dataset,
            dataset_version='public-v2',plan_id=plan,plan_version='public-v2',source_ref='review-item:'+review,visible_input=visible)
        quality=dict(agent_id='ci_incident_investigator',run_id=run,evaluation_project_id=project,evaluation_run_id=str(uuid4()),
            attempt_id=run,request_id=str(uuid4()),case_id=case_id,case_version='public-v2',case_digest='a'*64,
            execution_manifest_digest='b'*64,capture_profile_id='public-ci',capture_profile_version=1,capture_profile_digest='c'*64,
            timeout_milliseconds=1000,query_digest=hashlib.sha256(QUERY_V2.encode()).hexdigest())
        binding=dict(contract_version=PROTOCOL_V2,run_id=run,execution_id=execution_id,incident_id=incident,
            frozen_ci=frozen,plan_digest='d'*64,mode=mode,quality_correlation=quality)
        result=execute(typed.state(),execution_id)
        row=NS(execution_id=execution_id,state_revision=1,**result)
        entries={e['kind']:e for e in result['evidence_manifest']}
        calls=[('ci_get_execution_context',{}),*[('ci_read_execution_evidence',{'evidence_id':entries[k]['evidence_id']}) for k in ('PARSER_LOG','ENVIRONMENT_RECORD','CASE_REQUIREMENTS')]]
        tools=[];actions=[]
        for number,(name,args) in enumerate(calls):
            invocation=str(uuid4());action=str(uuid4());call='call-'+str(number)
            iteration,ordinal=((1,0),(2,0),(2,1),(3,0))[number]
            if name=='ci_get_execution_context':
                value=dict(incident_id=incident,execution_id=execution_id,environment_id=row.environment_id if hasattr(row,'environment_id') else typed.state()['environment']['environment_id'],
                    case_id=typed.state()['test_case']['case_id'],outcome=result['outcome'],completed_at=result['completed_at'].isoformat(),
                    evidence=[{k:e[k] for k in ('evidence_id','kind','availability','media_type','digest')} for e in result['evidence_manifest']])
            else:value=dict(incident_id=incident,**next(e for e in result['evidence_manifest'] if e['evidence_id']==args['evidence_id']))
            content=json.dumps(value,ensure_ascii=False,allow_nan=False)
            n=len(content.encode());now=datetime.now(timezone.utc)
            output=ToolOutput('application/json',content,n,n,False,hashlib.sha256(content.encode()).hexdigest())
            completed=ToolExecutionResult(invocation,str(uuid4()),name,ToolExecutionStatus.SUCCEEDED,output,'公开中文来源',
                ToolSideEffectState.NOT_STARTED,False,RetryDisposition.UNSAFE,None,now,now,0)
            payload,owner_digest=_committed_result_payload(completed)
            recipe=dict(tool_name=name,arguments=args)
            projection=dict(outcome='SUCCEEDED',content=content)
            tools.append(NS(invocation_id=invocation,run_id=run,tool_name=name,state='COMMITTED',arguments_digest=canonical_json_digest(args),
                committed_result=payload,committed_result_digest=owner_digest))
            actions.append(NS(action_id=action,run_id=run,frame_id=frame,iteration_no=iteration,ordinal=ordinal,protocol_call_id=call,
                invocation_id=invocation,recipe=recipe,recipe_digest=digest(recipe),source_digest=owner_digest,result_projection=projection,result_digest=digest(projection)))
        exported=build_source_binding(binding,row,NS(root=NS(run_id=run),tool_invocations=tools),actions)
        sample=dict(binding=binding,source_binding=exported,source_binding_digest=catalog_digest(exported),frame_id=frame)
        if include_owner:sample['_owner']=(row,tools,actions)
        samples.append(sample)
    limited=execute(initial_state('limited_diagnostics'),str(uuid4()))
    branches=[dict(kind=e['kind'],execution_id=e['execution_id'],content=e['content'],semantic_digest=semantic_digest(e['kind'],e['content'],e['execution_id']))
        for e in limited['evidence_manifest'] if e['kind'] in ('ENVIRONMENT_RECORD','COMPARISON_LOG')]
    return dict(schema_version='public-ci-pair-vector.v1',visible_input=visible,ground_truth=gt,metadata=metadata,semantic_digests=semantics,branches=branches,samples=samples)


def test_public_source_semantics_and_owner_bytes():
    vector=public_vector();a,b=vector['samples']
    for kind in KINDS:
        x=next(e for e in a['source_binding']['evidence_manifest'] if e['kind']==kind)
        y=next(e for e in b['source_binding']['evidence_manifest'] if e['kind']==kind)
        assert x['evidence_id']!=y['evidence_id'] and x['digest']!=y['digest']
        assert semantic_digest(kind,x['content'],x['execution_id'])==semantic_digest(kind,y['content'],y['execution_id'])
    raw=a['source_binding']['tools'][1]['result_canonical_bytes']
    assert '\\u516c' in raw  # 原Tool Owner ensure_ascii=True，保留原始字节。
    assert hashlib.sha256(raw.encode()).hexdigest()==a['source_binding']['tools'][1]['result_digest']
    # 原Producer的正常/缺诊断分支和五类业务初态；仅纯函数，无PG/模型。
    for scenario in ('environment_configuration_failure','stale_case_input','buffer_behavior_change','limited_diagnostics'):
        execution_id=str(uuid4());actual=execute(initial_state(scenario),execution_id)
        for e in actual['evidence_manifest']:
            if e['kind'] in KINDS and e['availability']=='AVAILABLE':
                semantic_digest(e['kind'],e['content'],execution_id)
    parser=next(e for e in a['source_binding']['evidence_manifest'] if e['kind']=='PARSER_LOG')
    for change in ({'extra':None},{'field_revision':True},{'parsed_fields':{'report':'decoded','execution_id':'nested'}}):
        with pytest.raises(ValueError):semantic_digest('PARSER_LOG',{**parser['content'],**change},parser['execution_id'])


def test_visible_input_gt_and_identity_rejections():
    visible=public_input()
    for mutate in (lambda v:v.update(ground_truth={}),lambda v:v['initial_state'].update(expected_diagnosis='PARSER'),
            lambda v:v['initial_state']['environment'].update(sql='SELECT 1'),lambda v:v.update(state_digest='0'*64)):
        bad=deepcopy(visible);mutate(bad)
        with pytest.raises(ValidationError):CaseInput.model_validate(bad)
    vector=public_vector();f=FrozenPair.model_validate(vector['samples'][0]['binding']['frozen_ci'])
    request=NS(contract_version=PROTOCOL_V2,mode='BASELINE',frozen_ci=f)
    prior=vector['samples'][0]['binding']
    assert same_intent(prior,request)
    changed=f.model_copy(update={'input_digest':'f'*64})
    assert same_intent(prior,NS(**{**vars(request),'frozen_ci':changed}))  # 改digest不刷新额度。
    assert not same_intent(prior,NS(**{**vars(request),'mode':'CANDIDATE'}))


def test_pair_authorization_and_original_record_binding():
    vector=public_vector();asset=prepare('公开候选指令',feedback_refs=['public'],review_refs=['public'],change_reason='test',created_at='2026-10-30')
    principal=NS(principal_kind='SERVICE',user_id=str(uuid4()),tenant_id='public-tenant',scopes=frozenset({EVALUATION_EXECUTE_SCOPE,CI_SYNTHETIC_EVALUATE_SCOPE}))
    frozen_policy=None;records=[]
    for sample in vector['samples']:
        body=sample['binding'];mode=body['mode'];definition=choose(asset,approved_manifest_digest=asset['manifest_digest'],baseline=mode=='BASELINE')
        from dataclasses import replace
        pin=dict(schema_version='ci-prompt-selection.v1',candidate_id=asset['manifest']['candidate_id'],manifest_digest=asset['manifest_digest'],mode=mode,prompt_digest=catalog_digest(definition.instructions))
        definition=replace(definition,business_options={**definition.business_options,'ci_prompt_selection':pin})
        from core.runtime.agent_loop.context import _pin_value
        corr=body['quality_correlation'];q={k:v for k,v in corr.items() if k!='query_digest'};q['query']=QUERY_V2
        request=CIRequestV2.model_validate(dict(contract_version=PROTOCOL_V2,quality=q,frozen_ci=body['frozen_ci'],plan_digest=body['plan_digest'],candidate_manifest_digest=asset['manifest_digest'],mode=mode))
        policy=dict(schema_version='e2e-request-counting-policy.v2',strategy='E2E_TEST_UNVERIFIED_REQUEST_COUNTING',execution_env='E2E_TEST',tenant_id=principal.tenant_id,project_id=corr['evaluation_project_id'],principal_id=principal.user_id,operator_id='public-operator',execution_source='OPERATOR_FROZEN_CI',plan_id=body['frozen_ci']['plan_id'],plan_version='public-v2',agent_id=definition.agent_id,execution_policy='durable-agent-loop.v2',profile_id='remote_advanced',provider_kind='deepseek',model_identity='deepseek-flash',loop_limits=LIMITS,max_tokens=4096,provider_timeout_seconds=120)
        assert frozen_policy is None or frozen_policy==policy
        frozen_policy=policy
        auth={k:corr[k] for k in ('evaluation_run_id','attempt_id','request_id','run_id','execution_manifest_digest')}
        auth.update(mode=mode,frozen_ci=body['frozen_ci'],plan_digest=body['plan_digest'],candidate_id=pin['candidate_id'],candidate_manifest_digest=asset['manifest_digest'],prompt_digest=pin['prompt_digest'],runtime_config_digest='e'*64,definition_digest=catalog_digest(_pin_value(definition)))
        trusted=compile_exception(dict(policy=policy,authorization=auth))
        assert match_request(trusted,request,principal,definition)==trusted
        receipt=NS(correlation=request.quality.correlation())
        bound={**body,'manifest_digest':asset['manifest_digest'],'agent_id':definition.agent_id,'source_type':'SYNTHETIC','session_id':str(uuid4())}
        bound['quality_correlation']=receipt.correlation
        record=issue_record(trusted,bound,receipt);assert validate_record(record)
        records.append(record)
        for change in ({'mode':'CANDIDATE' if mode=='BASELINE' else 'BASELINE'},{'plan_digest':'f'*64}):
            with pytest.raises(ValueError):match_request(trusted,request.model_copy(update=change),principal,definition)
    assert records[0]['authorization']!=records[1]['authorization']
    assert records[0]['policy']==records[1]['policy']


@pytest.mark.asyncio
async def test_prepare_uses_original_executor_without_pg_claim(monkeypatch):
    """内存事务替身只验证真实Prepare/execute_instance调用路径，不是PG原子性证据。"""
    from contextlib import asynccontextmanager
    from core.incidents.evaluation import prepare as atomic_prepare
    from core.incidents.models import SyntheticEvaluationBindingRow as Binding
    from core.ci_simulation.models import ScenarioInstanceRow,CIExecutionRow
    from core.persistence.models import UserRow
    from core.persistence.repositories.agent_quality import AgentQualityReceiptRepository
    from core.agent_platform.ci_diagnosis import definition
    rows=[]
    class Result:
        def __init__(self,values):self.values=values
        def scalars(self):return self
        def all(self):return self.values
        def scalar_one_or_none(self):return self.values[0] if self.values else None
    class Session:
        async def get(self,entity,key,**kw):
            if entity is UserRow:
                assert kw.get('with_for_update') is True
                return NS()
            if entity is Binding:return next((r for r in rows if isinstance(r,Binding) and r.run_id==key),None)
            return None
        async def execute(self,statement):
            entity=statement.column_descriptions[0]['entity']
            if entity is Binding:return Result([r for r in rows if isinstance(r,Binding)])
            if entity is ScenarioInstanceRow:return Result([r for r in rows if isinstance(r,ScenarioInstanceRow)][-1:])
            if entity is CIExecutionRow:return Result([])
            raise AssertionError(entity)
        def add(self,row):rows.append(row)
        async def flush(self):pass
    class Database:
        @asynccontextmanager
        async def transaction(self):yield Session()
    async def admitted(*args,**kwargs):return True,NS()
    monkeypatch.setattr(AgentQualityReceiptRepository,'admit_in_transaction',admitted)
    service=NS(database=Database());principal=NS(user_id=str(uuid4()),tenant_id='public')
    vector=public_vector();requests=[]
    for sample in vector['samples']:
        body=sample['binding'];q={k:v for k,v in body['quality_correlation'].items() if k!='query_digest'};q['query']=QUERY_V2
        request=CIRequestV2.model_validate(dict(contract_version=PROTOCOL_V2,quality=q,frozen_ci=body['frozen_ci'],plan_digest=body['plan_digest'],mode=body['mode'],candidate_manifest_digest='e'*64))
        winner,binding=await atomic_prepare(service,principal,request,definition(),{}, {})
        assert winner and binding['plan_digest']==request.plan_digest
        requests.append(request)
    assert len([r for r in rows if isinstance(r,CIExecutionRow)])==2
    assert all(r.current_state==vector['visible_input']['initial_state'] for r in rows if isinstance(r,ScenarioInstanceRow))
    # 同Attempt幂等不重复物化；换Root/摘要不能刷新原意图。
    assert not (await atomic_prepare(service,principal,requests[0],definition(),{}, {}))[0]
    new=uuid4();q=requests[0].quality.model_copy(update={'run_id':new,'attempt_id':new})
    with pytest.raises(ValueError,match='CI_SYNTHETIC_ATTEMPT_LIMIT'):
        await atomic_prepare(service,principal,requests[0].model_copy(update={'quality':q,'plan_digest':'f'*64}),definition(),{}, {})


@pytest.mark.asyncio
async def test_v2_first_seal_uses_owner_and_replays_exact_bytes():
    """直接调用注册的真实GET/finish；内存持久替身不证明PG锁/事务。"""
    from contextlib import asynccontextmanager
    from fastapi import FastAPI,Request,HTTPException
    from fastapi.responses import Response
    from core.agent_platform.ci_synthetic_evaluation import register
    from core.incidents.models import SyntheticEvaluationBindingRow as Binding,InvestigationRow,IncidentRow
    from core.ci_simulation.models import CIExecutionRow,ScenarioInstanceRow
    from core.persistence.models import RuntimeLoopActionRow
    from core.auth import require_scope
    sample=public_vector(include_owner=True)['samples'][0]
    ci,tools,actions=sample['_owner'];payload=sample['binding'];run=payload['run_id']
    principal=NS(principal_kind='SERVICE',user_id=str(uuid4()),tenant_id='public-owner',scopes=frozenset({EVALUATION_EXECUTE_SCOPE,CI_SYNTHETIC_EVALUATE_SCOPE}))
    instance_id,investigation_id,session_id=[str(uuid4()) for _ in range(3)]
    payload.update(instance_id=instance_id,investigation_id=investigation_id,session_id=session_id,
        agent_id='ci_incident_investigator',agent_version='public-v2',report_version=1,source_type='SYNTHETIC')
    ci.instance_id=instance_id
    binding=NS(payload=payload,sidecar=None,tenant_id=principal.tenant_id,principal_id=principal.user_id,
        project_id=payload['quality_correlation']['evaluation_project_id'],execution_id=ci.execution_id,
        incident_id=payload['incident_id'],instance_id=instance_id)
    incident=NS(tenant_id=principal.tenant_id,owner_user_id=principal.user_id,execution_id=ci.execution_id)
    instance=NS(instance_id=instance_id,tenant_id=principal.tenant_id,owner_user_id=principal.user_id)
    investigation=NS(run_id=run,incident_id=binding.incident_id,session_id=session_id,agent_id=payload['agent_id'],report_version=1,runtime_result_digest='e'*64)
    image=NS(root=NS(run_id=run,status='SUCCEEDED',resume_input=dict(session_id=session_id,resolved_agent_id=payload['agent_id'],agent_version=payload['agent_version'])),models=[],tool_invocations=tools)
    class Result:
        def scalars(self):return iter(actions)
    class Session:
        async def get(self,entity,key,**kw):
            if entity is Binding:
                assert key==run
                return binding
            return {InvestigationRow:investigation,IncidentRow:incident,CIExecutionRow:ci,ScenarioInstanceRow:instance}[entity]
        async def execute(self,statement):
            assert statement.column_descriptions[0]['entity'] is RuntimeLoopActionRow
            return Result()
    class Database:
        @asynccontextmanager
        async def transaction(self):yield Session()
        session=transaction
    class Execution:
        database=Database()
        async def load(self,key,**kw):assert key==run;return image
    quality=canonical_bytes({'public':'sealed-quality-fixture'}).decode()
    receipt=NS(sealed_response=quality,sealed_digest=catalog_digest(json.loads(quality)),evaluation_capture={})
    class Receipts:
        async def read(self,key):assert key==run;return receipt
    async def projected(key):assert key==investigation_id
    authorized=[]
    async def authorize(request,execution,value):
        require_scope(principal,EVALUATION_EXECUTE_SCOPE);authorized.append(run)
    app=FastAPI();app.state.ci_investigation_service=NS(project_if_terminal=projected)
    register(app,settings=NS(quality_evaluation_project_id=binding.project_id),trusted=lambda *a:principal,
        repository=lambda request:(Receipts(),Execution()),wire=lambda value,status=200:Response(content=value,status_code=status),
        require_service=lambda:None,bind_run=None,authorize_existing=authorize,final_answer_capture=None)
    request=Request({'type':'http','app':app,'headers':[]})
    endpoint=next(r.endpoint for r in app.routes if getattr(r,'path','')=='/api/runtime/runs/{run_id}/ci-synthetic-evidence/v2')
    instance.owner_user_id=str(uuid4())
    with pytest.raises(HTTPException) as error:await endpoint(run,request)
    assert error.value.status_code==409 and binding.sidecar is None
    instance.owner_user_id=principal.user_id
    first=await endpoint(run,request);body=json.loads(first.body)
    assert body['sidecar']['source_binding']==sample['source_binding']
    assert body['sidecar']['source_binding_digest']==sample['source_binding_digest']
    ci.evidence_manifest=[]  # 已封存后不能用新来源改写同一Sidecar。
    second=await endpoint(run,request)
    assert first.body==second.body and len(authorized)==3
