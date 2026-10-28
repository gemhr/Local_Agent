"""E2 定向验证；构造 receipt 的用例全部为 TEST_FIXTURE，不是 Agent 执行。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import socket
import threading
from types import SimpleNamespace as NS
from uuid import uuid4

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, text

from core.auth import Principal, ObjectAuthorizationService
from core.feature_impact.agent import prepare, task_prompt
from core.feature_impact.agent_reports import validated_agent_report, evaluate_agent_report, require_binding_owner
from core.feature_impact.app import create_app
from core.feature_impact.knowledge import digest, read
from core.feature_impact.models import TaskRow, ReportRow, InvestigationRow
from core.feature_impact.public_tasks import load_public_task
from core.feature_impact.reports import rule_report
from core.feature_impact.service import FeatureImpactService, BusinessError
from core.feature_impact.tools import AGENT_ID, TOOL_NAMES, FeatureEvidenceTool
from core.persistence.database import Database, DatabaseConfig
from core.persistence.errors import PersistenceError
from core.persistence.sync_bridge import SyncPersistenceBridge
from core.persistence.models import UserRow, RoleRow, UserRoleRow, ObjectOwnershipRow
from core.runtime.memory_scope import MemoryAccessContext
from core.runtime.snapshot_serialization import sha256_digest, text_digest
from core.runtime.tool_contract import canonical_json_digest
from core.runtime.tool_adapters import ToolAdapterInvocationError
from core.settings import Settings
from tests._pg_fixtures import test_database_url as database_url


def agent_fixture():
    """TEST_FIXTURE：借用 E1 报告内容测试解析，不调用 Provider、不写 Runtime receipt。"""
    task = NS(task_id='test-fixture-task', owner_user_id=uuid4(), tenant_id='test-fixture-tenant', snapshot=load_public_task('T2'))
    report = deepcopy(rule_report(task.snapshot)[0])
    for finding in report['findings']:
        for ref in finding['supporting_evidence'] + finding['counter_evidence']:
            ref['basis'] = 'TASK_INPUT'
    raw = json.dumps(report, ensure_ascii=False)
    binding = NS(run_id='test-fixture-run', session_id='test-fixture-session', task_id=task.task_id)
    payload = dict(content=raw, producer_agent_id=AGENT_ID)
    seed = dict(messages=[dict(role='user', content=task_prompt(task.snapshot))])
    frame = NS(run_id=binding.run_id, frame_id='f', frame_role='ROOT', step_id='s', execution_agent_id=AGENT_ID,
               result_decision_id='final', context_seed=seed, seed_digest=sha256_digest(seed),
               final_delivery_receipt=dict(status='DELIVERED', decision_id='final', result_digest=text_digest(raw)))
    final = NS(run_id=binding.run_id, step_id='s', frame_id='f', decision_id='final', decision_kind='FINAL', iteration_no=2, state='CLOSED')
    image = NS(root=NS(run_id=binding.run_id, status='SUCCEEDED', execution_policy='durable-agent-loop.v2',
        resume_input=dict(resolved_agent_id=AGENT_ID, agent_version='1.1.0', session_id=binding.session_id, user_query=task_prompt(task.snapshot))),
        loop_frames=[frame], steps=[NS(run_id=binding.run_id, status='SUCCEEDED', step_id='s', typed_result_payload=payload, result_digest=sha256_digest(payload))],
        loop_iterations=[final], tool_invocations=[])
    return task, binding, image


@pytest.mark.asyncio
async def test_tool_rechecks_current_run_and_conversation_before_reading(monkeypatch):
    """TEST_FIXTURE：持久状态可变，已准备 Context 不变；权限拒绝必须先于资料读取。"""
    import core.feature_impact.service as service_module
    from sqlalchemy.exc import SQLAlchemyError
    owner_id=uuid4()
    task=NS(task_id='ownership-fixture-task',owner_user_id=owner_id,tenant_id='ownership-fixture-tenant',
            snapshot=load_public_task('T2'),exposed_at=None)
    binding=NS(run_id='ownership-fixture-run',session_id='ownership-fixture-session',task_id=task.task_id)
    access=MemoryAccessContext(task.tenant_id,str(owner_id),AGENT_ID,binding.session_id)
    context=NS(run_id=binding.run_id,session_id=binding.session_id,memory_access_context=access,
               data=NS(entry_agent_id=AGENT_ID,agent_version='1.1.0'))
    run_key=('RUN',binding.run_id)
    conversation_key=('CONVERSATION',access.conversation_id)
    correct=lambda:NS(owner_user_id=owner_id,tenant_id=task.tenant_id)
    owners={run_key:correct(),conversation_key:correct()}
    queries=[]
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get(self,kind,key):
            if kind is InvestigationRow: return binding if key==binding.run_id else None
            if kind is TaskRow: return task if key==task.task_id else None
            if kind is ObjectOwnershipRow:
                queries.append(key)
                value=owners.get(key)
                if isinstance(value,Exception): raise value
                return value
    service=FeatureImpactService(NS(session=Session))
    accessed=[]
    original_read,original_search=service_module.read,service_module.search
    def tracked_read(*args): accessed.append('read');return original_read(*args)
    def tracked_search(*args): accessed.append('search');return original_search(*args)
    monkeypatch.setattr(service_module,'read',tracked_read)
    monkeypatch.setattr(service_module,'search',tracked_search)
    # 同一份已准备好的 context 先合法成功，再模拟归属撤销/变化。
    assert (await service.bound_evidence(context,{'source_ref':'T2:design:v1'}))['source_ref']=='T2:design:v1'
    assert (await service.bound_evidence(context,{'query':''}))['items']
    assert run_key in queries
    for key,replacement in (
        (conversation_key,NS(owner_user_id=uuid4(),tenant_id=task.tenant_id)),
        (conversation_key,None),
        (conversation_key,NS(owner_user_id=owner_id,tenant_id='other-tenant')),
        (run_key,NS(owner_user_id=uuid4(),tenant_id=task.tenant_id)),
        (run_key,None),
        (conversation_key,SQLAlchemyError('ownership authority unavailable')),
    ):
        owners[key]=replacement
        if not isinstance(replacement,Exception):
            with pytest.raises(ValueError,match='AGENT_RUN_OWNER_INVALID'):
                await require_binding_owner(Session(),binding,task)
        before=list(accessed)
        for arguments in ({'source_ref':'T2:design:v1'},{'query':''}):
            with pytest.raises(BusinessError,match='FEATURE_SCOPE_DENIED') as denied:
                await service.bound_evidence(context,arguments)
            assert denied.value.status==403 and accessed==before
        owners[key]=correct()
    # 还原后可读；现有跨任务来源和参数检查保留。
    assert (await service.bound_evidence(context,{'source_ref':'T2:design:v1'}))['source_ref']=='T2:design:v1'
    assert conversation_key in queries
    with pytest.raises(ValueError):
        await service.bound_evidence(context,{'source_ref':'T3:design:v1'})
    before=list(accessed)
    with pytest.raises(BusinessError,match='FEATURE_TOOL_INPUT_INVALID'):
        await service.bound_evidence(context,{'query':'','task_id':'another-task'})
    assert accessed==before


def test_agent_final_contract_fixture_rejects_identity_and_false_reads():
    task, binding, image = agent_fixture()
    raw, parsed, source = validated_agent_report(binding, image, [], task)
    assert raw == image.steps[0].typed_result_payload['content']
    assert not source['read_sources'] and len(source['task_input_sources']) == 7
    for mutator in (
        lambda x: setattr(x.root, 'run_id', 'other'),
        lambda x: x.root.resume_input.update(session_id='other'),
        lambda x: x.root.resume_input.update(resolved_agent_id='other'),
        lambda x: x.root.resume_input.update(user_query='other input'),
        lambda x: setattr(x.loop_frames[0], 'final_delivery_receipt', {}),
        lambda x: x.loop_frames[0].final_delivery_receipt.update(result_digest='forged'),
        lambda x: setattr(x.steps[0], 'result_digest', 'forged'),
    ):
        changed = deepcopy(image)
        mutator(changed)
        with pytest.raises(ValueError):
            validated_agent_report(binding, changed, [], task)
    # 将一条引用声明为工具原文，不能使用预装输入冒充已观察 Tool。
    parsed['findings'][0]['supporting_evidence'][0]['basis'] = 'EVIDENCE_READ'
    image.steps[0].typed_result_payload['content'] = json.dumps(parsed, ensure_ascii=False)
    image.steps[0].result_digest = sha256_digest(image.steps[0].typed_result_payload)
    image.loop_frames[0].final_delivery_receipt['result_digest'] = text_digest(image.steps[0].typed_result_payload['content'])
    with pytest.raises(ValueError):
        validated_agent_report(binding, image, [], task)
    # TEST_FIXTURE 的真实字段结构：COMMITTED + 同 Run/action + OBSERVED 正文匹配。
    doc = read(task.snapshot, 'T2:design:v1')
    committed = dict(status='SUCCEEDED', output=dict(content=json.dumps(doc, ensure_ascii=False)))
    projection = dict(outcome='SUCCEEDED', content=committed['output']['content'])
    action = NS(action_id='a', protocol_call_id='call', invocation_id='a', run_id=binding.run_id, step_id='s', frame_id='f', iteration_no=1,
                source_digest=canonical_json_digest(committed), result_projection=projection, result_digest=sha256_digest(projection))
    tool = NS(invocation_id='a', run_id=binding.run_id, step_id='s', state='COMMITTED', tool_name=TOOL_NAMES[1],
              committed_result=committed, committed_result_digest=canonical_json_digest(committed))
    observation = dict(results=[dict(action_id='a', call_id='call', result=projection)])
    iteration = NS(run_id=binding.run_id, step_id='s', frame_id='f', iteration_no=1, state='OBSERVED', observation=observation, observation_digest=sha256_digest(observation), decision_id='tools')
    image.tool_invocations = [tool]
    image.loop_iterations.insert(0, iteration)
    assert validated_agent_report(binding, image, [action], task)[2]['read_sources'] == ['T2:design:v1']
    action.run_id = 'another-run'
    with pytest.raises(ValueError):
        validated_agent_report(binding, image, [action], task)
    future=deepcopy(task.snapshot)
    future['documents'][0]['published_at']='2099-01-01T00:00:00Z'
    with pytest.raises(ValueError): read(future,'T2:design:v1')
    adapter=FeatureEvidenceTool(TOOL_NAMES[1],None,None)
    with pytest.raises(ToolAdapterInvocationError): adapter.build_invocation(json.dumps(dict(source_ref='T2:design:v1',task_id='other')))


@pytest.mark.asyncio
async def test_agent_replay_fixture_revalidates_authority_and_preserves_raw():
    """TEST_FIXTURE repository/session，仅验证转换合同；不写伪造 Runtime/PG 成功事实。"""
    task, binding, image = agent_fixture()
    raw, report, source = validated_agent_report(binding, image, [], task)
    row = NS(report_id='test-fixture-report', task_id=task.task_id, rejection=None, raw_output=raw, payload=report, source_record=source,
             envelope=dict(candidate_kind='AGENT', candidate_version='1.1.0', run_id=binding.run_id, task_id=task.task_id,
             task_version=1, input_digest=task.snapshot['input_digest'], corpus_digest=task.snapshot['corpus_digest'], report_digest=digest(report)))
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, kind, key):
            if kind is ReportRow: return row if key == row.report_id else None
            if kind is TaskRow: return task if key == task.task_id else None
            if kind is InvestigationRow: return binding if key == binding.run_id else None
            if kind is ObjectOwnershipRow: return NS(tenant_id=task.tenant_id, owner_user_id=task.owner_user_id)
        async def execute(self, query): return NS(scalars=lambda:NS(all=lambda:[]))
    async def load(run, **kwargs): return image if run == binding.run_id else None
    service = NS(database=NS(session=Session))
    repository = NS(load=load)
    principal = Principal(uuid4(),'fixture',frozenset({'SERVICE'}),'fixture',datetime.now(timezone.utc),datetime.now(timezone.utc)+timedelta(hours=1),
                          principal_kind='SERVICE',scopes=frozenset({'localagent:feature-impact:evaluate'}),tenant_id=task.tenant_id)
    value = dict(task_id=task.task_id, snapshot=task.snapshot, candidate_kind='AGENT',candidate_version='1.1.0',report=dict(report_id=row.report_id,run_id=binding.run_id))
    body = dict(input=value, project_id='fixture-project', run_id='fixture-eo-run', attempt_id='fixture-attempt',request_id='fixture-request',case_id='fixture-case',frozen_input_digest=digest(value))
    out = await evaluate_agent_report(service,repository,principal,body)
    assert out['report'] == report and out['source_record']['raw_output'] == raw
    assert out['source_record']['report_id'] == row.report_id
    # 合同 fixture 单独导出给 Go，不伪装 HTTP 或真实 AGENT 运行。
    if os.getenv('FEATURE_AGENT_CONTRACT_FIXTURE'):
        Path(os.environ['FEATURE_AGENT_CONTRACT_FIXTURE']).write_text(json.dumps(dict(evidence_kind='TEST_FIXTURE', input=value, output=out),ensure_ascii=False,indent=2),encoding='utf-8')
    for change in ('run','snapshot','raw','owner','final'):
        changed = deepcopy(body)
        if change == 'run': changed['input']['report']['run_id']='wrong'
        if change == 'snapshot': changed['input']['snapshot']['title']='wrong'
        if change == 'raw': row.raw_output='changed'
        if change == 'owner': principal=replace(principal,tenant_id='other-tenant')
        if change == 'final': image.loop_frames[0].final_delivery_receipt={}
        changed['frozen_input_digest']=digest(changed['input'])
        with pytest.raises((BusinessError,ValueError)):
            await evaluate_agent_report(service,repository,principal,changed)
        row.raw_output=raw
        principal=replace(principal,tenant_id=task.tenant_id)


@pytest.mark.asyncio
async def test_public_binding_real_http_pg_without_model_or_database_reset():
    database = Database(DatabaseConfig(url=database_url()))
    service = FeatureImpactService(database)
    issued = datetime.now(timezone.utc)
    async def identity(role, kind='HUMAN'):
        uid=uuid4()
        async with database.transaction() as session:
            session.add(UserRow(id=uid,subject=str(uid),principal_kind=kind,service_scopes=['localagent:feature-impact:evaluate'] if kind=='SERVICE' else []))
            await session.flush()
            assigned=(await session.execute(select(RoleRow).where(RoleRow.code==role))).scalar_one_or_none()
            if assigned is None:
                assigned=RoleRow(id=uuid4(),code=role);session.add(assigned);await session.flush()
            session.add(UserRoleRow(user_id=uid,role_id=assigned.id))
        return Principal(uid,str(uid),frozenset({role}),'test-fixture',issued,issued+timedelta(hours=1),principal_kind=kind,
                         scopes=frozenset({'localagent:feature-impact:evaluate'}) if kind=='SERVICE' else frozenset(),tenant_id='00000000-0000-0000-0000-000000000001')
    operator, ordinary, evaluator = await identity('OPERATOR'), await identity('USER'), await identity('SERVICE','SERVICE')
    key=Ed25519PrivateKey.generate()
    settings=replace(Settings.load(),database_url=database.config.url,jwt_public_key=key.public_key(),jwt_issuer='feature-binding',jwt_audience='feature-binding')
    app=create_app(settings)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    def token(p): return jwt.encode(dict(iss='feature-binding',aud='feature-binding',sub=p.subject,roles=list(p.roles),scopes=list(p.scopes),jti=uuid4().hex,iat=issued,nbf=issued,exp=p.expires_at),key,algorithm='EdDSA')
    evidence=dict(evidence_kind='REAL_HTTP_PG_NO_MODEL',tasks={},rejections=[],model_calls=0)
    try:
        for _ in range(100):
            if server.started: break
            await asyncio.sleep(.05)
        assert server.started
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',trust_env=False,headers={'Authorization':'Bearer '+token(operator)}) as client:
            for code in ('T2','T3','T5','T6'):
                body=dict(request_id=str(uuid4()),public_task=code)
                response=await client.post('/api/feature-impact/public-experiment-tasks',json=body)
                assert response.status_code==200,response.text
                task=response.json();tid=task['task_id']
                assert {k:task[k] for k in load_public_task(code)}==load_public_task(code)
                assert (await client.post('/api/feature-impact/public-experiment-tasks',json=body)).json()['task_id']==tid
                conflict={**body,'public_task':'T3' if code!='T3' else 'T2'}
                assert (await client.post('/api/feature-impact/public-experiment-tasks',json=conflict)).status_code==409
                request,access=await prepare(service,ObjectAuthorizationService(database),operator,tid,str(uuid4()))
                assert request.input==task_prompt(load_public_task(code)) and request.agent_id==AGENT_ID
                context=NS(run_id=request.run_id,session_id=request.session_id,memory_access_context=access,data=NS(entry_agent_id=AGENT_ID,agent_version='1.1.0'))
                ref=f'{code}:design:v1'
                assert (await service.bound_evidence(context,{'source_ref':ref}))['source_ref']==ref
                adapter=FeatureEvidenceTool(TOOL_NAMES[1],service,SyncPersistenceBridge(asyncio.get_running_loop()))
                invocation=adapter.build_invocation(json.dumps({'source_ref':ref}))
                result=await asyncio.to_thread(adapter.invoke_once,invocation,NS(run_context=context,remaining_seconds=lambda:10,raise_if_cancelled=lambda:None))
                assert json.loads(result.content)['source_ref']==ref
                with pytest.raises(ValueError): await service.bound_evidence(context,{'source_ref':'T3:design:v1' if code!='T3' else 'T2:design:v1'})
                with pytest.raises(BusinessError): await service.bound_evidence(context,{'source_ref':ref,'task_id':'another'})
                context.memory_access_context=MemoryAccessContext.from_principal(ordinary,AGENT_ID,request.session_id)
                with pytest.raises(BusinessError): await service.bound_evidence(context,{'query':''})
                async with database.session() as session:
                    saved=await session.get(TaskRow,tid)
                    assert saved.snapshot==load_public_task(code) and saved.owner_user_id==operator.user_id
                    assert await session.get(ObjectOwnershipRow,('RUN',request.run_id))
                    await require_binding_owner(session,await session.get(InvestigationRow,request.run_id),saved)
                # 真实 PG 没有任何 Runtime FINAL；不能将准备好的身份当成已执行。
                value=dict(task_id=tid,snapshot=load_public_task(code),candidate_kind='AGENT',candidate_version='1.1.0',report=dict(report_id=str(uuid4()),run_id=request.run_id))
                q=dict(protocol='feature-impact-request.v1',project_id=str(uuid4()),run_id=str(uuid4()),attempt_id=str(uuid4()),request_id=str(uuid4()),case_id='public-contract',input=value,frozen_input_digest=digest(value))
                rejected=await client.post('/api/feature-impact/quality/evaluate',json=q,headers={'Authorization':'Bearer '+token(evaluator)})
                assert rejected.status_code==422
                assert (await client.post(f'/api/feature-impact/tasks/{tid}/investigations',json={'request_id':str(uuid4())})).status_code==409
                evidence['tasks'][code]=dict(task_id=tid,run_id=request.run_id,session_id=request.session_id,input_digest=task['input_digest'],status='PREPARED_NOT_EXECUTED')
            denied=await client.post('/api/feature-impact/public-experiment-tasks',json=dict(request_id=str(uuid4()),public_task='T2'),headers={'Authorization':'Bearer '+token(ordinary)})
            assert denied.status_code==403
            for invalid in ('../T2','T1'):
                assert (await client.post('/api/feature-impact/public-experiment-tasks',json=dict(request_id=str(uuid4()),public_task=invalid))).status_code==422
            # 旧 HUMAN create 与 RULE/HUMAN 分析保持可用。
            default=(await client.post('/api/feature-impact/tasks',json=dict(request_id=str(uuid4())))).json()
            rule=(await client.post(f'/api/feature-impact/tasks/{default["task_id"]}/rule-reports',json=dict(request_id=str(uuid4())))).json()
            assert rule['candidate_kind']=='RULE' and rule['status']=='SEALED'
            raw=deepcopy(rule['report'])
            for finding in raw['findings']:
                for ref in finding['supporting_evidence']+finding['counter_evidence']: ref['basis']='TASK_INPUT'
            human=(await client.post(f'/api/feature-impact/tasks/{default["task_id"]}/reports',json=dict(request_id=str(uuid4()),raw_output=json.dumps(raw)))).json()
            assert human['candidate_kind']=='HUMAN' and human['status']=='SEALED'
            evidence['compatibility']=dict(rule=rule['report_id'],human=human['report_id'])
            # 测试侧尝试 UPDATE，既有不可变触发器必须拒绝；事务回滚，不改变资料。
            with pytest.raises(PersistenceError,match='DATABASE_OPERATION_FAILED') as blocked:
                async with database.transaction() as session:
                    await session.execute(text("UPDATE feature_impact_tasks SET snapshot='{}'::jsonb WHERE task_id=:id"),dict(id=tid))
            assert 'FEATURE_IMPACT_IMMUTABLE' in str(blocked.value.__context__)
            async with database.session() as session:
                assert (await session.get(TaskRow,tid)).snapshot==load_public_task('T6')
        if os.getenv('FEATURE_AGENT_BINDING_EVIDENCE'):
            Path(os.environ['FEATURE_AGENT_BINDING_EVIDENCE']).write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        server.should_exit=True
        await asyncio.to_thread(thread.join,10)
        sock.close()
        await database.dispose()
        assert not thread.is_alive()
