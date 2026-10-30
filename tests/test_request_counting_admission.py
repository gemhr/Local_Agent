"""受控 E2E 豁免的五个定向合同；Provider fixture 不代表真实模型。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from sqlalchemy import select

from core.runtime.request_counting_admission import (
    AGENT, EXECUTION, LIMITS, STRATEGY, compile_exception, match_request,
    bind_context, eligible, validate_record, reference, persisted_eligible,
)
from core.runtime.agent_loop.quality_contract import catalog_digest, quality_runtime_config_digest
from core.runtime.execution_aggregate import canonical_payload_digest as digest
from core.incidents.evaluation import prepare
from core.incidents.service import IncidentService
from core.persistence.repositories.agent_quality import AgentQualityReceiptRepository
from tests.test_ci_synthetic_evaluation import inputs


def configuration(actor, definition, request):
    selection = definition.business_options['ci_prompt_selection']
    return dict(policy=dict(schema_version='e2e-request-counting-policy.v1',strategy=STRATEGY,
        execution_env='E2E_TEST',tenant_id=actor.tenant_id,project_id=str(request.quality.evaluation_project_id),
        principal_id=str(actor.user_id),agent_id=AGENT,execution_policy=EXECUTION,
        frozen_ci=request.frozen_ci.model_dump(),mode='CANDIDATE',candidate_id=selection['candidate_id'],
        candidate_manifest_digest=request.candidate_manifest_digest,prompt_digest=selection['prompt_digest'],
        profile_id='remote_advanced',provider_kind='deepseek',model_identity='deepseek-flash',loop_limits=LIMITS,
        max_tokens=4096,provider_timeout_seconds=120),
        authorization={k:str(getattr(request.quality,k)) for k in
            ('evaluation_run_id','attempt_id','request_id','run_id','execution_manifest_digest')})


def remote_profile():
    from core.runtime.model_selection import ModelProfile, ModelProfileId, ModelCostProfile
    return ModelProfile(ModelProfileId.REMOTE_ADVANCED,1000000,4096,True,True,True,True,2,2,
        ModelCostProfile(ModelProfileId.REMOTE_ADVANCED,True),True,'remote_advanced',
        supports_native_tool_calling=True,provider_kind='deepseek',model_identity='deepseek-flash')


async def ci_runtime(database,tmp_path,*,tamper=False):
    from core.agent_platform.registry import AgentRegistrationBundle
    from core.agent_platform.contracts import AgentRegistration
    from core.agent_platform.ci_diagnosis_tools import CIDiagnosisTool, TOOL_NAMES
    from core.persistence.sync_bridge import SyncPersistenceBridge
    from core.persistence.memory import PostgresMemoryManager, PostgresMemoryManagerBridge
    from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
    from core.runtime.agent_loop.context import pinned_loop_config, _pin_value
    from core.agent_platform.quality_configuration import runtime_build_digest
    from core.runtime.runtime_factory import CoordinatedRuntimeFactory
    from core.runtime.model_invocation import ModelAdapterResponse
    from tests.test_agent_loop_runtime import assembly, native
    from tests.test_agent_batch_handoff import Provider
    actor,definition,request,_=await inputs(database)
    service=IncidentService(database)
    bridge=SyncPersistenceBridge(asyncio.get_running_loop())
    tools=[CIDiagnosisTool(n,service,bridge) for n in TOOL_NAMES]
    await MemoryLifecycleRepository(database).configure_project(tenant_id=actor.tenant_id,
        project_id=str(request.quality.evaluation_project_id),agent_id=AGENT,
        grants={str(actor.user_id):['READ']},ttl_seconds=3600)
    first=native(TOOL_NAMES[0],'context')
    first=replace(first,native_tool_calls=(first.native_tool_call,),native_tool_call=None)
    model=Provider([first,ModelAdapterResponse('synthetic final',finish_reason='stop',tool_call_count=0)])
    bundle=AgentRegistrationBundle(registrations=(AgentRegistration(definition),),model_profile_ids=frozenset({'default'}),
        tool_grants={n:frozenset({AGENT}) for n in TOOL_NAMES})
    initial,repo,_,_=await assembly(database,tmp_path,[],tools=tools,context_policy={'version':2},
        context_model=model,business_bundle=bundle,model_profile=remote_profile(),max_tokens=4096,
        memory_manager=PostgresMemoryManagerBridge(PostgresMemoryManager(database),bridge))
    config=configuration(actor,definition,request)
    snapshot=initial._router.tool_discovery.create_tool_snapshot(str(uuid4()),
        initial._router.tool_discovery.discover_tools('ci',AGENT),selection_query='ci')
    pin=pinned_loop_config(initial._router,AGENT,NS(tool_resolution_snapshot=snapshot,
        execution_policy=EXECUTION,memory_effect_policy='SERVICE_PROJECT_READ_ONLY.v1'),request_counting_policy=config['policy'])
    manifest=dict(schema_version='agent-quality-execution-manifest.v1',runtime_build_digest=runtime_build_digest(),
        public_fixture_version='ci-public.v1',memory_effect_policy='SERVICE_PROJECT_READ_ONLY.v1',agents={AGENT:dict(
            agent_version=definition.agent_version,execution_policy=EXECUTION,
            definition_digest=digest(_pin_value(definition)),runtime_config_digest=quality_runtime_config_digest(pin))})
    profile=dict(id='ci-public',version=1,policy='agent-quality-capture.v1',tool_aliases={n:n for n in TOOL_NAMES},parameter_paths={},fact_keys=[])
    request=request.model_copy(update={'quality':request.quality.model_copy(update={
        'execution_manifest_digest':catalog_digest(manifest),'capture_profile_digest':catalog_digest(profile)})})
    config['authorization']['execution_manifest_digest']=request.quality.execution_manifest_digest
    trusted=compile_exception(config)
    factory=CoordinatedRuntimeFactory(initial._router,initial._services,agent_registry=initial._agent_registry,
        event_channel_capacity=256,execution_repository=repo,request_counting_exception=trusted)
    if tamper:
        original=initial._router.model_invocation_router.invoke
        def changed(**kw):
            kw['messages']=[*kw['messages'],{'role':'user','content':'changed after assessment'}]
            return original(**kw)
        initial._router.model_invocation_router.invoke=changed
    await prepare(service,actor,request,definition,manifest,profile,request_counting_exception=trusted)
    from core.auth import AuthorizationService
    await AuthorizationService(database).bind_new(actor,'RUN',str(request.quality.run_id))
    return factory,repo,model,actor,definition,request,trusted


async def execute_ci(factory,actor,request):
    from core.runtime.memory_scope import MemoryAccessContext
    from uuid import NAMESPACE_URL,uuid5
    run=str(request.quality.run_id)
    session=str(uuid5(NAMESPACE_URL,'agent-quality-session:'+run))
    scope=await factory.create_run_scope(AGENT,request.quality.query,run_id=run,session_id=session,persist=True,
        execution_policy=EXECUTION,timeout_seconds=30,
        loop_correlation={k:str(getattr(request.quality,k)) for k in ('evaluation_run_id','attempt_id','request_id')},
        memory_access_context=MemoryAccessContext.from_principal(actor,AGENT,session))
    try:
        return await scope.execute()
    finally:
        await scope.close(abort=True)


@pytest.mark.asyncio
async def test_default_runtime_heuristic_guard_is_closed(clean_database,tmp_path):
    from tests.test_agent_loop_runtime import assembly
    from tests.test_agent_batch_handoff import Provider
    from core.runtime.model_invocation import ModelAdapterResponse
    model=Provider([ModelAdapterResponse('must not execute',finish_reason='stop')])
    factory,repo,_,_=await assembly(clean_database,tmp_path,[],context_model=model,context_policy={'version':2},
        model_profile=remote_profile(),max_tokens=4096)
    scope=await factory.create_run_scope('core_router','public input',execution_policy=EXECUTION,persist=False)
    try:
        result=await scope.execute()
        image=await repo.load(scope.run_id)
        assert result.status.value=='FAILED'
        assert image.loop_frames[0].stop_code=='CONTEXT_BUDGET_UNCERTAIN'
        assert not model.requests and not image.models and scope.run_context.request_counting_admission is None
    finally:
        await scope.close(abort=True)


@pytest.mark.asyncio
async def test_exact_prepare_authorization_and_rehydration_fail_closed(clean_database):
    from core.agent_platform.ci_synthetic_evaluation import CIRequest
    from core.runtime.memory_scope import MemoryAccessContext
    actor,definition,request,_=await inputs(clean_database)
    trusted=compile_exception(configuration(actor,definition,request))
    assert match_request(trusted,request,actor,definition)==trusted
    for key in ('attempt_id','run_id','evaluation_run_id','request_id','case_id'):
        bad=request.model_copy(update={'quality':request.quality.model_copy(update={key:uuid4()})})
        with pytest.raises(ValueError):match_request(trusted,bad,actor,definition)
    from core.auth import AuthError
    for bad in (replace(actor,tenant_id='wrong'),replace(actor,principal_kind='HUMAN')):
        with pytest.raises((ValueError,AuthError)):match_request(trusted,request,bad,definition)
    with pytest.raises(ValueError):match_request(trusted,request.model_copy(update={'mode':'BASELINE'}),actor,definition)
    with pytest.raises(ValueError):CIRequest.model_validate({**request.model_dump(mode='json'),'request_counting_exception':trusted.policy})
    results=await asyncio.gather(*(prepare(IncidentService(clean_database),actor,request,definition,{}, {},
        request_counting_exception=trusted) for _ in range(2)))
    assert sum(r[0] for r in results)==1
    receipt=await AgentQualityReceiptRepository(clean_database).read(str(request.quality.run_id))
    record=receipt.evaluation_capture['e2e_test_admission']
    assert validate_record(record,receipt=receipt)
    binding=results[0][1]
    context=NS(run_id=binding['run_id'],session_id=binding['session_id'],execution_policy=EXECUTION,
        loop_correlation={k:str(getattr(request.quality,k)) for k in ('evaluation_run_id','attempt_id','request_id')})
    access=MemoryAccessContext.from_principal(actor,AGENT,binding['session_id'])
    assert bind_context(trusted,receipt,context,access).record==record
    for config in (None,compile_exception({**configuration(actor,definition,request),
            'authorization':{**trusted.authorization,'request_id':str(uuid4())}})):
        with pytest.raises(ValueError):bind_context(config,receipt,context,access)
    for access_bad in (replace(access,principal_kind='HUMAN'),replace(access,tenant_id='wrong')):
        with pytest.raises(ValueError):bind_context(trusted,receipt,context,access_bad)
    assert bind_context(None,NS(evaluation_capture={}),context,access) is None  # 普通 Quality 无授权
    assert not (await prepare(IncidentService(clean_database),actor,request,definition,{}, {},request_counting_exception=trusted))[0]
    assert (await AgentQualityReceiptRepository(clean_database).read(binding['run_id'])).evaluation_capture==receipt.evaluation_capture


@pytest.mark.asyncio
async def test_real_runtime_snapshot_second_admission_and_durable_raw_assessment(clean_database,tmp_path):
    factory,repo,model,actor,definition,request,trusted=await ci_runtime(clean_database,tmp_path)
    result=await execute_ci(factory,actor,request)
    image=await repo.load(str(request.quality.run_id))
    assert result.status.value=='SUCCEEDED',(result.error_code,result.safe_message)
    assert len(model.requests)==len(image.models)==2 and len(image.tool_invocations)==1
    receipt=await AgentQualityReceiptRepository(clean_database).read(image.root.run_id)
    for row in image.models:
        raw=row.request_assessment
        assert raw['mode']=='HEURISTIC' and raw['reliable'] is False and raw['admissible'] is False
        assert raw['request_digest']==row.request_digest
        assert persisted_eligible(raw,image.root,image.loop_frames[0],receipt)
    assert all(s.validation['budget'] is False for s in image.context_snapshots)
    assert all(s.validation['e2e_test_admission']['RELIABLE_TOKEN_ADMISSION']=='NOT_VERIFIED' for s in image.context_snapshots)
    from core.persistence.models import RuntimeLoopActionRow,RuntimeMemoryBindingRow
    from core.runtime.agent_loop.execution_evidence import build_execution_evidence
    async with clean_database.session() as s:
        actions=tuple((await s.scalars(select(RuntimeLoopActionRow).where(RuntimeLoopActionRow.run_id==image.root.run_id))).all())
        binding=await s.get(RuntimeMemoryBindingRow,image.root.run_id)
    assert build_execution_evidence(image,actions,(),binding)['coverage']=='COMPLETE'
    broken=replace(image,models=())
    assert 'MODEL_BINDING_MISSING' in build_execution_evidence(broken,actions,(),binding)['gaps']
    changed_root=NS(**{**image.root.__dict__,'resume_input':{**image.root.resume_input,'agent_loop':{**image.root.resume_input['agent_loop'],'e2e_test_admission':None}}})
    gaps=build_execution_evidence(replace(image,root=changed_root),actions,(),binding)['gaps']
    assert 'MODEL_BINDING_MISSING' in gaps and 'SNAPSHOT_VALIDATION_MISSING' in gaps


@pytest.mark.asyncio
async def test_final_request_digest_still_rejects_before_provider(clean_database,tmp_path):
    factory,repo,model,actor,definition,request,trusted=await ci_runtime(clean_database,tmp_path,tamper=True)
    result=await execute_ci(factory,actor,request)
    image=await repo.load(str(request.quality.run_id))
    assert result.status.value=='FAILED'
    assert image.loop_frames[0].stop_code=='CONTEXT_BUDGET_UNCERTAIN'
    assert not model.requests and not image.models


@pytest.mark.asyncio
async def test_forged_persistent_annotation_cannot_initialize_root(clean_database,tmp_path):
    from core.persistence.models import RuntimeAgentEvaluationReceiptRow
    factory,repo,model,actor,definition,request,trusted=await ci_runtime(clean_database,tmp_path)
    # Factory 持有原授权后，Initialize 重新从事务读取 Receipt；不能只相信内存标记。
    original=repo.initialize
    async def removed(root,**kw):
        async with clean_database.transaction() as s:
            receipt=await s.get(RuntimeAgentEvaluationReceiptRow,root.run_id)
            receipt.evaluation_capture={}
        return await original(root,**kw)
    repo.initialize=removed
    result=await execute_ci(factory,actor,request)
    assert result.status.value=='FAILED'
    assert await repo.load(str(request.quality.run_id)) is None
    assert not model.requests
