"""显式启用的三个真实 DeepSeek 业务验收，原 Runtime 和业务 Validator。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import uuid4
import pytest
from sqlalchemy import text,select
from core.skills.catalog import SkillCatalog
from core.skills.tool_adapter import SkillTool,TOOL_NAMES
from core.runtime.model_invocation import GeneratorModelAdapter
from core.runtime.execution_aggregate import canonical_payload_digest as digest
from core.agent_platform.application import AgentApplicationService,ExecutionRequest
from core.agent_platform.registry import AgentRegistrationBundle
from core.agent_platform.contracts import AgentRegistration
from core.chat_service import ChatService
from tests.test_agent_loop_runtime import assembly
from tests.test_ci_incident_diagnosis import user
from tests.test_skill_context_lifecycle import generic_definition
ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=Path(os.getenv("LOCAL_AGENT_SKILL_BUSINESS_EVIDENCE_PATH", str(ROOT/".ai/handoff/stage18_skill/91_wp3_business_evidence.json")))


class Capture(GeneratorModelAdapter):
    def __init__(self,engine):
        super().__init__(engine)
        self.requests=[]
    async def ainvoke(self,messages,**kwargs):
        self.requests.append(dict(messages=deepcopy(messages),messages_digest=digest(messages),
            options=deepcopy(kwargs.get("generation_options")),max_tokens=kwargs["max_tokens"],snapshot_id=kwargs["run_context"].loop_context_snapshot_id))
        return await super().ainvoke(messages,**kwargs)


async def records(database,run_id):
    tables=("runtime_run_executions","runtime_step_executions","runtime_loop_frames","runtime_loop_iterations","runtime_loop_actions","runtime_model_invocations","runtime_tool_invocations","runtime_context_snapshots")
    result={}
    async with database.session() as session:
        for table in tables:
            result[table]=[dict(r) for r in (await session.execute(text("select * from "+table+" where run_id=:rid"),{"rid":run_id})).mappings()]
    return result


def save(case,value):
    data=json.loads(EVIDENCE.read_text(encoding="utf-8")) if EVIDENCE.exists() else {}
    data.setdefault(case,[]).append(value)
    EVIDENCE.write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str),encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("LOCAL_AGENT_SKILL_REAL_BUSINESS")!="1",reason="仅显式启用真实业务验收")
@pytest.mark.parametrize("case",["C2","C3","C4"])
async def test_real_skill_business(clean_database,tmp_path,case):
    from core.llm_engine import RemoteLLMEngine
    from core.persistence.sync_bridge import SyncPersistenceBridge
    from core.persistence.memory import PostgresMemoryManager,PostgresMemoryManagerBridge
    from core.runtime.model_selection import ModelProfile,ModelProfileId,ModelCostProfile
    from core.runtime.tool_snapshot_store import PostgresToolResolutionSnapshotStore
    from core.auth import AuthorizationService
    from core.agent_platform.ci_diagnosis import definition as ci_definition,validate_report
    from core.agent_platform.ci_diagnosis_tools import CIDiagnosisTool,TOOL_NAMES as CI_TOOLS
    from core.feature_impact.agent import definition as feature_definition,FeatureInvestigationService
    from core.feature_impact.tools import FeatureEvidenceTool,TOOL_NAMES as FEATURE_TOOLS
    from core.feature_impact.service import FeatureImpactService
    from core.incidents.service import IncidentService
    from core.persistence.models import ObjectOwnershipRow,RuntimeLoopActionRow
    from core.runtime.memory_scope import MemoryAccessContext
    from core.feature_impact.models import ReportRow,InvestigationRow,TaskRow
    from core.feature_impact.agent_reports import validated_agent_report
    engine=RemoteLLMEngine("https://api.deepseek.com",os.getenv("LOCAL_AGENT_REMOTE_MODEL_NAME","deepseek-flash"),api_key=os.environ["LOCAL_AGENT_REMOTE_API_KEY"],
        timeout_seconds=120,verify_tls=True,enable_thinking=False,provider_kind="deepseek")
    model=Capture(engine)
    bridge=SyncPersistenceBridge(asyncio.get_running_loop())
    catalog=SkillCatalog.discover([ROOT/"skills"])
    agent=(ci_definition() if case=="C2" else feature_definition() if case=="C3" else generic_definition())
    # 复用 WP2 已有隔离预算证据；不修改生产默认。
    agent=replace(agent,business_options={**agent.business_options,"loop_policy":{**agent.business_options["loop_policy"],"total_tokens":262144,"cost_units":262144,"max_tool_calls":10}})
    if case=="C4":
        agent=replace(agent,business_options={k:v for k,v in agent.business_options.items() if k!="context_policy"})
    principal=await user(clean_database)
    principal=replace(principal,roles=frozenset({"USER","OPERATOR"}))
    service=IncidentService(clean_database) if case=="C2" else FeatureImpactService(clean_database)
    from core.runtime.tool_registry import ToolRegistry
    from core.skills.tool_adapter import register_tools as register_skill_tools
    from core.agent_platform.ci_diagnosis_tools import register_tools as register_ci_tools
    from core.feature_impact.tools import register_tools as register_feature_tools
    registrations=ToolRegistry()
    register_skill_tools(registrations,catalog)
    if case=="C2": register_ci_tools(registrations,service,bridge)
    elif case=="C3": register_feature_tools(registrations,service,bridge)
    else: agent=replace(agent,allowed_tools=frozenset(TOOL_NAMES))
    tools=[r.adapter for r in registrations.startup_registrations]
    bundle=AgentRegistrationBundle(registrations=(AgentRegistration(agent),),tool_grants={t.spec.tool_name:frozenset({agent.agent_id}) for t in tools})
    profile=ModelProfile(ModelProfileId.LOCAL_FAST,1000000,4096,True,True,True,True,1,1,ModelCostProfile(ModelProfileId.LOCAL_FAST,True,fixed_call_cost_units=1,estimated_latency_ms=1),
        True,"skills-business",supports_native_tool_calling=True,provider_kind="deepseek",model_identity=engine.model_name,
        request_counting_mode="BOUNDED_ESTIMATE",request_counting_version="utf8-byte-upper-bound.v1",request_framing_bound=64,request_message_framing_bound=16,output_accounting_contract="bounded-visible-output.v1")
    factory,repo,_,_=await assembly(clean_database,tmp_path,[],tools=tools,context_model=model,business_bundle=bundle,model_profile=profile,max_tokens=4096,tool_registrations=registrations.startup_registrations,
        memory_manager=PostgresMemoryManagerBridge(PostgresMemoryManager(clean_database),bridge))
    router=factory._router
    router.skill_catalog=catalog
    router.tool_snapshot_store=PostgresToolResolutionSnapshotStore(clean_database)
    application=AgentApplicationService(ChatService(router,coordinated_runtime_factory=factory),router.agent_registry)
    evidence=dict(case=case,model=engine.model_name,mode="NON_THINKING",status="STARTED")
    saved=False
    try:
        if case=="C3":
            task=await service.create(principal,str(uuid4()),public_task="T2")
            class Supervisor:
                def __init__(self): self.tasks=[]
                async def acquire_slot(self): return True
                def release_slot(self,reserved): pass
                def spawn(self,run_id,stream,*,reserved_slot):
                    async def consume(): return [event async for event in stream]
                    self.tasks.append(asyncio.create_task(consume()))
            supervisor=Supervisor()
            investigation_service=FeatureInvestigationService(service,application,AuthorizationService(clean_database),repo,supervisor,execute_models=True)
            started=await investigation_service.start(principal,task["task_id"],str(uuid4()))
            run_id=started["run_id"]
            await asyncio.gather(*supervisor.tasks)
        else:
            if case=="C2":
                instance,_=await service.create_scenario(principal,"report_pipeline_failure",str(uuid4()))
                execution,incident,_=await service.execute(principal,instance.instance_id,str(uuid4()))
                investigation,_=await service.admit(principal,incident.incident_id,str(uuid4()))
                run_id,session_id=investigation.run_id,investigation.session_id
                prompt="请检查该 CI 执行对应的环境配置和用例要求是否兼容，重点核对相关字段版本与执行前置条件，并依据实际证据给出结论。incident_id="+incident.incident_id+" execution_id="+execution.execution_id
            else:
                run_id,session_id=str(uuid4()),str(uuid4())
                prompt="请解释 Python 列表切片的基本用法，并给出一个简单例子。"
            access=MemoryAccessContext.from_principal(principal,agent.agent_id,session_id)
            authorization=AuthorizationService(clean_database)
            await authorization.bind_new(principal,"RUN",run_id)
            await authorization.bind_new(principal,"CONVERSATION",access.conversation_id)
            events=[event async for event in application.stream(ExecutionRequest(agent_id=agent.agent_id,input=prompt,run_id=run_id,session_id=session_id,timeout_seconds=180),
                retrieval_cache_authz_domain=principal.authz_domain_id,memory_access_context=access)]
        image=await repo.load(run_id)
        evidence.update(run_id=run_id,records=await records(clean_database,run_id),requests=model.requests,status=image.root.status)
        raw=image.steps[0].typed_result_payload.get("content","") if image.steps[0].typed_result_payload else ""
        async with clean_database.session() as session:
            actions=(await session.execute(select(RuntimeLoopActionRow).where(RuntimeLoopActionRow.run_id==run_id))).scalars().all()
            if case=="C2":
                evidence["ci_inputs"]=dict(investigation={k:v for k,v in vars(investigation).items() if not k.startswith("_")},incident={k:v for k,v in vars(incident).items() if not k.startswith("_")},execution={k:v for k,v in vars(execution).items() if not k.startswith("_")})
                if image.root.status=="SUCCEEDED": evidence["validated_report"]=validate_report(raw,investigation,incident,execution,image,actions)
            elif case=="C3":
                binding=await session.get(InvestigationRow,run_id)
                task_row=await session.get(TaskRow,binding.task_id)
                evidence["feature_inputs"]=dict(binding={k:v for k,v in vars(binding).items() if not k.startswith("_")},task={k:v for k,v in vars(task_row).items() if not k.startswith("_")})
                reports=(await session.execute(select(ReportRow).where(ReportRow.task_id==binding.task_id))).scalars().all()
                evidence["business_reports"]=[dict(rejection=r.rejection,payload=r.payload,source=r.source_record) for r in reports]
                if image.root.status=="SUCCEEDED": evidence["validated_report"]=validated_agent_report(binding,image,actions,task_row)[1]
        evidence["final_output"]=raw
        save(case,evidence)
        saved=True
        assert image.root.status=="SUCCEEDED",(case,image.root.status)
        frame=image.loop_frames[0]
        assert frame.pinned_config["skills"]
        if case=="C4":
            assert not image.tool_invocations
            assert "切片" in raw or "slice" in raw.lower()
        else:
            expected="ci-environment-check" if case=="C2" else "feature-impact-analysis"
            invocations=image.tool_invocations
            assert {t.tool_name for t in invocations if t.state=="COMMITTED"} >= set(TOOL_NAMES)|set(CI_TOOLS if case=="C2" else FEATURE_TOOLS)
            contents=[json.loads(t.committed_result["output"]["content"]) for t in invocations if t.tool_name=="load_skill" and t.state=="COMMITTED"]
            assert any(c["name"]==expected for c in contents)
            assert evidence.get("validated_report")
    except BaseException as exc:
        if not saved:
            evidence["failure_type"]=type(exc).__name__
            if "run_id" in locals(): evidence.update(run_id=run_id,records=await records(clean_database,run_id),requests=model.requests)
            save(case,evidence)
        raise
    finally:
        await engine.aclose()
