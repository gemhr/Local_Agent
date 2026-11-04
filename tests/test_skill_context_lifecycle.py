"""首次正文保留、重复组压缩与持久恢复的定向验证。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import pytest
from core.skills.catalog import SkillCatalog
from core.skills.tool_adapter import SkillTool, TOOL_NAMES
from core.runtime.agent_loop.context import context_sources_v2
from core.runtime.agent_loop.contracts import LoopSuspended
from core.runtime.model_invocation import ModelAdapterResponse, NativeToolCall
from tests.test_agent_batch_handoff import Provider
from tests.test_agent_loop_runtime import assembly, ObservationTool
from core.agent_platform.ci_diagnosis import definition
from core.agent_platform.contracts import AgentRegistration
from core.agent_platform.registry import AgentRegistrationBundle

ROOT = Path(__file__).resolve().parents[1]


def call(name, args, identity):
    raw = json.dumps(args)
    return ModelAdapterResponse("", native_tool_calls=(NativeToolCall(identity, name, raw),), tool_call_count=1, finish_reason="tool_calls",
        assistant_message={"role":"assistant","content":None,"tool_calls":[{"id":identity,"type":"function","function":{"name":name,"arguments":raw}}]})


def generic_definition():
    original = definition()
    return replace(original, agent_id="skill_context_reader", instructions="按任务使用工具；无关任务直接回答。", display_name="方法读取助手",
        allowed_tools=frozenset((*TOOL_NAMES,"loop_a","loop_b")), business_options={**original.business_options,
            "context_policy":{"version":2,"trigger_percent":63,"target_percent":55},
            "loop_policy":{**original.business_options["loop_policy"],"total_tokens":262144,"cost_units":262144,"max_tool_calls":10}})


async def build(database, tmp_path, model, *, catalog=None, repository=None):
    catalog = catalog or SkillCatalog.discover([ROOT/"skills"])
    agent = generic_definition()
    bundle = AgentRegistrationBundle(registrations=(AgentRegistration(agent),),tool_grants={n:frozenset({agent.agent_id}) for n in agent.allowed_tools})
    tools = [SkillTool(n,catalog) for n in TOOL_NAMES] + [ObservationTool("loop_a"),ObservationTool("loop_b")]
    from core.runtime.model_selection import ModelProfile,ModelProfileId,ModelCostProfile
    profile=ModelProfile(ModelProfileId.LOCAL_FAST,22000,128,True,True,True,True,1,1,
        ModelCostProfile(ModelProfileId.LOCAL_FAST,False,fixed_call_cost_units=1,estimated_latency_ms=1),False,"skill-context",
        supports_native_tool_calling=True,provider_kind="synthetic",model_identity="controlled",
        request_counting_mode="BOUNDED_ESTIMATE",request_counting_version="utf8-byte-upper-bound.v1",request_framing_bound=64,
        request_message_framing_bound=16,output_accounting_contract="bounded-visible-output.v1")
    factory,repo,_,_=await assembly(database,tmp_path,[],tools=tools,context_model=model,business_bundle=bundle,model_profile=profile,repository=repository)
    from core.runtime.tool_snapshot_store import PostgresToolResolutionSnapshotStore
    factory._router.tool_snapshot_store=PostgresToolResolutionSnapshotStore(database)
    factory._router.skill_catalog=catalog
    return factory,repo,tools


def save_result(key, value):
    path=ROOT/".ai/handoff/stage18_skill/92_wp3_compaction_recovery_evidence.json"
    payload=json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    payload[key]=value
    path.write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding="utf-8")


@pytest.mark.asyncio
async def test_first_body_survives_real_runtime_compaction(clean_database,tmp_path):
    class Model(Provider):
        def invoke_request(self,messages,*,run_context,**kwargs):
            self.requests.append(deepcopy(messages))
            if run_context.loop_model_purpose=="COMPACTION":
                sources=json.loads(messages[-1]["content"])
                return ModelAdapterResponse(json.dumps({"version":1,"groups":[{"source_ref":s["source_ref"],"facts":["已读取的方法资料"],"decisions":[],"open_items":[]} for s in sources]}),finish_reason="stop",tool_call_count=0)
            return self.responses.pop(0)
    model=Model([call("load_skill",{"name":"ci-failure-investigation"},"first"),
        call("loop_a",{},"between"),call("load_skill",{"name":"ci-failure-investigation"},"repeat"),
        call("read_skill_resource",{"name":"ci-failure-investigation","relative_path":"references/failure-evidence-guide.md"},"reference"),
        call("loop_a",{},"a"),call("loop_b",{},"b"),ModelAdapterResponse("完成",finish_reason="stop",tool_call_count=0)])
    factory,repo,tools=await build(clean_database,tmp_path,model)
    scope=await factory.create_run_scope("skill_context_reader","请使用 load_skill 和 read_skill_resource 后调用 loop_a 和 loop_b",persist=False)
    try: result=await scope.execute()
    finally: await scope.close(abort=True)
    image=await repo.load(scope.run_id)
    assert result.status.value=="SUCCEEDED",(result.error_code,result.safe_message)
    frame=image.loop_frames[0]
    groups=context_sources_v2(frame,image.loop_iterations)
    assert groups[0]["mandatory"] and groups[0]["skill_loads"][0]["first"]
    assert not groups[2]["mandatory"] and not groups[2]["skill_loads"][0]["first"]
    assert not groups[3]["mandatory"] and not groups[3]["skill_loads"]
    assert any(o.state=="ACCEPTED" for o in image.context_operations)
    compacted=[s for s in image.context_snapshots if s.payload["kind"]=="COMPACTED"]
    assert compacted
    assert any(m.get("tool_call_id")=="first" for m in model.requests[-1])
    skill=tools[0].catalog.get("ci-failure-investigation")
    returned=[json.loads(json.loads(m["content"])["content"]) for m in model.requests[-1] if m.get("tool_call_id")=="first"]
    assert returned[0]["content"]==(skill.package_path/"SKILL.md").read_bytes().decode("utf-8")
    save_result("compaction",dict(run_id=scope.run_id,frame_id=frame.frame_id,evidence_level="CONTROLLED_MODEL_REAL_RUNTIME",groups=[{k:v for k,v in g.items() if k!="messages"} for g in groups],
        snapshots=[dict(id=s.snapshot_id,digest=s.snapshot_digest,payload=s.payload) for s in compacted],final_request=model.requests[-1]))


@pytest.mark.asyncio
async def test_recovery_reuses_committed_body_after_file_removal(clean_database,tmp_path):
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.runtime.run_control import DurableRunControlService
    class Boundary(DurableExecutionRepository):
        suspended=False
        async def commit_loop_batch(self,*args,**kwargs):
            result=await super().commit_loop_batch(*args,**kwargs)
            if not self.suspended:
                self.suspended=True
                raise LoopSuspended("TEST_COMMITTED_BOUNDARY")
            return result
    shutil.copytree(ROOT/"skills",tmp_path/"skills")
    catalog=SkillCatalog.discover([tmp_path/"skills"])
    model=Provider([call("load_skill",{"name":"ci-failure-investigation"},"first"),ModelAdapterResponse("恢复完成",finish_reason="stop",tool_call_count=0)])
    control=DurableRunControlService(clean_database,lease_seconds=10)
    repo=Boundary(clean_database,control)
    factory,repo,tools=await build(clean_database,tmp_path,model,catalog=catalog,repository=repo)
    # 使用同一原控制服务，保证 Repository 与 Factory 的 lease Owner 一致。
    factory._services=replace(factory.services,durable_run_control=control)
    scope=await factory.create_run_scope("skill_context_reader","请 load_skill 后回答",persist=False)
    try:
        with pytest.raises(LoopSuspended): await scope.execute()
    finally: await scope.close(abort=True)
    before=await repo.load(scope.run_id)
    assert before.root.status=="ACTIVE" and len(before.tool_invocations)==1
    original=before.tool_invocations[0].committed_result
    (catalog.get("ci-failure-investigation").package_path/"SKILL.md").unlink()
    reference=catalog.get("ci-failure-investigation").package_path/"references/failure-evidence-guide.md"
    reference.write_text("changed",encoding="utf-8")
    factory._router.skill_catalog=SkillCatalog.discover([])
    lease=await control.claim(scope.run_id,"skill-recovery")
    recovered=await repo.prepare_recovery(lease)
    resumed=await factory.create_rehydrated_run_scope(recovered,lease=lease,persist=False)
    try: result=await resumed.execute()
    finally: await resumed.close(abort=True)
    after=await repo.load(scope.run_id)
    assert result.status.value=="SUCCEEDED",(result.error_code,result.safe_message)
    assert len(after.tool_invocations)==1 and after.tool_invocations[0].committed_result==original
    assert any(m.get("tool_call_id")=="first" for m in model.requests[-1])
    assert context_sources_v2(after.loop_frames[0],after.loop_iterations)[0]["mandatory"]
    from types import SimpleNamespace as NS
    from core.runtime.tool_adapters import ToolAdapterInvocationError
    ctx=NS(raise_if_cancelled=lambda:None,run_context=NS(loop_skill_identity=after.loop_frames[0].pinned_config["skills"],loop_frame_id=after.loop_frames[0].frame_id,loop_skill_agent_id="skill_context_reader"))
    with pytest.raises(ToolAdapterInvocationError):
        tools[1].invoke_once(tools[1].build_invocation(json.dumps({"name":"ci-failure-investigation","relative_path":"references/failure-evidence-guide.md"})),ctx)
    save_result("recovery",dict(run_id=scope.run_id,frame_id=after.loop_frames[0].frame_id,evidence_level="DETERMINISTIC_PERSISTED_RECOVERY",committed_invocation_id=after.tool_invocations[0].invocation_id,
        committed_result=original,final_request=model.requests[-1],snapshot_digests=[s.snapshot_digest for s in after.context_snapshots]))
