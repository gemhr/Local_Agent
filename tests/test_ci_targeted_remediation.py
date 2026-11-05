"""D1 预算、错误投影及 FINAL/REFUSAL 的最小定向回归；无真实模型请求。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from core.agent_platform.ci_diagnosis import definition
from core.runtime.agent_loop.contracts import LoopStopped
from core.runtime.agent_loop.driver_v2 import AgentLoopDriverV2
from core.runtime.budget import BudgetLedger, BudgetUsage, RunBudget, BudgetExceededError, UsageSource
from core.runtime.execution_aggregate import canonical_payload_digest as digest
from core.runtime.model_context import ModelContextRequirements
from core.runtime.model_invocation import ModelAdapterResponse
from core.runtime.model_selection import ModelProfile, ModelProfileId, ModelCostProfile, ModelSelectionPolicy, ModelSelectionRequest, ModelPreference, ModelSelectionError
from core.runtime.planning import TaskCapabilityRequirements, RiskLevel

VECTOR = Path(__file__).parent / 'fixtures/ci_diagnosis_live_failure.json'


def vector():
    return json.loads(VECTOR.read_text(encoding='utf-8'))


def test_archived_budget_auto_forced_capacity_and_ledger():
    v=vector()['selection']
    p=deepcopy(v['profile']);p['profile_id']=ModelProfileId(p['profile_id'])
    p['cost_profile']=ModelCostProfile(**{**p['cost_profile'],'profile_id':p['profile_id']});profile=ModelProfile(**p)
    a=NS(input_bound=v['estimated_input_tokens'],output_reserve=v['output_reserve'])
    ledger=BudgetLedger.from_durable(RunBudget(**v['budget_totals']),committed_usage=BudgetUsage(**v['budget_consumed']))
    cap=TaskCapabilityRequirements(requires_tools=True,risk_level=RiskLevel.LOW)
    req=ModelContextRequirements(a.input_bound,a.input_bound+a.output_reserve,True,False,False,11,0,0,False,True)
    policy=ModelSelectionPolicy();before=ledger.snapshot()
    assert (a.input_bound,a.output_reserve,before.remaining.total_tokens)==(16694,4096,24699)
    for preference in (ModelPreference.AUTO,ModelPreference.FORCE_REMOTE):
        request=ModelSelectionRequest('ci_incident_investigator',cap,req,preference,(profile,),ledger.snapshot())
        assert policy.select(request).selected_profile==profile.profile_id
        # 实际少于20790才拒绝，不通过增加原预算解决。
        tight=BudgetLedger.from_durable(ledger.budget,committed_usage=BudgetUsage(total_tokens=11979,_allow_independent_total=True))
        with pytest.raises(ModelSelectionError):policy.select(replace(request,budget_snapshot=tight.snapshot()))
        assert policy.select(replace(request,available_profiles=(replace(profile,context_window=22870),))).selected_profile==profile.profile_id
        with pytest.raises(ModelSelectionError):policy.select(replace(request,available_profiles=(replace(profile,context_window=22869),)))
    assert ledger.snapshot().committed_usage==before.committed_usage and ledger.snapshot().active_reservation_count==0
    usage=BudgetUsage(model_calls=1,remote_model_calls=1,input_tokens=a.input_bound,output_tokens=a.output_reserve,total_tokens=20790)
    held=ledger.reserve(usage,reservation_type='regression');ledger.release(held)
    assert ledger.snapshot().remaining==before.remaining
    held=ledger.reserve(usage,reservation_type='regression');ledger.commit(held,BudgetUsage(input_tokens=7,output_tokens=3,total_tokens=12),usage_source=UsageSource.ACTUAL)
    assert ledger.snapshot().committed_usage.total_tokens==before.committed_usage.total_tokens+12
    with pytest.raises(BudgetExceededError):ledger.reserve(BudgetUsage(total_tokens=32768,_allow_independent_total=True),reservation_type='insufficient')


def test_known_model_selection_error_projects_without_sensitive_text():
    from core.runtime import AgentState, ParallelExecutor, ParallelFailureMode, create_run_context
    from tests.test_parallel_execution import claims
    async def run(error):
        context,_=create_run_context(entry_agent_id='test');state=AgentState.for_run_context(context.run_id);batch=claims(state,'answer')
        class Driver:
            async def execute(self,claim,context):raise error
        class Completion:
            async def commit(self,**kw):raise AssertionError('failed driver cannot commit')
        report=await ParallelExecutor().execute(claims=batch,state=state,run_context=context,driver=Driver(),
            failure_mode=ParallelFailureMode.BEST_EFFORT,completion_owner=Completion())
        result=report.outcomes[0]
        assert result.status.value=='FAILED' and 'secret' not in (result.error_message or '')
        assert state.steps['answer'].error_code==result.error_code
        return result.error_code
    for reason in ('budget_unavailable','forced_remote_budget_unavailable','forced_profile_budget_unavailable'):
        assert asyncio.run(run(ModelSelectionError(reason)))=='MODEL_SELECTION_BUDGET_UNAVAILABLE'
    for error in (ModelSelectionError('unknown secret'),RuntimeError('budget_unavailable secret')):
        assert asyncio.run(run(error))=='AGENT_STEP_FAILED'


def drive(monkeypatch,response,agent=None,observe_restriction=None):
    from core.runtime.agent_loop import driver_v2
    from core.runtime.agent_loop.context import _pin_value
    from core.runtime.agent_loop.adaptive_stop import POLICY, POLICY_DIGEST
    v=vector();agent=agent or definition()
    frame=NS(**v['final_frame']);frame.phase='NEED_DECISION';frame.execution_agent_id=agent.agent_id
    frame.context_seed={'protocol_mode':'native'};frame.row_version=1
    if agent.agent_id!='ci_incident_investigator':frame.adaptive_decision=None
    frame.pinned_config={'agent':_pin_value(agent),'adaptive_stop':{**POLICY,'digest':POLICY_DIGEST}}
    frame.pinned_config['delegation_targets']={'reader':{}}
    frame.config_digest=digest(frame.pinned_config)
    iteration=NS(iteration_no=4,state='OPEN',decision_id=None)
    binding={'output':response.output,'finish_reason':response.finish_reason,'tool_call_count':response.tool_call_count,
        'native_tool_calls':[],'refusal':response.refusal}
    row=NS(state='COMPLETED',result_binding=binding,result_digest=digest(binding))
    class Repo:
        accepted=False
        async def load_loop(self,*a):return frame,[iteration]
        async def accept_loop_batch(self,*a,**kw):
            self.accepted=True;frame.phase='RESULT_READY';iteration.decision_id='accepted';iteration.decision_digest=kw['decision'].digest
        async def get_loop_model_response(self,*a):return row
    repo=Repo()
    router=NS(agent_registry=NS(resolve=lambda _:NS(definition=agent)),model_profiles=(),max_tokens=4096,
        _invoke_model_contract=lambda **_:NS(response=response,model_attempt_ref='answer:1:4'))
    context=NS(execution_repository=repo,durable_lease=object(),memory_guard=None,raise_if_inactive=lambda:None)
    emitter=NS(parent=NS(submit_coroutine_from_worker=asyncio.run))
    coordinator=NS(event_emitter=NS(for_step=lambda _:emitter))
    with monkeypatch.context() as patch:
        patch.setattr(driver_v2,'pinned_loop_config',lambda *a,**kw:frame.pinned_config)
        patch.setattr(driver_v2,'consider_stop',lambda *a:None)
        patch.setattr(driver_v2,'prepare_context',lambda *a:{'messages':[],'options':{'tools':[],'tool_choice':'none'}})
        if observe_restriction is not None:
            original=driver_v2.enforce_final_only
            def restriction(*args):observe_restriction.append(args[1].kind);return original(*args)
            patch.setattr(driver_v2,'enforce_final_only',restriction)
        try:
            result=AgentLoopDriverV2(router,coordinator).execute(NS(step_id='answer',preferred_agent=agent.agent_id,capability_requirements=TaskCapabilityRequirements()),context)
            assert repo.accepted and result.complete
            return result
        except LoopStopped:
            assert not repo.accepted
            raise


def test_ci_final_guard_preserves_refusal_and_other_agent_contracts(monkeypatch):
    business=vector()
    report=dict(schema_version='ci-diagnosis.v1',kind='REPORT',incident_id=business['incident_id'],diagnosis_status='SUPPORTED',
        failure_stage='PARSER',root_cause_hypothesis='仅测试结构',supporting_evidence=business['supporting_evidence'],
        uncertainties=[],recommended_checks=['核对能力'],recommended_owner_role='TOOL_MAINTAINER')
    raw=json.dumps(report,ensure_ascii=False)
    assert drive(monkeypatch,ModelAdapterResponse(raw,finish_reason='stop',tool_call_count=0)).content==raw
    dsml=business['final_output']
    with pytest.raises(LoopStopped,match='LOOP_DECISION_INVALID'):
        drive(monkeypatch,ModelAdapterResponse(dsml,finish_reason='stop',tool_call_count=0))
    refusal=ModelAdapterResponse('',refusal='拒答',finish_reason='stop',tool_call_count=0)
    assert drive(monkeypatch,refusal).content=='拒答'
    other=replace(definition(),agent_id='ordinary_agent')
    assert drive(monkeypatch,ModelAdapterResponse(dsml,finish_reason='stop',tool_call_count=0),other).content==dsml
    other=replace(other,output_schema={'type':'object'})
    with pytest.raises(LoopStopped,match='LOOP_DECISION_INVALID'):
        drive(monkeypatch,ModelAdapterResponse(dsml,finish_reason='stop',tool_call_count=0),other)


def test_final_only_still_rejects_native_tool_and_handoff(monkeypatch):
    from tests.test_ci_adaptive_stop import calls
    for name,args,kind in (('ci_read_execution_evidence',{'evidence_id':'forbidden'},'TOOL_BATCH'),
            ('delegate_to_agent',{'target_agent_id':'reader','task':'forbidden'},'HANDOFF')):
        from core.runtime.agent_loop.contracts import HANDOFF_FUNCTION
        if kind=='HANDOFF':name=HANDOFF_FUNCTION
        checked=[]
        with pytest.raises(LoopStopped,match='LOOP_DECISION_INVALID'):
            drive(monkeypatch,calls(('illegal',name,args)),observe_restriction=checked)
        assert checked==[kind]
