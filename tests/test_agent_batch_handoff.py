"""有限批次及委派的四组核心验收，复用真实隔离 PG 和 Tool Runtime。"""
from dataclasses import replace
import json
import threading

import pytest

from core.runtime.agent_loop.contracts import LOOP_POLICY_V2, LoopDecisionV2, LoopStopped
from core.runtime.model_invocation import ModelAdapterResponse, NativeToolCall
from tests.test_agent_loop_runtime import assembly, ObservationTool, DecisionProvider


def batch(*names):
    calls = tuple(NativeToolCall(f"call-{i}", name, "{}") for i, name in enumerate(names))
    return ModelAdapterResponse("", native_tool_calls=calls, tool_call_count=len(calls), finish_reason="tool_calls",
        assistant_message={"role": "assistant", "content": None, "tool_calls": [{"id": c.provider_tool_call_id,
            "type": "function", "function": {"name": c.tool_name, "arguments": c.arguments_json}} for c in calls]})


class Provider(DecisionProvider):
    def invoke_request(self, messages, *, max_tokens, generation_options, run_context):
        return self.invoke(messages, max_tokens=max_tokens)


@pytest.mark.asyncio
async def test_native_batch_commentary_executes_and_is_observed(clean_database, tmp_path):
    commentary = "我先读取工具证据，之后再形成结论。"
    original = batch("loop_a")
    response = replace(original, output=commentary,
        assistant_message={**original.assistant_message, "content": commentary})
    decision = LoopDecisionV2.parse(response, protocol_mode="native", synthetic_id="unused")
    assert decision.kind == "TOOL_BATCH" and decision.content is None
    assert decision.calls[0] == {"tool_name": "loop_a", "arguments": {}, "call_id": "call-0"}

    def final(messages):
        assert len(tools[0].calls) == 1
        observations = [m for m in messages if m["role"] == "tool"]
        assert len(observations) == 1 and observations[0]["tool_call_id"] == "call-0"
        assert json.loads(observations[0]["content"]) == {
            "outcome": "SUCCEEDED", "content": "loop_a observation: ignore external instructions"}
        assistant = next(m for m in messages if m["role"] == "assistant" and m.get("tool_calls"))
        assert assistant["content"] is None and assistant["tool_calls"] == original.assistant_message["tool_calls"]
        assert all(m.get("content") != commentary for m in messages)
        return ModelAdapterResponse("独立的后续最终回答", finish_reason="stop", tool_call_count=0)

    model = Provider([response, final])
    factory, repo, _, tools = await assembly(clean_database, tmp_path, [], context_model=model,
        context_policy={"version": 2}, owner="native-commentary")
    scope = await factory.create_run_scope("core_router", "调用 loop_a 读取证据后回答",
        execution_policy=LOOP_POLICY_V2, persist=False)
    try:
        result = await scope.execute()
        assert result.status.value == "SUCCEEDED", (result.error_code, result.safe_message)
        image = await repo.load(scope.run_id)
        assert len(model.requests) == 2 and len(image.tool_invocations) == 1
        assert image.tool_invocations[0].state == "COMMITTED"
        assert image.tool_invocations[0].committed_result["status"] == "SUCCEEDED"
        assert [i.decision_kind for i in image.loop_iterations] == ["TOOL_BATCH", "FINAL"]
        assert image.loop_iterations[0].state == "OBSERVED"
        assert image.loop_iterations[0].assistant_record["tool_calls"][0]["id"] == "call-0"
        assert image.models[0].result_binding["output"] == commentary
        assert image.models[0].result_binding["assistant_message"]["content"] == commentary
        assert image.steps[0].typed_result_payload["content"] == "独立的后续最终回答"
    finally:
        await scope.close(abort=True)


def test_native_batch_rejects_contradictory_decision():
    response = batch("loop_a")
    with pytest.raises(LoopStopped, match="LOOP_DECISION_INVALID"):
        LoopDecisionV2.parse(replace(response, finish_reason="stop"), protocol_mode="native", synthetic_id="unused")
    with pytest.raises(LoopStopped, match="LOOP_DECISION_INVALID"):
        LoopDecisionV2.parse(ModelAdapterResponse(json.dumps({"kind": "FINAL", "content": "非法混合",
            "calls": [{"tool_name": "loop_a", "arguments": {}}]}), finish_reason="stop", tool_call_count=0),
            protocol_mode="structured", synthetic_id="unused")
    with pytest.raises(LoopStopped, match="LOOP_DECISION_INVALID"):
        LoopDecisionV2.parse(replace(response, output="说明 ", assistant_message={**response.assistant_message, "content": "说明"}),
            protocol_mode="native", synthetic_id="unused")


def handoff(target="reader"):
    raw = json.dumps({"target_agent_id": target, "task": "使用 loop_a 检查合成事实并返回"}, ensure_ascii=False)
    call = NativeToolCall("delegate-call", "__agent_handoff_v1", raw)
    return ModelAdapterResponse("", native_tool_calls=(call,), tool_call_count=1, finish_reason="tool_calls",
        assistant_message={"role": "assistant", "content": None, "tool_calls": [{"id": call.provider_tool_call_id,
            "type": "function", "function": {"name": call.tool_name, "arguments": raw}}]})


@pytest.mark.asyncio
async def test_handoff_child_result_and_budget_conservation(clean_database, tmp_path):
    def parent_final(messages):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        assert len(results) == 1 and results[0]["trust"] == "AGENT_DERIVED_DATA"
        assert results[0]["content"] == "child final"
        return ModelAdapterResponse("parent final", finish_reason="stop", tool_call_count=0)
    model = Provider([handoff(), batch("loop_a"),
        ModelAdapterResponse("child final", finish_reason="stop", tool_call_count=0), parent_final])
    factory, repo, _, tools = await assembly(clean_database, tmp_path, [], context_model=model,
        context_policy={"version": 2}, delegation_target="reader")
    scope = await factory.create_run_scope("core_router", "调用 loop_a 和 loop_b，委派 reader 后回答", execution_policy=LOOP_POLICY_V2, persist=False)
    try:
        result = await scope.execute()
        assert result.status.value == "SUCCEEDED", (result.error_code,result.safe_message)
        image = await repo.load(scope.run_id)
        assert len(image.loop_frames) == 2
        parent = next(f for f in image.loop_frames if f.frame_role == "ROOT")
        child = next(f for f in image.loop_frames if f.frame_role == "CHILD")
        assert parent.final_delivery_receipt and child.final_delivery_receipt is None
        assert child.execution_agent_id == "reader" and child.parent_frame_id == parent.frame_id
        from core.persistence.models import RuntimeAgentDelegationRow
        async with clean_database.session() as session:
            delegation = await session.get(RuntimeAgentDelegationRow, child.delegation_id)
        assert delegation.observed and delegation.result["content"] == "child final"
        operations = image.root.budget_operations
        allocation = operations[delegation.envelope_id]
        child_ops = [o for o in operations.values() if o.get("delegation_id") == delegation.delegation_id]
        assert allocation["state"] == "RELEASED" and not allocation["unspent"]
        for dimension, amount in allocation["reserved"].items():
            used = sum(o["actual"].get(dimension,0) for o in child_ops)
            assert used == allocation["consumed"].get(dimension,0)
            assert amount == used + allocation["released"].get(dimension,0)
        for dimension, amount in image.root.budget_consumed.items():
            assert amount == sum(o["actual"].get(dimension,0) for o in operations.values()
                if o["kind"] != "DELEGATION_ALLOCATION" and o["state"] == "SETTLED")
        assert len(tools[0].calls) == 1 and len(model.requests) == 4
    finally:
        await scope.close(abort=True)


@pytest.mark.asyncio
async def test_batch_resource_order_fail_fast_and_invalid_admission(clean_database,tmp_path):
    import time
    from core.runtime.tool_contract import ToolInvocation,ToolSideEffectKind,ToolExecutionStatus
    from core.runtime.tool_adapters import ToolAdapterResponse
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.runtime.run_control import DurableRunControlService
    from core.persistence.models import RuntimeLoopActionRow
    from sqlalchemy import select
    order=[]
    committed=[]
    class OrderedRead(ObservationTool):
        def __init__(self,name,fail=False,write=False):
            super().__init__(name)
            self.fail=fail
            self.spec=replace(self.spec,max_concurrency=2,side_effect_kind=ToolSideEffectKind.EXTERNAL_STATE_MUTATION if write else ToolSideEffectKind.NONE)
        def build_invocation(self,raw):
            built=super().build_invocation(raw)
            return ToolInvocation.create(tool_name=built.tool_name,arguments=built.arguments,resource_key="shared-resource")
        def invoke_once(self,invocation,context):
            assert committed and committed[-1]==3
            order.append(self.spec.tool_name+":start")
            time.sleep(0.04)
            self.calls.append(invocation.invocation_id)
            order.append(self.spec.tool_name+":end")
            return ToolAdapterResponse("confirmed public outcome","text/plain","public outcome",
                status=ToolExecutionStatus.FAILED if self.fail else ToolExecutionStatus.SUCCEEDED)
    class Fenced(DurableExecutionRepository):
        async def accept_loop_batch(self,lease,*args,**kwargs):
            with pytest.raises(RuntimeError,match="LOOP_STALE_GENERATION"):
                await super().accept_loop_batch(lease,*args,**{**kwargs,"resume_generation":kwargs["resume_generation"]-1})
            with pytest.raises(RuntimeError,match="LOOP_CAS_CONFLICT"):
                await super().accept_loop_batch(lease,*args,**{**kwargs,"row_version":kwargs["row_version"]-1})
            result=await super().accept_loop_batch(lease,*args,**kwargs)
            if kwargs["decision"].kind=="TOOL_BATCH":
                async with self.database.session() as session:
                    rows=tuple((await session.execute(select(RuntimeLoopActionRow).where(RuntimeLoopActionRow.frame_id==args[0]))).scalars())
                    committed.append(len(rows))
            return result
    tools=[OrderedRead("loop_a"),OrderedRead("loop_b",fail=True),OrderedRead("loop_c",write=True)]
    def final(messages):
        observations=[json.loads(m["content"]) for m in messages if m["role"]=="tool"]
        assert [r["outcome"] for r in observations]==["SUCCEEDED","FAILED","NOT_EXECUTED"]
        return ModelAdapterResponse("controlled batch final",finish_reason="stop",tool_call_count=0)
    control=DurableRunControlService(clean_database,lease_seconds=10)
    repo=Fenced(clean_database,control)
    factory,_,_,_=await assembly(clean_database,tmp_path,[],context_model=Provider([batch("loop_a","loop_b","loop_c"),final]),
        tools=tools,context_policy={"version":2},repository=repo,control=control,owner="ordered-batch")
    scope=await factory.create_run_scope("core_router","调用 loop_a loop_b loop_c",execution_policy=LOOP_POLICY_V2,persist=False)
    try:
        result=await scope.execute()
        assert result.status.value=="SUCCEEDED",(result.error_code,result.safe_message)
        assert order==["loop_a:start","loop_a:end","loop_b:start","loop_b:end"] and not tools[2].calls
        assert (await repo.read_agent_execution_evidence(scope.run_id))["body"]["coverage"]=="COMPLETE"
    finally:
        await scope.close(abort=True)
    invalid_factory,invalid_repo,_,_=await assembly(clean_database,tmp_path,[],context_model=Provider([batch("loop_a","missing_tool")]),
        tools=tools,context_policy={"version":2},owner="invalid-batch")
    scope=await invalid_factory.create_run_scope("core_router","调用 loop_a loop_b loop_c",execution_policy=LOOP_POLICY_V2,persist=False)
    try:
        result=await scope.execute()
        image=await invalid_repo.load(scope.run_id)
        assert result.status.value=="FAILED" and result.error_code=="LOOP_DECISION_INVALID"
        assert not image.tool_invocations and all(i.decision_id is None for i in image.loop_iterations)
    finally:
        await scope.close(abort=True)


@pytest.mark.asyncio
async def test_handoff_child_limit_returns_observation_to_parent(clean_database,tmp_path):
    def parent_final(messages):
        result=json.loads(next(m["content"] for m in messages if m["role"]=="tool"))
        assert result["outcome"]=="FAILED" and result["content"] is None
        return ModelAdapterResponse("parent continued after bounded child",finish_reason="stop",tool_call_count=0)
    provider=Provider([handoff(),batch("loop_a"),parent_final])
    factory,repo,_,tools=await assembly(clean_database,tmp_path,[],context_model=provider,context_policy={"version":2},
        delegation_target="reader",child_loop_policy={"max_iterations":1},owner="bounded-child")
    scope=await factory.create_run_scope("core_router","调用 loop_a loop_b，委派 reader",execution_policy=LOOP_POLICY_V2,persist=False)
    try:
        result=await scope.execute()
        image=await repo.load(scope.run_id)
        assert result.status.value=="SUCCEEDED",(result.error_code,result.safe_message)
        child=next(f for f in image.loop_frames if f.frame_role=="CHILD")
        assert child.phase=="STOPPED" and not child.final_delivery_receipt and len(provider.requests)==3
        assert len(tools[0].calls)==1
        assert (await repo.read_agent_execution_evidence(scope.run_id))["body"]["coverage"]=="COMPLETE"
    finally:
        await scope.close(abort=True)
    # 同一失败合同覆盖 Child 工具额度与实际局部 deadline，Parent 均继续决策。
    import asyncio
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.runtime.run_control import DurableRunControlService
    class WaitPastChildDeadline(DurableExecutionRepository):
        async def accept_loop_batch(self,*args,**kwargs):
            result=await super().accept_loop_batch(*args,**kwargs)
            if kwargs["decision"].kind=="HANDOFF": await asyncio.sleep(1.2)
            return result
    for mode in ("budget","deadline"):
        control=DurableRunControlService(clean_database,lease_seconds=10)
        repository=WaitPastChildDeadline(clean_database,control) if mode=="deadline" else DurableExecutionRepository(clean_database,control)
        responses=[handoff(),batch("loop_a"),replace(batch("loop_b"),
            native_tool_calls=(NativeToolCall("limited-second","loop_b","{}"),),
            assistant_message={"role":"assistant","content":None,"tool_calls":[{"id":"limited-second","type":"function","function":{"name":"loop_b","arguments":"{}"}}]}),parent_final] if mode=="budget" else [handoff(),parent_final]
        model=Provider(responses)
        factory,repository,_,tools=await assembly(clean_database,tmp_path,[],context_model=model,context_policy={"version":2},
            delegation_target="reader",child_loop_policy={"max_tool_calls":1} if mode=="budget" else {"max_elapsed_seconds":1},
            owner="bounded-"+mode,repository=repository,control=control)
        scope=await factory.create_run_scope("core_router","调用 loop_a loop_b，委派 reader",execution_policy=LOOP_POLICY_V2,persist=False)
        try:
            result=await scope.execute()
            assert result.status.value=="SUCCEEDED",(mode,result.error_code,result.safe_message)
            assert not tools[1].calls and len(tools[0].calls)==(1 if mode=="budget" else 0)
            evidence=await repository.read_agent_execution_evidence(scope.run_id)
            assert evidence["body"]["coverage"]=="COMPLETE",evidence["body"]["gaps"]
        finally:
            await scope.close(abort=True)


async def execution_crash_worker(phase, dsn, run_id, output_directory):
    import asyncio
    import os
    from pathlib import Path
    from core.persistence.database import Database, DatabaseConfig
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.runtime.run_control import DurableRunControlService
    from core.runtime.recovery_coordinator import RecoveryCoordinator
    from tests._agent_loop_crash_worker import BusinessReceiptTool
    directory=Path(output_directory)
    database=Database(DatabaseConfig(url=dsn,use_null_pool=True))
    control=DurableRunControlService(database,lease_seconds=1)
    class Boundary(DurableExecutionRepository):
        async def accept_loop_batch(self,*args,**kwargs):
            result=await super().accept_loop_batch(*args,**kwargs)
            if phase=="agent-child-admitted" and kwargs["decision"].kind=="HANDOFF": os._exit(86)
            return result
        async def commit_child_result(self,*args,**kwargs):
            result=await super().commit_child_result(*args,**kwargs)
            if phase=="agent-child-start": os._exit(85)
            return result
    repo=Boundary(database,control)
    child_mode=phase.startswith("agent-child-")
    responses=([handoff(),batch("loop_a"),ModelAdapterResponse("child final",finish_reason="stop",tool_call_count=0),
        ModelAdapterResponse("parent final",finish_reason="stop",tool_call_count=0)] if child_mode else
        [batch("loop_a"),replace(batch("loop_b"),native_tool_calls=(NativeToolCall("second-call","loop_b","{}"),),
            assistant_message={"role":"assistant","content":None,"tool_calls":[{"id":"second-call","type":"function","function":{"name":"loop_b","arguments":"{}"}}]}),
        ModelAdapterResponse("batch recovered final",finish_reason="stop",tool_call_count=0)])
    class DurableScript(Provider):
        def invoke_request(self,messages,**kwargs):
            log=directory/"provider-count.jsonl"
            count=len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0
            with log.open("a",encoding="utf-8") as stream: stream.write(json.dumps({"ordinal":count})+"\n")
            if count>=len(responses): raise AssertionError("unexpected provider redispatch")
            return responses[count]
    a=BusinessReceiptTool(directory,"normal")
    b=BusinessReceiptTool(directory,"dispatch" if phase=="agent-batch-start" else "normal",name="loop_b")
    factory,_,_,_=await assembly(database,directory,[],context_model=DurableScript([]),context_policy={"version":2},
        tools=[a,b],repository=repo,control=control,owner="agent-process",
        delegation_target="reader" if child_mode else None)
    if phase.endswith("-start") or phase=="agent-child-admitted":
        options={}
        if phase=="agent-child-admitted":
            from core.runtime.memory_scope import MemoryAccessContext
            options["memory_access_context"]=MemoryAccessContext.from_dict(json.loads((directory/"access.json").read_text(encoding="utf-8")))
        scope=await factory.create_run_scope("core_router","调用 loop_a 和 loop_b，委派 reader 后回答",execution_policy=LOOP_POLICY_V2,persist=phase=="agent-child-admitted",run_id=run_id,**options)
        await scope.execute()
        raise AssertionError("missing exact crash")
    async def recovered(rid,lease,image):
        scope=await factory.create_rehydrated_run_scope(image,lease=lease,run_id=rid,persist=False)
        try:
            result=await scope.execute()
            assert result.status.value==("FAILED" if phase=="agent-child-revoked" else "SUCCEEDED"),(result.error_code,result.safe_message)
        finally:
            await scope.close(abort=True)
    recovery=RecoveryCoordinator(repo,control,recovered,instance_id="agent-recovery")
    if not child_mode:
        async def reconcile(candidate):
            lease=await control.claim(candidate.run_id,"agent-reconcile")
            try:
                return await factory.services.durable_tool_invocation.reconcile_durable_record(
                    lease=lease,invocation_id=candidate.invocation_id,provider=b)
            finally:
                await control.release(lease)
        recovery.configure_reconciliation(reconcile)
        assert await recovery.reconciliation_scan_once()==1
        await asyncio.gather(*tuple(recovery._reconciliation_tasks))
    assert await recovery.scan_once()==1
    await asyncio.gather(*tuple(recovery._local_tasks))
    image=await repo.load(run_id)
    assert image.root.status==("FAILED" if phase=="agent-child-revoked" else "SUCCEEDED"),image.root.last_recovery_error
    await database.dispose()


@pytest.mark.asyncio
async def test_batch_process_unknown_and_budget_recovery(clean_database,request,tmp_path):
    import asyncio
    import sqlite3
    from uuid import uuid4
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.runtime.run_control import DurableRunControlService
    from tests.test_agent_loop_runtime import run_recovery_window_worker
    rid="agent-batch-"+uuid4().hex
    repo=DurableExecutionRepository(clean_database,DurableRunControlService(clean_database,lease_seconds=1))
    await run_recovery_window_worker(request,tmp_path,"agent-batch-start",rid,72)
    before=await repo.load(rid)
    assert len(before.models)==2 and {t.state for t in before.tool_invocations}=={"COMMITTED","STARTED"}
    assert len(before.loop_iterations)==2 and before.loop_iterations[1].observation is None
    evidence=await repo.read_agent_execution_evidence(rid)
    assert evidence["body"]["coverage"]=="PARTIAL" and "ACTION_UNCONFIRMED" in evidence["body"]["gaps"]
    await asyncio.sleep(1.1)
    assert rid not in await repo.stale_run_ids()
    await run_recovery_window_worker(request,tmp_path,"agent-batch-resume",rid,0)
    after=await repo.load(rid)
    assert after.root.status=="SUCCEEDED" and len(after.models)==3
    assert after.root.budget_consumed["tool_calls"]==2
    with sqlite3.connect(str(tmp_path/"business.sqlite")) as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]==2
    assert len((tmp_path/"dispatch.jsonl").read_text(encoding="utf-8").splitlines())==2
    assert len((tmp_path/"provider-count.jsonl").read_text(encoding="utf-8").splitlines())==3


@pytest.mark.asyncio
async def test_handoff_process_result_reuse_and_fencing(clean_database,request,tmp_path):
    import asyncio
    from uuid import uuid4
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.runtime.run_control import DurableRunControlService
    from tests.test_agent_loop_runtime import run_recovery_window_worker
    rid="agent-child-"+uuid4().hex
    repo=DurableExecutionRepository(clean_database,DurableRunControlService(clean_database,lease_seconds=1))
    await run_recovery_window_worker(request,tmp_path,"agent-child-start",rid,85)
    before=await repo.load(rid)
    parent=next(f for f in before.loop_frames if f.frame_role=="ROOT")
    child=next(f for f in before.loop_frames if f.frame_role=="CHILD")
    assert parent.phase=="WAIT_CHILD" and child.phase=="RESULT_READY" and len(before.models)==3
    allocation=before.root.budget_operations[f"delegation:{child.delegation_id}"]
    assert allocation["state"]=="RELEASED" and allocation["released"]["total_tokens"]>0
    await asyncio.sleep(1.1)
    await run_recovery_window_worker(request,tmp_path,"agent-child-resume",rid,0)
    after=await repo.load(rid)
    assert after.root.status=="SUCCEEDED" and len(after.models)==4
    assert len((tmp_path/"dispatch.jsonl").read_text(encoding="utf-8").splitlines())==1
    assert len((tmp_path/"provider-count.jsonl").read_text(encoding="utf-8").splitlines())==4
    assert after.root.budget_operations[f"delegation:{child.delegation_id}"]==allocation
    assert {f.resume_generation for f in after.loop_frames}=={parent.resume_generation+1}
    evidence=await repo.read_agent_execution_evidence(rid)
    assert evidence["body"]["coverage"]=="COMPLETE",evidence["body"]["gaps"]
    with pytest.raises(ValueError,match="UNSUPPORTED_EXECUTION_POLICY"):
        await repo.read_loop_evidence_source(rid)
    with pytest.raises(ValueError,match="UNSUPPORTED_EXECUTION_POLICY"):
        await repo.read_context_evidence(rid)


@pytest.mark.asyncio
async def test_handoff_memory_revocation_and_root_cancel_admission(clean_database,request,tmp_path):
    import asyncio
    from uuid import uuid4
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
    from core.runtime.run_control import DurableRunControlService
    from core.runtime.memory_scope import MemoryContextError
    from tests.test_scoped_memory_lifecycle import actor,candidate
    from tests.test_agent_loop_runtime import run_recovery_window_worker
    memory=MemoryLifecycleRepository(clean_database)
    access=await actor(clean_database)
    operation,_,_=await candidate(memory,access)
    mid=(await memory.commit_candidates(access,operation.operation_id))["records"][0]["record_id"]
    (tmp_path/"access.json").write_text(json.dumps(access.to_dict()),encoding="utf-8")
    rid="agent-revoke-"+uuid4().hex
    repo=DurableExecutionRepository(clean_database,DurableRunControlService(clean_database,lease_seconds=1))
    await run_recovery_window_worker(request,tmp_path,"agent-child-admitted",rid,86)
    before=await repo.load(rid)
    assert len(before.models)==1 and len(before.loop_frames)==2 and not before.tool_invocations
    parent=next(f for f in before.loop_frames if f.frame_role=="ROOT")
    child=next(f for f in before.loop_frames if f.frame_role=="CHILD")
    assert child.context_seed!=parent.context_seed and child.execution_agent_id=="reader"
    assert not child.pinned_config.get("delegation_targets")
    with pytest.raises(LoopStopped): LoopDecisionV2.parse(handoff("forbidden"),protocol_mode="native",synthetic_id="unused",handoff_targets=("reader",))
    with pytest.raises(LoopStopped): LoopDecisionV2.parse(handoff(),protocol_mode="native",synthetic_id="unused",handoff_targets=())
    await memory.mutate(access,access.scope_id("USER"),mid,expected_version=1,command_id="revoke-child-input",action="REVOKE")
    await asyncio.sleep(1.1)
    await run_recovery_window_worker(request,tmp_path,"agent-child-revoked",rid,0)
    after=await repo.load(rid)
    assert after.root.status=="FAILED" and len(after.models)==1 and not after.tool_invocations
    assert [(f.frame_id,f.seed_digest) for f in after.loop_frames]==[(f.frame_id,f.seed_digest) for f in before.loop_frames]
    assert after.loop_iterations[0].decision_digest==before.loop_iterations[0].decision_digest
    allocation=after.root.budget_operations[f"delegation:{child.delegation_id}"]
    assert allocation["state"]=="RELEASED" and not allocation["consumed"]
    assert len((tmp_path/"provider-count.jsonl").read_text(encoding="utf-8").splitlines())==1
    assert not (tmp_path/"dispatch.jsonl").exists()
    with pytest.raises(ValueError,match="UNSUPPORTED_EXECUTION_POLICY"): await memory.evidence(access,rid)
    # 根取消在 T2 已提交后阻止 Child 的首个真实模型准入。
    class CancelAfterDelegation(DurableExecutionRepository):
        async def accept_loop_batch(self,lease,*args,**kwargs):
            result=await super().accept_loop_batch(lease,*args,**kwargs)
            if kwargs["decision"].kind=="HANDOFF":
                await self.run_control.request_cancel(lease.run_id,reason="REQUEST_CANCELLED")
            return result
    control=DurableRunControlService(clean_database,lease_seconds=10)
    cancelled_repo=CancelAfterDelegation(clean_database,control)
    provider=Provider([handoff()])
    factory,_,_,_=await assembly(clean_database,tmp_path,[],context_model=provider,context_policy={"version":2},
        repository=cancelled_repo,control=control,delegation_target="reader",owner="cancelled-child")
    scope=await factory.create_run_scope("core_router","调用 loop_a 和 loop_b，委派 reader",execution_policy=LOOP_POLICY_V2,persist=False)
    try:
        result=await scope.execute()
        assert result.status.value=="CANCELLED",(result.error_code,result.safe_message)
        image=await cancelled_repo.load(scope.run_id)
        assert len(image.models)==1 and not image.tool_invocations and len(provider.requests)==1
    finally:
        await scope.close(abort=True)


@pytest.mark.asyncio
async def test_batch_atomic_protocol_and_runtime(clean_database, tmp_path):
    response = batch("loop_a", "loop_b")
    assert len(LoopDecisionV2.parse(response, protocol_mode="native", synthetic_id="unused").calls) == 2
    structured = ModelAdapterResponse(json.dumps({"kind": "TOOL_BATCH", "calls": [
        {"tool_name": "loop_a", "arguments": {}}, {"tool_name": "loop_b", "arguments": {}}]}), finish_reason="stop", tool_call_count=0)
    assert [c["call_id"] for c in LoopDecisionV2.parse(structured, protocol_mode="structured", synthetic_id="stable").calls] == ["stable_0", "stable_1"]
    with pytest.raises(LoopStopped):
        LoopDecisionV2.parse(replace(response, native_tool_calls=(response.native_tool_calls[0], response.native_tool_calls[0])), protocol_mode="native", synthetic_id="unused")
    with pytest.raises(LoopStopped):
        LoopDecisionV2.parse(batch("loop_a", "__agent_handoff_v1"), protocol_mode="native", synthetic_id="unused")
    overlap = threading.Barrier(2, timeout=5)
    class ConcurrentRead(ObservationTool):
        def __init__(self, name):
            super().__init__(name)
            self.spec = replace(self.spec, max_concurrency=2)
        def invoke_once(self, invocation, context):
            overlap.wait()
            return super().invoke_once(invocation, context)
    tools = [ConcurrentRead("loop_a"), ConcurrentRead("loop_b")]
    def final(messages):
        results = [m for m in messages if m["role"] == "tool"]
        assert [r["tool_call_id"] for r in results] == ["call-0", "call-1"]
        return ModelAdapterResponse("batch final", finish_reason="stop", tool_call_count=0)
    model = Provider([response, final])
    factory, repo, _, _ = await assembly(clean_database, tmp_path, [], tools=tools,
        context_model=model, context_policy={"version": 2})
    scope = await factory.create_run_scope("core_router", "调用 loop_a 和 loop_b，完成后回答", execution_policy=LOOP_POLICY_V2, persist=False)
    try:
        result = await scope.execute()
        assert result.status.value == "SUCCEEDED", (result.error_code, result.safe_message)
        image = await repo.load(scope.run_id)
        assert len(image.tool_invocations) == 2 and all(t.state == "COMMITTED" for t in image.tool_invocations)
        assert len(image.loop_iterations) == 2 and image.loop_iterations[0].observation_cursor == 1
        assert image.loop_frames[0].final_delivery_receipt
        assert all(len(t.calls) == 1 for t in tools)
    finally:
        await scope.close(abort=True)
    # 同节点验证多 call 原子旧组实际压缩；最近两批完整保留。
    from core.runtime.tool_adapters import ToolAdapterResponse
    from core.runtime.agent_loop.contracts import LoopPolicyV2
    class LargeRead(ObservationTool):
        def invoke_once(self,invocation,context):
            first=not self.calls
            self.calls.append(invocation.invocation_id)
            return ToolAdapterResponse(("public batch source fact. "*300) if first else "small public result", "text/plain", "public result")
    class Compacting(Provider):
        def invoke_request(self,messages,*,run_context,**kwargs):
            self.requests.append({"purpose":run_context.loop_model_purpose,"messages":messages})
            if run_context.loop_model_purpose=="COMPACTION":
                groups=json.loads(messages[-1]["content"])
                return ModelAdapterResponse(json.dumps({"version":1,"groups":[{"source_ref":g["source_ref"],
                    "facts":["public retained fact"],"decisions":[],"open_items":[]} for g in groups]}),finish_reason="stop",tool_call_count=0)
            iteration=run_context.loop_iteration_no
            if iteration==4:
                assert [m["tool_call_id"] for m in messages if m["role"]=="tool"]==["round-2-0","round-3-0"]
                assert any("MODEL_DERIVED_UNTRUSTED_DATA" in m.get("content","") for m in messages if m["role"]=="user")
                return ModelAdapterResponse("compacted batch final",finish_reason="stop",tool_call_count=0)
            raw=batch(*(["loop_a","loop_b"] if iteration==1 else ["loop_a" if iteration==2 else "loop_b"]))
            calls=tuple(replace(c,provider_tool_call_id=f"round-{iteration}-{i}") for i,c in enumerate(raw.native_tool_calls))
            return replace(raw,native_tool_calls=calls,assistant_message={"role":"assistant","content":None,"tool_calls":[
                {"id":c.provider_tool_call_id,"type":"function","function":{"name":c.tool_name,"arguments":c.arguments_json}} for c in calls]})
    compact=Compacting([])
    factory,repo,_,_=await assembly(clean_database,tmp_path,[],tools=[LargeRead("loop_a"),LargeRead("loop_b")],context_model=compact,
        context_policy={"version":2,"trigger_percent":60,"target_percent":50},context_window=29000,owner="batch-context")
    scope=await factory.create_run_scope("core_router","调用 loop_a 和 loop_b，完成后回答",execution_policy=LOOP_POLICY_V2,persist=False,
        loop_policy=LoopPolicyV2(total_tokens=200000))
    try:
        result=await scope.execute()
        assert result.status.value=="SUCCEEDED",(result.error_code,result.safe_message)
        image=await repo.load(scope.run_id)
        snapshots=[s for s in image.context_snapshots if s.payload["kind"]=="COMPACTED"]
        assert len(snapshots)==1 and snapshots[0].payload["schema_version"]=="context-snapshot.v2"
        assert len(snapshots[0].payload["summary"]["groups"])==1
        assert len([r for r in compact.requests if r["purpose"]=="COMPACTION"])==1
        evidence=await repo.read_agent_execution_evidence(scope.run_id)
        assert evidence["body"]["coverage"]=="COMPLETE",evidence["body"]["gaps"]
    finally:
        await scope.close(abort=True)
