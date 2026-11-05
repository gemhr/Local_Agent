"""CI 最终 JSON 请求及持久 Step 收尾的定向回归；仅受控模型/HTTPX/测试 PG。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace as NS

import httpx
import pytest
from sqlalchemy import select

from core.agent_platform.ci_diagnosis import definition
from core.llm_engine import RemoteLLMEngine
from core.persistence.models import RuntimeEventJournalRow, RuntimeStepExecutionRow, RunControlRow
from core.runtime.agent_loop.contracts import LoopStopped
from core.runtime.agent_loop.context import _pin_value
from core.runtime.agent_loop.handoff import loop_options
from core.runtime.model_invocation import GeneratorModelAdapter, ModelAdapterResponse, ModelInvocationRouter
from core.runtime.execution_aggregate import canonical_payload_digest
from tests.test_ci_targeted_remediation import drive, vector


def test_final_json_wire_and_refusal_keep_original_contract(monkeypatch):
    v=vector()
    agent=definition()
    frame=NS(context_seed={'protocol_mode':'native'},adaptive_decision={'allowed_actions':'FINAL_ONLY'},
        pinned_config={'agent':_pin_value(agent)},frame_role='ROOT')
    options=loop_options(frame)
    assert options=={'tools':[],'tool_choice':'none','response_format':{'type':'json_object'}}
    other=deepcopy(frame);other.pinned_config['agent']['agent_id']='ordinary_agent'
    assert 'response_format' not in loop_options(other)
    investigating=deepcopy(frame);investigating.adaptive_decision=None;investigating.pinned_config['tools']=[]
    assert loop_options(investigating)=={'tools':[],'tool_choice':'auto'}
    old=deepcopy(frame);old.pinned_config['agent']['business_options'].pop('final_response_format')
    assert 'response_format' not in loop_options(old)
    report=dict(schema_version='ci-diagnosis.v1',kind='REPORT',incident_id=v['incident_id'],diagnosis_status='SUPPORTED',
        failure_stage='PARSER',root_cause_hypothesis='结构测试',supporting_evidence=v['supporting_evidence'],
        uncertainties=[],recommended_checks=['核对能力'],recommended_owner_role='TOOL_MAINTAINER')
    async def request(delta,opts):
        sent=[]
        def transport(req):
            sent.append(req.content)
            events=[{'choices':[{'index':0,'delta':delta,'finish_reason':None}]},
                {'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]}]
            return httpx.Response(200,text=''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n',
                headers={'content-type':'text/event-stream'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            engine=RemoteLLMEngine('https://isolated.invalid','deepseek-flash',provider_kind='deepseek',client=client)
            assert engine.supports_provider_structured_output() is False
            adapter=GeneratorModelAdapter(engine)
            messages=[{'role':'system','content':'Return JSON report.'}]
            body=engine._body(messages,temperature=0.7,max_tokens=4096,enable_thinking=False,stream=True,**opts)
            measured=httpx.Request('POST','https://isolated.invalid',json=body).content
            result=await adapter.ainvoke(messages,max_tokens=4096,generation_options=opts,
                run_context=NS(execution_policy='durable-agent-loop.v2',raise_if_inactive=lambda:None))
            assert sent==[measured]
            assert json.loads(sent[0]).get('response_format')==opts.get('response_format')
            return result
    for delta,accepted in [({'content':json.dumps(report)},True),({'content':'   '},False),
        ({'content':v['final_output']},False),({'refusal':'拒答'},True),
        ({'content':json.dumps({'kind':'REFUSAL','reason':'拒答'})},False)]:
        response=asyncio.run(request(delta,options))
        if accepted:assert drive(monkeypatch,response).complete
        else:
            with pytest.raises(LoopStopped,match='LOOP_DECISION_INVALID'):drive(monkeypatch,response)
    assert asyncio.run(request({'content':'ordinary'},{'tools':[],'tool_choice':'none'})).output=='ordinary'


async def controlled_json_runtime(database,tmp_path):
    from tests.test_ci_adaptive_stop import build
    from core.runtime.model_invocation import ModelAdapterResolver
    q=await build(database,tmp_path)
    q.http_requests=[]
    def transport(request):
        q.http_requests.append(request)
        body=json.loads(request.content)
        response=q.model.invoke_request(body['messages'],generation_options={k:body[k] for k in ('tools','tool_choice')},
            run_context=NS(loop_iteration_no=len(q.http_requests)),max_tokens=body['max_tokens'])
        delta={'content':response.output}
        if response.native_tool_calls:
            delta={**response.assistant_message,'tool_calls':[dict(index=i,**call) for i,call in enumerate(response.assistant_message['tool_calls'])]}
        events=[{'choices':[{'index':0,'delta':delta,'finish_reason':None}]},
            {'choices':[{'index':0,'delta':{},'finish_reason':response.finish_reason}]}]
        return httpx.Response(200,text=''.join('data: '+json.dumps(e,ensure_ascii=False)+'\n\n' for e in events)+'data: [DONE]\n\n',
            headers={'content-type':'text/event-stream'})
    q.client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
    q.engine=RemoteLLMEngine('https://isolated.invalid','deepseek-flash',provider_kind='deepseek',client=q.client)
    profile=q.factory._router.model_profiles[0]  # 原受控 synthetic BOUNDED_ESTIMATE，不给真实远程 Profile 伪造计量。
    q.factory._router.model_adapter_resolver=ModelAdapterResolver({profile.profile_id:GeneratorModelAdapter(q.engine)})
    return q


@pytest.mark.asyncio
async def test_json_snapshot_request_digest_and_actual_wire_match(clean_database,tmp_path):
    from tests.test_ci_adaptive_stop import run,actions
    from core.agent_platform.ci_diagnosis import validate_report
    q=await controlled_json_runtime(clean_database,tmp_path)
    try:
        result,image=await run(q)
        assert result.status.value=='SUCCEEDED'
        report=validate_report(image.steps[0].typed_result_payload['content'],q.row,q.incident,q.execution,image,await actions(q))
        assert report['diagnosis_status']=='SUPPORTED'
        assert len(image.models)==len(q.http_requests)==4
        assert image.steps[0].status=='SUCCEEDED'
        for snapshot in image.context_snapshots:
            assert snapshot.payload['options_digest']==canonical_payload_digest(snapshot.payload['options'])
        for row,req in zip(image.models,q.http_requests):
            snapshot=next(s for s in image.context_snapshots if s.snapshot_id==row.context_snapshot_id)
            options=snapshot.payload['options'];body=json.loads(req.content)
            assert body.get('response_format')==options.get('response_format')
            assert body['messages']==snapshot.payload['messages']
            assert body['tools']==options['tools'] and body['tool_choice']==options['tool_choice']
            assert row.request_digest==ModelInvocationRouter._durable_model_request_digest(snapshot.payload['messages'],
                max_tokens=body['max_tokens'],generation_options=options)
            assert row.request_assessment['reliable'] is True and row.request_assessment['admissible'] is True
            assert 'experimental_request' not in row.request_assessment
        assert all('response_format' not in json.loads(r.content) for r in q.http_requests[:-1])
        assert json.loads(q.http_requests[-1].content)['response_format']=={'type':'json_object'}
        # 复用夹具原有 131072 测试额度；生产 CI 默认额度仍为 32768。
        assert image.root.budget_totals['max_total_tokens']==131072
        assert definition().business_options['loop_policy']['total_tokens']==32768
    finally:await q.client.aclose()


async def journal(database,run_id):
    async with database.session() as session:
        return [(r.sequence,r.event_type,json.loads(r.safe_payload)) for r in
            (await session.scalars(select(RuntimeEventJournalRow).where(RuntimeEventJournalRow.run_id==run_id)
                .order_by(RuntimeEventJournalRow.sequence))).all()]


@pytest.mark.asyncio
async def test_step_terminal_pg_budget_loop_and_normal_fenced_completion(clean_database,tmp_path,monkeypatch):
    from tests.test_ci_adaptive_stop import build,run
    for case in ('budget','invalid_final','normal'):
        q=await build(clean_database,tmp_path,adaptive=case=='normal',loop_limits={'total_tokens':1} if case=='budget' else None)
        if case=='invalid_final':
            q.model.invoke_request=lambda *a,**kw:ModelAdapterResponse(vector()['final_output'],finish_reason='stop',tool_call_count=0)
        complete=q.repo.complete_step;captured=[]
        async def checked(lease,**kw):
            before=await journal(clean_database,q.row.run_id)
            with pytest.raises(RuntimeError,match='stale step attempt'):
                await complete(lease,**{**kw,'attempt':kw['attempt']+1})
            from core.runtime.run_control import OwnershipLost
            with pytest.raises(OwnershipLost):
                await complete(replace(lease,fencing_token=lease.fencing_token+1),**kw)
            assert await journal(clean_database,q.row.run_id)==before
            captured.append((lease,kw))
            result=await complete(lease,**kw)
            # 相同完成再次调用不应追加第二个 Journal 事件。
            after=await journal(clean_database,q.row.run_id)
            assert await complete(lease,**kw)
            assert await journal(clean_database,q.row.run_id)==after
            return result
        with monkeypatch.context() as patch:
            patch.setattr(q.repo,'complete_step',checked)
            result,image=await run(q)
        expected='SUCCEEDED' if case=='normal' else 'CANCELLED'
        assert image.steps[0].status==expected
        assert result.status.value==image.root.status==('SUCCEEDED' if case=='normal' else 'FAILED')
        assert not result.cleanup_error_codes
        if case!='normal':
            assert image.loop_frames[0].phase=='STOPPED'
            assert image.loop_frames[0].stop_code==('LOOP_BUDGET_EXHAUSTED' if case=='budget' else 'LOOP_DECISION_INVALID')
        completed=[e for e in await journal(clean_database,q.row.run_id) if e[1]=='STEP_COMPLETED']
        assert len(completed)==1 and completed[0][2]['status']==expected
        assert image.steps[0].safe_error==completed[0][2]['safe_error_code']
        assert len(captured)==1
        async with clean_database.session() as session:
            row=(await session.scalars(select(RuntimeStepExecutionRow).where(RuntimeStepExecutionRow.run_id==q.row.run_id))).one()
            control=await session.get(RunControlRow,q.row.run_id)
            assert row.status==expected and control.state=='CLOSED'
        from core.runtime.run_control import OwnershipLost
        with pytest.raises(OwnershipLost):await complete(captured[0][0],**captured[0][1])
        assert len([e for e in await journal(clean_database,q.row.run_id) if e[1]=='STEP_COMPLETED'])==1


@pytest.mark.asyncio
async def test_unreliable_remote_rejected_without_http_or_model_start(clean_database,tmp_path):
    from tests.test_ci_adaptive_stop import run
    from tests.test_request_counting_admission import remote_profile
    from core.runtime.model_invocation import ModelAdapterResolver
    from core.runtime.model_context import assess_request
    from core.agent_platform.adaptive_evaluation import Configuration
    from core.runtime.request_counting_admission import eligible,validate_record
    from pydantic import ValidationError
    q=await controlled_json_runtime(clean_database,tmp_path)
    profile=remote_profile()
    q.factory._router.model_profiles=(profile,)
    q.factory._router.model_adapter_resolver=ModelAdapterResolver({profile.profile_id:GeneratorModelAdapter(q.engine)})
    assessment=assess_request([{'role':'user','content':'read context'}],{},512,profile)
    assert assessment.mode=='HEURISTIC' and not assessment.reliable and not assessment.admissible
    stale={'schema_version':'experimental-request-admission.v1','experimental_override':True}
    assert not validate_record(stale) and not eligible(assessment,stale,profile=profile)
    with pytest.raises(ValidationError) as error:Configuration.model_validate({'experimental_admission':{}})
    assert any(e['loc']==('experimental_admission',) and e['type']=='extra_forbidden' for e in error.value.errors())
    try:
        result,image=await run(q)
        assert result.status.value=='FAILED'
        assert image.loop_frames[0].stop_code=='CONTEXT_BUDGET_UNCERTAIN'
        assert not q.http_requests and not image.models
        assert image.steps[0].status=='CANCELLED'
    finally:await q.client.aclose()


@pytest.mark.asyncio
async def test_step_cleanup_transaction_failure_is_reported_without_fake_journal(clean_database,tmp_path,monkeypatch):
    from tests.test_ci_adaptive_stop import build,run
    q=await build(clean_database,tmp_path,adaptive=False)
    q.model.invoke_request=lambda *a,**kw:ModelAdapterResponse(vector()['final_output'],finish_reason='stop',tool_call_count=0)
    complete=q.repo.complete_step
    async def fail(lease,**kwargs):
        async def fail_append(session):raise RuntimeError('controlled Journal transaction failure')
        return await complete(lease,**{**kwargs,'journal_append':fail_append})
    with monkeypatch.context() as patch:
        patch.setattr(q.repo,'complete_step',fail)
        result,image=await run(q)
    assert result.status.value=='FAILED'
    assert 'ACTIVE_STEP_CLEANUP_FAILED' in result.cleanup_error_codes
    assert image.steps[0].status=='RUNNING'
    assert not [e for e in await journal(clean_database,q.row.run_id) if e[1]=='STEP_COMPLETED']
