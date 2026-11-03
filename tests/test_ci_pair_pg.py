"""TEST_SCOPE：真实隔离PG；模拟模型只能证明持久化集成。"""
import asyncio,json
from dataclasses import replace
from types import SimpleNamespace as NS
from uuid import uuid4
import pytest
from sqlalchemy import select,func
from core.incidents.evaluation import prepare
from core.incidents.service import IncidentService
from core.incidents.models import SyntheticEvaluationBindingRow as Binding,IncidentRow,InvestigationRow
from core.ci_simulation.models import ScenarioInstanceRow,CIExecutionRow
from core.persistence.models import RuntimeAgentEvaluationReceiptRow as Receipt
from core.agent_platform.ci_synthetic_evaluation import CIRequestV2,PROTOCOL_V2,QUERY_V2
from core.runtime.agent_loop.quality_contract import catalog_digest
from tests.test_ci_synthetic_evaluation import inputs
from tests.test_ci_pair_protocol import public_input


async def pair_inputs(database):
    actor,definition,old,asset=await inputs(database)
    visible=public_input()
    f=dict(case_id=str(old.quality.case_id),case_version='test-v2',case_digest='a'*64,input_digest=catalog_digest(visible),
        dataset_id=str(uuid4()),dataset_version='test-v2',plan_id=str(uuid4()),plan_version='test-v2',source_ref='test-scope:public-review',visible_input=visible)
    q=old.quality.model_dump(mode='json');q.update(query=QUERY_V2,case_version='test-v2')
    request=CIRequestV2.model_validate(dict(contract_version=PROTOCOL_V2,quality=q,frozen_ci=f,plan_digest='d'*64,candidate_manifest_digest=asset['manifest_digest'],mode='CANDIDATE'))
    return actor,definition,request,asset


@pytest.mark.asyncio
async def test_pair_prepare_pg_atomic_once(clean_database,monkeypatch):
    from core.ci_simulation import service as simulation
    actor,definition,request,_=await pair_inputs(clean_database)
    owner=IncidentService(clean_database)
    original=simulation.execute_instance
    async def after_ci(*a,**kw):
        await original(*a,**kw)
        raise ValueError('TEST_SCOPE_AFTER_CI_ROLLBACK')
    monkeypatch.setattr(simulation,'execute_instance',after_ci)
    with pytest.raises(ValueError,match='TEST_SCOPE_AFTER_CI_ROLLBACK'):await prepare(owner,actor,request,definition,{}, {})
    tables=(ScenarioInstanceRow,CIExecutionRow,IncidentRow,InvestigationRow,Binding,Receipt)
    async with clean_database.session() as s:
        assert all([await s.scalar(select(func.count()).select_from(t))==0 for t in tables])
    monkeypatch.setattr(simulation,'execute_instance',original)
    candidates=[]
    for _ in range(3):
        run=uuid4();candidates.append(request.model_copy(update={'quality':request.quality.model_copy(update={'run_id':run,'attempt_id':run,'request_id':uuid4()}),'plan_digest':'e'*64}))
    results=await asyncio.gather(*(prepare(owner,actor,r,definition,{}, {}) for r in candidates),return_exceptions=True)
    winners=[(i,x) for i,x in enumerate(results) if not isinstance(x,Exception)]
    assert len(winners)==1 and all(isinstance(x,ValueError) for x in results if isinstance(x,Exception))
    i,(_,binding)=winners[0];request=candidates[i]
    assert not (await prepare(owner,actor,request,definition,{}, {}))[0]
    for field,value in [('plan_digest','f'*64),('quality',request.quality.model_copy(update={'request_id':uuid4()}))]:
        with pytest.raises(ValueError):await prepare(owner,actor,request.model_copy(update={field:value}),definition,{}, {})
    run=uuid4();baseline=request.model_copy(update={'mode':'BASELINE','quality':request.quality.model_copy(update={'run_id':run,'attempt_id':run,'request_id':uuid4()})})
    _,second=await prepare(owner,actor,baseline,definition,{}, {})
    async with clean_database.session() as s:
        assert all([await s.scalar(select(func.count()).select_from(t))==2 for t in tables])
        a=await s.get(CIExecutionRow,binding['execution_id']);b=await s.get(CIExecutionRow,second['execution_id'])
        left=dict(a.execution_snapshot);right=dict(b.execution_snapshot)
        left['report']={**left['report'],'report_artifact_id':'frozen'};right['report']={**right['report'],'report_artifact_id':'frozen'}
        assert left==right
        assert {e['evidence_id'] for e in a.evidence_manifest}.isdisjoint({e['evidence_id'] for e in b.evidence_manifest})
        for payload in (binding,second):
            incident=await s.get(IncidentRow,payload['incident_id']);investigation=await s.get(InvestigationRow,payload['investigation_id'])
            assert incident.execution_id==payload['execution_id'] and investigation.run_id==payload['run_id'] and investigation.incident_id==incident.incident_id
    print('PG_PREPARE: rollback=PASS competing_attempts=3 winners=1 idempotency=PASS independent_modes=2 snapshot_equal=PASS')


@pytest.mark.asyncio
async def test_pair_source_first_seal_pg(clean_database,tmp_path):
    await assemble_ci_pair_test_runtime(clean_database,tmp_path)


async def assemble_ci_pair_test_runtime(clean_database,tmp_path,exchange=None):
    """SYNTHETIC 模型适配器经过原Runtime/Tool提交；不是远程模型完成证明。"""
    from fastapi import FastAPI,Request,HTTPException
    from fastapi.responses import Response
    from core.agent_platform.registry import AgentRegistrationBundle
    from core.agent_platform.contracts import AgentRegistration
    from core.agent_platform.ci_diagnosis_tools import CIDiagnosisTool,TOOL_NAMES
    from core.agent_platform.ci_diagnosis import CIInvestigationService
    from core.agent_platform.ci_synthetic_evaluation import register
    from core.agent_platform.quality_configuration import runtime_build_digest
    from core.persistence.sync_bridge import SyncPersistenceBridge
    from core.persistence.memory import PostgresMemoryManager,PostgresMemoryManagerBridge
    from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
    from core.persistence.repositories.agent_quality import AgentQualityReceiptRepository
    from core.runtime.agent_loop.context import pinned_loop_config,_pin_value
    from core.runtime.execution_aggregate import canonical_payload_digest as digest
    from core.runtime.generation_evidence import FinalAnswerEvidenceV1
    from core.runtime.model_invocation import ModelAdapterResponse
    from core.runtime.memory_scope import MemoryAccessContext
    from core.auth import AuthorizationService,AuthorizationAction
    from tests.test_agent_loop_runtime import assembly,native
    from tests.test_agent_batch_handoff import Provider
    actor,definition,request,asset=await pair_inputs(clean_database)
    if exchange is not None:
        from core.agent_platform.ci_prompt_candidate import select as choose
        definition=choose(asset,approved_manifest_digest=asset['manifest_digest'],baseline=exchange['mode']=='BASELINE')
        definition=replace(definition,business_options={**definition.business_options,'ci_prompt_selection':dict(schema_version='ci-prompt-selection.v1',candidate_id=asset['manifest']['candidate_id'],manifest_digest=asset['manifest_digest'],mode=exchange['mode'],prompt_digest=catalog_digest(definition.instructions))})
        if exchange['phase']=='execute':
            request=CIRequestV2.model_validate(exchange['visible_request'])
            assert request.mode==exchange['mode'] and request.quality.run_id==request.quality.attempt_id
    owner=IncidentService(clean_database);project=str(request.quality.evaluation_project_id)
    await MemoryLifecycleRepository(clean_database).configure_project(tenant_id=actor.tenant_id,project_id=project,agent_id=definition.agent_id,grants={str(actor.user_id):['READ']},ttl_seconds=3600)
    bridge=SyncPersistenceBridge(asyncio.get_running_loop())
    def call(name,cid,args=None):
        r=native(name,cid,args);return replace(r,native_tool_calls=(r.native_tool_call,),native_tool_call=None)
    def read(messages):
        value=json.loads(json.loads(next(m['content'] for m in messages if m['role']=='tool'))['content'])
        entry=next(e for e in value['evidence'] if e['kind']=='PARSER_LOG')
        return call(TOOL_NAMES[1],'public-read',{'evidence_id':entry['evidence_id']})
    answer=[]
    def report(messages):
        values=[json.loads(json.loads(m['content'])['content']) for m in messages if m['role']=='tool']
        context,evidence=values[:2]
        value=dict(schema_version='ci-diagnosis.v1',kind='REPORT',incident_id=context['incident_id'],diagnosis_status='INSUFFICIENT_EVIDENCE',failure_stage='UNKNOWN',root_cause_hypothesis='SYNTHETIC PG storage fixture',supporting_evidence=[dict(evidence_id=evidence['evidence_id'],digest=evidence['digest'])],uncertainties=['SYNTHETIC_PROVIDER_NOT_REMOTE_MODEL'],recommended_checks=['人工复核'],recommended_owner_role='UNKNOWN')
        answer.append(json.dumps(value,ensure_ascii=False));return ModelAdapterResponse(answer[0],finish_reason='stop',tool_call_count=0)
    if exchange is not None:
        def next_read(kind,cid):
            def respond(messages):
                context=json.loads(json.loads(next(m['content'] for m in messages if m['role']=='tool'))['content'])
                entry=next(e for e in context['evidence'] if e['kind']==kind)
                return call(TOOL_NAMES[1],cid,{'evidence_id':entry['evidence_id']})
            return respond
        def complete_report(messages):
            values=[json.loads(json.loads(m['content'])['content']) for m in messages if m['role']=='tool']
            value=dict(schema_version='ci-diagnosis.v1',kind='REPORT',incident_id=values[0]['incident_id'],diagnosis_status='SUPPORTED',failure_stage='PARSER',root_cause_hypothesis='SYNTHETIC parser revision policy',supporting_evidence=[dict(evidence_id=e['evidence_id'],digest=e['digest']) for e in values[1:]],uncertainties=[],recommended_checks=['核对工具版本'],recommended_owner_role='TOOL_MAINTAINER')
            answer.append(json.dumps(value,ensure_ascii=False));return ModelAdapterResponse(answer[0],finish_reason='stop',tool_call_count=0)
        def paired_read(messages):
            from core.runtime.model_invocation import NativeToolCall
            context=json.loads(json.loads(next(m['content'] for m in messages if m['role']=='tool'))['content'])
            calls=tuple(NativeToolCall(kind,TOOL_NAMES[1],json.dumps({'evidence_id':next(e['evidence_id'] for e in context['evidence'] if e['kind']==kind)})) for kind in ('PARSER_LOG','ENVIRONMENT_RECORD'))
            return ModelAdapterResponse('',native_tool_calls=calls,tool_call_count=2,finish_reason='tool_calls',assistant_message={'role':'assistant','content':None,'tool_calls':[{'id':c.provider_tool_call_id,'type':'function','function':{'name':c.tool_name,'arguments':c.arguments_json}} for c in calls]})
        responses=[call(TOOL_NAMES[0],'public-context'),paired_read,next_read('CASE_REQUIREMENTS','c'),complete_report]
    else:responses=[call(TOOL_NAMES[0],'public-context'),read,report]
    model=Provider(responses)
    bundle=AgentRegistrationBundle(registrations=(AgentRegistration(definition),),model_profile_ids=frozenset({'default'}),tool_grants={n:frozenset({definition.agent_id}) for n in TOOL_NAMES})
    factory,repository,_,_=await assembly(clean_database,tmp_path,[],context_policy={'version':2},context_model=model,tools=[CIDiagnosisTool(n,owner,bridge) for n in TOOL_NAMES],business_bundle=bundle,memory_manager=PostgresMemoryManagerBridge(PostgresMemoryManager(clean_database),bridge))
    router=factory._router;snapshot=router.tool_discovery.create_tool_snapshot(str(uuid4()),router.tool_discovery.discover_tools('ci',definition.agent_id),selection_query='ci')
    pin=pinned_loop_config(router,definition.agent_id,NS(tool_resolution_snapshot=snapshot,execution_policy='durable-agent-loop.v2',memory_effect_policy='SERVICE_PROJECT_READ_ONLY.v1'))
    manifest=dict(schema_version='agent-quality-execution-manifest.v1',runtime_build_digest=runtime_build_digest(),public_fixture_version='SYNTHETIC_PG_STORAGE_ONLY',memory_effect_policy='SERVICE_PROJECT_READ_ONLY.v1',agents={definition.agent_id:dict(agent_version=definition.agent_version,execution_policy='durable-agent-loop.v2',definition_digest=digest(_pin_value(definition)),runtime_config_digest=digest(pin))})
    profile=dict(id='ci-public',version=1,policy='agent-quality-capture.v1',tool_aliases={n:n for n in TOOL_NAMES},parameter_paths={},fact_keys=[])
    if exchange is not None:
        pins=dict(manifest=manifest,profile=profile,runtime_json=json.dumps(pin,ensure_ascii=False,sort_keys=True,separators=(',',':')),definition_json=json.dumps(_pin_value(definition),ensure_ascii=False,sort_keys=True,separators=(',',':')),candidate_id=asset['manifest']['candidate_id'],candidate_manifest_digest=asset['manifest_digest'],prompt_digest=catalog_digest(definition.instructions))
        if exchange['phase']=='pins':return pins
        expected=exchange['pins']
        assert pins==expected,'FROZEN_SYNTHETIC_PINS_CHANGED'
        assert request.quality.execution_manifest_digest==catalog_digest(manifest) and request.quality.capture_profile_digest==catalog_digest(profile)
    else:
        request=request.model_copy(update={'quality':request.quality.model_copy(update={'execution_manifest_digest':catalog_digest(manifest),'capture_profile_digest':catalog_digest(profile)})})
    _,binding=await prepare(owner,actor,request,definition,manifest,profile)
    run=binding['run_id'];auth=AuthorizationService(clean_database);await auth.bind_new(actor,'RUN',run)
    scope=await factory.create_run_scope(definition.agent_id,QUERY_V2,run_id=run,session_id=binding['session_id'],persist=True,execution_policy='durable-agent-loop.v2',timeout_seconds=30,loop_correlation={k:str(getattr(request.quality,k)) for k in ('evaluation_run_id','attempt_id','request_id')},memory_access_context=MemoryAccessContext.from_principal(actor,definition.agent_id,binding['session_id']))
    try:result=await scope.execute();assert result.status.value=='SUCCEEDED',(result.error_code,result.safe_message)
    finally:await scope.close(abort=True)
    receipts=AgentQualityReceiptRepository(clean_database)
    await receipts.capture(repository,run,final_answer=FinalAnswerEvidenceV1.from_delivered_output(run_id=run,content=answer[0]).to_wire_dict())
    investigator=CIInvestigationService(owner,None,repository,None,auth,real_model=False)
    app=FastAPI();app.state.ci_investigation_service=investigator
    async def authorize(req,execution,receipt):await auth.authorize(actor,'RUN',run,AuthorizationAction.READ,required_scope='localagent:evaluation:execute')
    current=[actor]
    register(app,settings=NS(quality_evaluation_project_id=project),trusted=lambda *a:current[0],repository=lambda req:(receipts,repository),wire=lambda value,status=200:Response(value,status_code=status),require_service=lambda:None,bind_run=None,authorize_existing=authorize,final_answer_capture=None)
    endpoint=next(r.endpoint for r in app.routes if getattr(r,'path','')=='/api/runtime/runs/{run_id}/ci-synthetic-evidence/v2')
    req=Request({'type':'http','app':app,'headers':[]})
    current[0]=replace(actor,user_id=uuid4())
    with pytest.raises(HTTPException):await endpoint(run,req)
    current[0]=actor
    with pytest.raises(HTTPException):await endpoint(str(uuid4()),req)
    async with clean_database.session() as s:assert (await s.get(Binding,run)).sidecar is None
    responses=await asyncio.gather(*(endpoint(run,req) for _ in range(3)),return_exceptions=True)
    assert all(not isinstance(r,Exception) for r in responses),responses
    assert len({r.body for r in responses})==1
    body=json.loads(responses[0].body);source=body['sidecar']['source_binding']
    assert source['input_digest']==request.frozen_ci.input_digest
    assert len(source['tools'])==(4 if exchange else 2) and len(model.requests)==(4 if exchange else 3)
    import hashlib
    for tool in source['tools']:assert hashlib.sha256(tool['result_canonical_bytes'].encode()).hexdigest()==tool['result_digest']
    async with clean_database.session() as s:assert (await s.get(Binding,run)).sidecar.encode()==responses[0].body
    assert (await endpoint(run,req)).body==responses[0].body
    if exchange is not None:return json.loads(responses[0].body)
    print('PG_FIRST_SEAL: SYNTHETIC_PROVIDER=YES durable_tools=2 concurrent_reads=3 identical_bytes=PASS real_model_calls=0')
