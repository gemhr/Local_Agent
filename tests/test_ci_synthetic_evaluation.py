"""获准架构的窄 PG/HTTP 验证；测试身份/Provider 不作正式模型证明。"""
import asyncio
import json
from types import SimpleNamespace as NS
from dataclasses import replace
from uuid import uuid4
from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import select, func
from core.auth import Principal, EVALUATION_EXECUTE_SCOPE, CI_SYNTHETIC_EVALUATE_SCOPE
from core.incidents.evaluation import prepare, frozen_input
from core.incidents.service import IncidentService
from core.incidents.models import SyntheticEvaluationBindingRow, IncidentRow
from core.persistence.models import UserRow, RuntimeAgentEvaluationReceiptRow
from core.agent_platform.ci_synthetic_evaluation import CIRequest, PROTOCOL, QUERY
from core.runtime.agent_loop.quality_contract import catalog_digest
from tests.test_ci_prompt_candidate import candidate
from core.agent_platform.ci_prompt_candidate import select as choose


async def inputs(database):
    uid=uuid4();tenant='00000000-0000-0000-0000-000000000001'
    async with database.transaction() as s:
        s.add(UserRow(id=uid,subject=str(uid),tenant_id=tenant,principal_kind='SERVICE',
            service_scopes=[EVALUATION_EXECUTE_SCOPE,CI_SYNTHETIC_EVALUATE_SCOPE]))
    actor=Principal(uid,str(uid),frozenset({'SERVICE'}),'synthetic',datetime.now(timezone.utc),
        datetime.now(timezone.utc)+timedelta(minutes=10),'SERVICE',frozenset({EVALUATION_EXECUTE_SCOPE,CI_SYNTHETIC_EVALUATE_SCOPE}),tenant)
    asset=candidate();definition=choose(asset,approved_manifest_digest=asset['manifest_digest'])
    definition=replace(definition,business_options={**definition.business_options,'ci_prompt_selection':{
        'schema_version':'ci-prompt-selection.v1','candidate_id':asset['manifest']['candidate_id'],
        'manifest_digest':asset['manifest_digest'],'mode':'CANDIDATE','prompt_digest':catalog_digest(definition.instructions)}})
    run=str(uuid4());case=str(uuid4());project=str(uuid4())
    frozen=dict(**frozen_input(),case_id=case,case_version='1',case_digest='a'*64)
    request=CIRequest.model_validate(dict(contract_version=PROTOCOL,frozen_ci=frozen,
        candidate_manifest_digest=asset['manifest_digest'],mode='CANDIDATE',quality=dict(agent_id=definition.agent_id,query=QUERY,
        run_id=run,attempt_id=run,request_id=str(uuid4()),evaluation_run_id=str(uuid4()),evaluation_project_id=project,
        case_id=case,case_version='1',case_digest='a'*64,execution_manifest_digest='b'*64,
        capture_profile_id='ci-public',capture_profile_version=1,capture_profile_digest='c'*64,timeout_milliseconds=30000)))
    return actor,definition,request,asset


@pytest.mark.asyncio
async def test_atomic_prepare_idempotency_rollback_and_bound_tool(clean_database,monkeypatch):
    from core.ci_simulation import service as simulation
    from core.runtime.run_control import DurableRunControlService
    from core.persistence.repositories.agent_quality import AgentQualityReceiptRepository
    from core.persistence.models import ObjectOwnershipRow
    from core.runtime.memory_scope import MemoryAccessContext
    actor,definition,request,_=await inputs(clean_database)
    service=IncidentService(clean_database)
    original=simulation.execute_instance
    async def fail(*args,**kwargs):raise ValueError('prepare-test-failure')
    monkeypatch.setattr(simulation,'execute_instance',fail)
    with pytest.raises(ValueError):await prepare(service,actor,request,definition,{}, {})
    async with clean_database.session() as s:
        assert await s.scalar(select(func.count()).select_from(RuntimeAgentEvaluationReceiptRow))==0
        assert await s.scalar(select(func.count()).select_from(IncidentRow))==0
    monkeypatch.setattr(simulation,'execute_instance',original)
    results=await asyncio.gather(*(prepare(service,actor,request,definition,{}, {}) for _ in range(3)))
    assert sum(r[0] for r in results)==1
    binding=results[0][1]
    assert len({r[1]['investigation_id'] for r in results})==1 and binding['run_id']==str(request.quality.attempt_id)
    with pytest.raises(ValueError):
        await prepare(service,actor,request.model_copy(update={'mode':'BASELINE'}),definition,{}, {})
    async with clean_database.transaction() as s:
        s.add(ObjectOwnershipRow(object_type='RUN',object_id=binding['run_id'],owner_user_id=actor.user_id,tenant_id=actor.tenant_id))
    context=NS(run_id=binding['run_id'],session_id=binding['session_id'],data=NS(entry_agent_id=definition.agent_id,agent_version=definition.agent_version),
        memory_access_context=MemoryAccessContext.from_principal(actor,definition.agent_id,binding['session_id']))
    assert (await service.bound_execution(context))['execution_id']==binding['execution_id']
    from core.ci_simulation.service import CIError
    for changes in ({'session_id':'wrong'}, {'run_id':str(uuid4())},
            {'data':NS(entry_agent_id=definition.agent_id,agent_version='wrong')},
            {'memory_access_context':replace(context.memory_access_context,user_id=str(uuid4()))}):
        with pytest.raises(CIError):await service.bound_execution(NS(**{**vars(context),**changes}))
    from sqlalchemy import text
    from core.persistence.errors import PersistenceError
    for sql in ("UPDATE ci_synthetic_evaluation_bindings SET fingerprint='changed' WHERE run_id=:run",
            "DELETE FROM ci_synthetic_evaluation_bindings WHERE run_id=:run"):
        with pytest.raises(PersistenceError):
            async with clean_database.transaction() as s:
                await s.execute(text(sql),{'run':binding['run_id']})
    assert not (await prepare(service,actor,request,definition,{}, {}))[0]  # pre-Root 不授执行
    _,definition,other,_=await inputs(clean_database)
    control=DurableRunControlService(clean_database)
    await control.claim(str(other.quality.run_id),'existing-owner')
    with pytest.raises(ValueError):await prepare(service,actor,other,definition,{}, {})


@pytest.mark.asyncio
async def test_scope_and_frozen_request_validation(clean_database,tmp_path):
    from core.agent_platform.ci_synthetic_evaluation import validate_request
    from core.auth import AuthError
    actor,definition,request,_=await inputs(clean_database)
    path=tmp_path/'managed.json'
    config={'ci_synthetic':{'tenant_id':actor.tenant_id,'frozen_ci':request.frozen_ci.model_dump(),
        'attempts':{'CANDIDATE':str(request.quality.attempt_id)}}}
    path.write_text(json.dumps(config),encoding='utf-8')
    settings=NS(quality_evaluation_configuration_path=str(path),quality_evaluation_project_id=str(request.quality.evaluation_project_id),quality_evaluation_service_principal_id=str(actor.user_id))
    validate_request(request,settings,actor,definition,None)
    for scopes in ({EVALUATION_EXECUTE_SCOPE},{CI_SYNTHETIC_EVALUATE_SCOPE}):
        with pytest.raises(AuthError):validate_request(request,settings,replace(actor,scopes=frozenset(scopes)),definition,None)
    for bad in (replace(actor,principal_kind='HUMAN'),replace(actor,tenant_id='wrong'),replace(actor,user_id=uuid4())):
        with pytest.raises((AuthError,ValueError)):validate_request(request,settings,bad,definition,None)
    with pytest.raises(ValueError):validate_request(request.model_copy(update={'mode':'BASELINE'}),settings,actor,definition,None)
    for changes in ({'evaluation_project_id':uuid4()}, {'case_id':uuid4()}, {'case_digest':'d'*64}):
        bad=request.model_copy(update={'quality':request.quality.model_copy(update=changes)})
        with pytest.raises(ValueError):validate_request(bad,settings,actor,definition,None)
    with pytest.raises(ValueError):CIRequest.model_validate({**request.model_dump(mode='json'),'incident_id':'human-task'})
