"""一条受控 Python Runtime/真实 CI Tool/PG → Go Target HTTP 链路。"""
import asyncio,json,os,socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import uuid4
import pytest
from tests.test_ci_synthetic_evaluation import inputs


@pytest.mark.asyncio
async def test_ci_runtime_python_go_http(clean_database,tmp_path):
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from core.agent_platform.registry import AgentRegistrationBundle
    from core.agent_platform.contracts import AgentRegistration
    from core.agent_platform.application import AgentApplicationService
    from core.agent_platform.agent_quality_evaluation import register_agent_quality_evaluation
    from core.agent_platform.quality_configuration import runtime_build_digest
    from core.agent_platform.ci_diagnosis import CIInvestigationService
    from core.agent_platform.ci_diagnosis_tools import CIDiagnosisTool,TOOL_NAMES
    from core.incidents.service import IncidentService
    from core.auth import AuthorizationService,AuthError
    from core.persistence.sync_bridge import SyncPersistenceBridge
    from core.persistence.memory import PostgresMemoryManager,PostgresMemoryManagerBridge
    from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
    from core.chat_service import ChatService
    from core.runtime.agent_loop.context import pinned_loop_config,_pin_value
    from core.runtime.execution_aggregate import canonical_payload_digest
    from core.runtime.agent_loop.quality_contract import catalog_digest
    from core.runtime.generation_evidence import FinalAnswerEvidenceV1
    from core.runtime.model_invocation import ModelAdapterResponse
    from tests.test_agent_loop_runtime import assembly,native
    from tests.test_agent_batch_handoff import Provider
    actor,definition,request,asset=await inputs(clean_database)
    incident_service=IncidentService(clean_database)
    project=str(request.quality.evaluation_project_id)
    lifecycle=MemoryLifecycleRepository(clean_database)
    await lifecycle.configure_project(tenant_id=actor.tenant_id,project_id=project,agent_id=definition.agent_id,
        grants={str(actor.user_id):['READ']},ttl_seconds=3600)
    bridge=SyncPersistenceBridge(asyncio.get_running_loop())
    tools=[CIDiagnosisTool(name,incident_service,bridge) for name in TOOL_NAMES]
    def ci_call(name,call_id,arguments=None):
        response=native(name,call_id,arguments)
        return replace(response,native_tool_calls=(response.native_tool_call,),native_tool_call=None)
    def read_first(messages):
        context=json.loads(json.loads(next(m['content'] for m in messages if m.get('role')=='tool'))['content'])
        return ci_call(TOOL_NAMES[1],'evidence',{'evidence_id':context['evidence'][0]['evidence_id']})
    def report(messages):
        entries=[json.loads(json.loads(m['content'])['content']) for m in messages if m.get('role')=='tool']
        context,evidence=entries
        value=dict(schema_version='ci-diagnosis.v1',kind='REPORT',incident_id=context['incident_id'],
            diagnosis_status='INSUFFICIENT_EVIDENCE',failure_stage='UNKNOWN',root_cause_hypothesis='合成测试保留不确定性',
            supporting_evidence=[dict(evidence_id=evidence['evidence_id'],digest=evidence['digest'])],
            uncertainties=['TEST_PROVIDER_NOT_REAL_MODEL'],recommended_checks=['人工核对'],recommended_owner_role='UNKNOWN')
        return ModelAdapterResponse(json.dumps(value,ensure_ascii=False),finish_reason='stop',tool_call_count=0)
    bundle=AgentRegistrationBundle(registrations=(AgentRegistration(definition),),model_profile_ids=frozenset({'default'}),
        tool_grants={name:frozenset({definition.agent_id}) for name in TOOL_NAMES})
    factory,repository,model,_=await assembly(clean_database,tmp_path,[],context_policy={'version':2},
        context_model=Provider([ci_call(TOOL_NAMES[0],'context'),read_first,report]),
        tools=tools,business_bundle=bundle,memory_manager=PostgresMemoryManagerBridge(PostgresMemoryManager(clean_database),bridge))
    router=factory._router
    snapshot=router.tool_discovery.create_tool_snapshot(str(uuid4()),router.tool_discovery.discover_tools('ci',definition.agent_id),selection_query='ci')
    pin=pinned_loop_config(router,definition.agent_id,NS(tool_resolution_snapshot=snapshot,
        execution_policy='durable-agent-loop.v2',memory_effect_policy='SERVICE_PROJECT_READ_ONLY.v1'))
    manifest=dict(schema_version='agent-quality-execution-manifest.v1',runtime_build_digest=runtime_build_digest(),
        public_fixture_version='ci-public.v1',memory_effect_policy='SERVICE_PROJECT_READ_ONLY.v1',agents={definition.agent_id:{
            'agent_version':definition.agent_version,'execution_policy':'durable-agent-loop.v2',
            'definition_digest':canonical_payload_digest(_pin_value(definition)),'runtime_config_digest':canonical_payload_digest(pin)}})
    profile=dict(id='ci-public',version=1,policy='agent-quality-capture.v1',tool_aliases={n:n for n in TOOL_NAMES},parameter_paths={},fact_keys=[])
    deployment=tmp_path/'managed.json'
    deployment.write_text(json.dumps(dict(manifest=manifest,capture_profile=profile,ci_synthetic={
        'tenant_id':actor.tenant_id,'frozen_ci':request.frozen_ci.model_dump(),'attempts':{'CANDIDATE':str(request.quality.attempt_id)}})),encoding='utf-8')
    service=AgentApplicationService(ChatService(router,coordinated_runtime_factory=factory),
        NS(require_entry=lambda name:NS(definition=definition,workflow=None)))
    app=FastAPI();app.state.loop_execution_repository=repository;app.state.incident_service=incident_service
    # 容量 seam 仅属测试 app；正式应用使用原 RunExecutionSupervisor。
    async def capacity():return True
    app.state.run_execution_supervisor=NS(acquire_slot=capacity,release_slot=lambda reserved:None)
    app.state.ci_investigation_service=CIInvestigationService(incident_service,service,repository,None,AuthorizationService(clean_database),real_model=False)
    observed=[]
    @app.middleware('http')
    async def identity(req,call_next):
        if req.headers.get('Authorization')!='Bearer synthetic-ci-token':return JSONResponse({'error':'UNAUTHORIZED'},401)
        req.state.principal=actor
        response=await call_next(req)
        observed.append(response.status_code)
        return response
    @app.exception_handler(AuthError)
    async def forbidden(req,exc):return JSONResponse({'error':exc.code},exc.status_code)
    async def bind(req,**kw):await AuthorizationService(clean_database).bind_new(actor,'RUN',kw['run_id'])
    async def authorize(req,run,action,**kw):await AuthorizationService(clean_database).authorize(actor,'RUN',run,action,**kw)
    def final_capture(*,output,result):
        observed.append(result.error_code)
        if result.status.value!='SUCCEEDED':return 'FAILED',None,None
        return 'COMPLETE',None,FinalAnswerEvidenceV1.from_delivered_output(run_id=result.run_id,content=output).to_wire_dict()
    register_agent_quality_evaluation(app,settings=NS(quality_evaluation_project_id=project,quality_evaluation_service_principal_id=str(actor.user_id),quality_evaluation_configuration_path=str(deployment)),
        require_service=lambda:service,bind_run=bind,authorize_run=authorize,final_answer_capture=final_capture)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False,lifespan='off'))
    serving=asyncio.create_task(server.serve(sockets=[sock]))
    config=dict(quality=dict(base_url=f'http://127.0.0.1:{port}',token_env='CI_TEST_TOKEN',trusted_project_id=project,
        max_response_bytes=2*1024*1024,call_milliseconds=45000,http={},execution_manifest=manifest,capture_profile=profile),
        frozen_ci=request.frozen_ci.model_dump(),candidate_manifest_digest=asset['manifest_digest'],mode='CANDIDATE',
        prompt_digest=catalog_digest(definition.instructions),candidate_id=asset['manifest']['candidate_id'])
    path=tmp_path/'consumer.json';path.write_text(json.dumps(config),encoding='utf-8')
    try:
        for _ in range(100):
            if server.started:break
            await asyncio.sleep(.01)
        proc=await asyncio.create_subprocess_exec('go','test','./internal/provider','-run','^TestCISyntheticPythonHTTP$','-count=1','-v',
            cwd=str(Path(__file__).resolve().parents[2]/'AgentEvalOps/go-backend'),
            env={**os.environ,'CI_SYNTHETIC_LIVE_CONFIG':str(path),'CI_SYNTHETIC_LIVE_ATTEMPT':str(request.quality.attempt_id),'CI_TEST_TOKEN':'synthetic-ci-token'},
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
        output,_=await asyncio.wait_for(proc.communicate(),90)
        assert proc.returncode==0,output.decode('utf-8',errors='replace')+repr(observed)
        assert len(model.requests)==3
        image=await repository.load(str(request.quality.attempt_id))
        assert image.root.status=='SUCCEEDED'
    finally:
        server.should_exit=True
        await serving
