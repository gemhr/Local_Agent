"""四组聚焦验收；真实隔离 PG、并发事务和单个进程恢复 seam。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from core.persistence.models import UserRow, TenantRow, LongTermMemoryRow, MemoryOperationRow, MemoryScopeRow
from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
from core.runtime.memory_scope import MemoryAccessContext, MemoryContextError
from core.runtime.run_control import DurableRunControlService

TENANT = "00000000-0000-0000-0000-000000000001"

async def actor(db, tenant=TENANT, session="default", agent="core_router"):
    uid=uuid4()
    async with db.transaction() as s:
        if await s.get(TenantRow, tenant) is None:
            s.add(TenantRow(tenant_id=tenant)); await s.flush()
        s.add(UserRow(id=uid, subject=str(uid), tenant_id=tenant, principal_kind="HUMAN"))
    return MemoryAccessContext(tenant, str(uid), agent, session)

async def source(repo, access, *, manifest=(), text="用户长期偏好：回答使用中文"):
    scopes=await repo.private_scopes(access)
    control=DurableRunControlService(repo.database, lease_seconds=60)
    lease=await control.claim(str(uuid4()), "scoped-test")
    await repo.bind_run(lease, access, scopes, list(manifest))
    receipt=await repo.append_exchange(access, run_id=lease.run_id, user_text=text, final_text="合成回答")
    return lease, receipt

async def candidate(repo, access, *, key="response.language", expected=None, manifest=(), text="用户偏好使用中文回答", epistemic="USER_ASSERTION"):
    lease,receipt=await source(repo,access,manifest=manifest,text="用户明确更正偏好：今后统一使用中文" if expected is not None else "用户长期偏好：回答使用中文")
    op,_=await repo.prepare_formation(access,access.scope_id("USER"),receipt["exchange_id"],run_id=lease.run_id)
    body={"text":text,"logical_key":key,"expected_version":expected,"payload":{"value":"中文"},
          "epistemic_kind":epistemic,"run_id":lease.run_id,"exchange_id":receipt["exchange_id"]}
    await repo.save_candidates(access,op.operation_id,[body])
    return op,lease,receipt

@pytest.mark.asyncio
async def test_scope_identity_isolation_and_project_grants(clean_database):
    from core.auth import Principal
    from core.runtime.memory_authorization import MemoryAccessAuthorizer, MemoryAccessPrincipal
    from core.persistence.memory import PostgresMemoryManager
    repo=MemoryLifecycleRepository(clean_database)
    a=await actor(clean_database)
    other=await actor(clean_database)
    foreign=await actor(clean_database,tenant="synthetic-tenant")
    op,_,_=await candidate(repo,a)
    outcome=await repo.commit_candidates(a,op.operation_id)
    assert outcome["outcome"]=="COMMITTED"
    rows=await repo.retrieve(a,[a.scope_id("USER")]); assert len(rows)==1
    own_session=replace(a,session_id="another-session")
    assert len(await repo.retrieve(own_session,[a.scope_id("USER")]))==1
    for denied in [other,foreign,replace(a,agent_id="another-agent")]:
        with pytest.raises(MemoryContextError):
            await repo.retrieve(denied,[a.scope_id("USER")])
    await repo.private_scopes(own_session)
    assert not await repo.history(own_session,own_session.scope_id("SESSION"))
    with pytest.raises(MemoryContextError): await repo.private_scopes(None)
    with pytest.raises(MemoryContextError): MemoryAccessContext.from_principal(object(),a.agent_id)
    assert not MemoryAccessAuthorizer().authorize_private_read(MemoryAccessPrincipal(a.agent_id),a.agent_id,"direct").allowed
    with pytest.raises(MemoryContextError): await PostgresMemoryManager(clean_database).get_all_messages()
    project=await repo.configure_project(tenant_id=a.tenant_id,project_id="synthetic-project",agent_id=a.agent_id,
        grants={a.user_id:["READ","WRITE","PROMOTE"]},ttl_seconds=3600)
    promoted=await repo.promote(a,rows[0]["memory_id"],project,expected_version=1,command_id="promote")
    assert promoted["outcome"]=="COMMITTED" and len(await repo.retrieve(a,[project]))==1
    with pytest.raises(MemoryContextError): await repo.retrieve(other,[project])
    # B 只有共享 Scope 的删除权；同源标识不能反向授权修改 A 的私人 Formation。
    async with clean_database.session() as s:
        private=await s.get(LongTermMemoryRow,rows[0]["memory_id"])
        formation=await s.get(MemoryOperationRow,op.operation_id)
        before_private=deepcopy({c.name:getattr(private,c.name) for c in private.__table__.columns})
        before_formation=deepcopy({c.name:getattr(formation,c.name) for c in formation.__table__.columns})
    await repo.configure_project(tenant_id=a.tenant_id,project_id="synthetic-project",agent_id=a.agent_id,
        grants={a.user_id:["READ","WRITE","PROMOTE"],other.user_id:["DELETE"]},ttl_seconds=3600)
    with pytest.raises(MemoryContextError): await repo.retrieve(other,[project])
    deleted=await repo.mutate(other,project,promoted["record_id"],expected_version=1,command_id="delete-shared",action="DELETE")
    assert deleted["outcome"]=="COMPLETE_FOR_DECLARED_SCOPE"
    assert not await repo.retrieve(a,[project])
    assert await repo.retrieve(a,[a.scope_id("USER")])==rows
    assert await repo.commit_candidates(a,op.operation_id)==outcome
    async with clean_database.session() as s:
        private=await s.get(LongTermMemoryRow,rows[0]["memory_id"])
        formation=await s.get(MemoryOperationRow,op.operation_id)
        assert {c.name:getattr(private,c.name) for c in private.__table__.columns}==before_private
        assert {c.name:getattr(formation,c.name) for c in formation.__table__.columns}==before_formation
    await repo.configure_project(tenant_id=a.tenant_id,project_id="synthetic-project",agent_id=a.agent_id,grants={},ttl_seconds=3600)
    with pytest.raises(MemoryContextError): await repo.retrieve(a,[project])

@pytest.mark.asyncio
async def test_formation_replay_and_postgres_concurrent_cas(clean_database):
    repo=MemoryLifecycleRepository(clean_database); a=await actor(clean_database)
    first,_,_=await candidate(repo,a)
    second,_,_=await candidate(repo,a)
    results=await asyncio.gather(repo.commit_candidates(a,first.operation_id),repo.commit_candidates(a,second.operation_id))
    assert sorted(r["outcome"] for r in results)==["COMMITTED","VERSION_CONFLICT"]
    winner=first if results[0]["outcome"]=="COMMITTED" else second
    committed=next(r for r in results if r["outcome"]=="COMMITTED")
    assert await repo.commit_candidates(a,winner.operation_id)==committed
    left,_,_=await candidate(repo,a,expected=1,text="用户明确更正为中文")
    right,_,_=await candidate(repo,a,expected=1,text="用户明确更正为英文")
    changes=await asyncio.gather(repo.commit_candidates(a,left.operation_id),repo.commit_candidates(a,right.operation_id))
    assert sorted(r["outcome"] for r in changes)==["COMMITTED","VERSION_CONFLICT"]
    rows=await repo.retrieve(a,[a.scope_id("USER")]); assert len(rows)==1 and rows[0]["version"]==2
    # 没有 durable extraction response 的 PREPARED 操作不再次计费。
    lease,receipt=await source(repo,a)
    prepared,created=await repo.prepare_formation(a,a.scope_id("USER"),receipt["exchange_id"],run_id=lease.run_id)
    recovered,created_again=await repo.prepare_formation(a,a.scope_id("USER"),receipt["exchange_id"],run_id=lease.run_id)
    assert created and not created_again and recovered.operation_id==prepared.operation_id and recovered.state=="UNKNOWN"
    # 未批准的 observation 不可伪装成确认事实；推断只能候选。
    with pytest.raises(MemoryContextError): await candidate(repo,a,key=None,epistemic="OBSERVATION")
    infer,_,_=await candidate(repo,a,key=None,epistemic="INFERENCE")
    await repo.commit_candidates(a,infer.operation_id)
    assert len(await repo.retrieve(a,[a.scope_id("USER")]))==1
    # 第二个 slot 失败时第一个 slot 也必须回滚。
    extra,lease,receipt=await candidate(repo,a,key="new.key")
    body=[{"text":"合成独立候选","logical_key":"new.key","payload":{},"epistemic_kind":"USER_ASSERTION","run_id":lease.run_id,"exchange_id":receipt["exchange_id"]},
          {"text":"冲突候选","logical_key":"response.language","payload":{},"epistemic_kind":"USER_ASSERTION","run_id":lease.run_id,"exchange_id":receipt["exchange_id"]}]
    # 新 operation 尚未存候选；保持候选 digest 不可变。
    fresh_lease,fresh_receipt=await source(repo,a)
    multi,_=await repo.prepare_formation(a,a.scope_id("USER"),fresh_receipt["exchange_id"],run_id=fresh_lease.run_id)
    for c in body: c.update(run_id=fresh_lease.run_id,exchange_id=fresh_receipt["exchange_id"])
    await repo.save_candidates(a,multi.operation_id,body)
    assert (await repo.commit_candidates(a,multi.operation_id))["outcome"]=="VERSION_CONFLICT"
    assert len(await repo.retrieve(a,[a.scope_id("USER")]))==1
    # 保守敏感过滤：候选 value/摘录也不能被写进长期 ACTIVE。
    from core.runtime.memory_service import MemoryService
    from core.runtime.final_memory_writer import CommittedExchangeReceipt
    from tests.test_semantic_memory_formation import remember
    secret="sk-public-synthetic-token-only"
    query="以后项目发布工具统一使用 "+secret+"。"
    lease,exchange=await source(repo,a,text=query)
    class SensitiveCandidate:
        def extract(self,*_args):
            return json.dumps({"schema_version":1,"candidates":[remember(category="ENGINEERING_CONSTRAINT",
                text="项目发布工具统一使用 "+secret+"。",value=secret,predicate="OPEN",pid=None,excerpt=query)]})
    filtered=await MemoryService(repo).form(access=a,receipt=CommittedExchangeReceipt(lease.run_id,exchange["exchange_id"],a.agent_id,"direct"),
        user_request=query,final_answer="不能成为事实来源的回答",extraction_model=SensitiveCandidate())
    assert filtered["outcome"]=="COMMITTED" and not filtered["records"]
    assert all(secret not in r["text"] for r in await repo.retrieve(a,[a.scope_id("USER")]))


@pytest.mark.asyncio
async def test_ttl_deletion_propagation_and_no_source_resurrection(clean_database):
    from core.persistence.models import MessageRow
    repo=MemoryLifecycleRepository(clean_database); a=await actor(clean_database)
    first,_,_=await candidate(repo,a)
    root=(await repo.commit_candidates(a,first.operation_id))["records"][0]["record_id"]
    row=(await repo.retrieve(a,[a.scope_id("USER")]))[0]
    derived,dependent_run,exchange=await candidate(repo,a,key="derived.key",manifest=row["dependencies"])
    child=(await repo.commit_candidates(a,derived.operation_id))["records"][0]["record_id"]
    pending,_,_=await candidate(repo,a,key="pending.key",manifest=row["dependencies"])
    expired,_,_=await candidate(repo,a,key="expired.key")
    expired_id=(await repo.commit_candidates(a,expired.operation_id))["records"][0]["record_id"]
    async with clean_database.transaction() as s:
        await s.execute(update(LongTermMemoryRow).where(LongTermMemoryRow.memory_id==expired_id).values(expires_at=datetime.now(UTC)-timedelta(seconds=1)))
    assert expired_id not in [r["memory_id"] for r in await repo.retrieve(a,[a.scope_id("USER")])]
    with pytest.raises(MemoryContextError,match="MEMORY_ERASURE_SCOPE_UNSUPPORTED"):
        await repo.mutate(a,a.scope_id("USER"),root,expected_version=1,command_id="all",action="DELETE",deletion_scope="ALL_COPIES")
    receipt=await repo.mutate(a,a.scope_id("USER"),root,expected_version=1,command_id="delete",action="DELETE")
    assert receipt["outcome"]=="COMPLETE_FOR_DECLARED_SCOPE" and receipt["all_copies_erased"] is False
    assert receipt["historical_payloads"]=="RETAINED_RESTRICTED_NO_REUSE" and receipt["backup_erasure"]=="UNKNOWN"
    assert await repo.mutate(a,a.scope_id("USER"),root,expected_version=1,command_id="delete",action="DELETE")==receipt
    assert not await repo.retrieve(a,[a.scope_id("USER")])
    history_refs=[d["id"] for h in await repo.history(a,a.scope_id("SESSION")) for d in h["dependencies"] if d["kind"]=="EXCHANGE"]
    assert exchange["exchange_id"] not in history_refs
    assert next(d["id"] for d in first.dependencies if d["kind"]=="EXCHANGE") not in history_refs
    with pytest.raises(MemoryContextError): await repo.commit_candidates(a,pending.operation_id)
    replay=await repo.append_exchange(a,run_id=dependent_run.run_id,user_text="原文不能复活",final_text="派生回答")
    assert replay["state"]=="SKIPPED_REVOKED"
    async with clean_database.session() as s:
        for rid in [root,child]:
            record=await s.get(LongTermMemoryRow,rid)
            assert record.canonical_text=="" and record.payload=={} and record.body_state=="REDACTED"
        assert (await s.get(MemoryOperationRow,pending.operation_id)).candidate_body is None
    # 尚未有 canonical 的来源也能抑制；新 kind/operation identity 不能绕开。
    pending_source,pending_lease,pending_exchange=await candidate(repo,a,key="new.pending")
    suppressed=await repo.revoke_source(a,a.scope_id("USER"),pending_source.operation_id,command_id="pending-revoke")
    assert await repo.revoke_source(a,a.scope_id("USER"),pending_source.operation_id,command_id="pending-revoke")==suppressed
    with pytest.raises(MemoryContextError): await repo.commit_candidates(a,pending_source.operation_id)
    with pytest.raises(MemoryContextError):
        await repo.prepare_formation(a,a.scope_id("USER"),pending_exchange["exchange_id"],run_id=pending_lease.run_id,kind="NEW_POLICY")
    # 无确认的 receipt 诚实降级。
    assert (await repo.receipt(a,a.scope_id("USER"),"unknown"))["coverage"]=="PARTIAL"
    with pytest.raises(MemoryContextError,match="MEMORY_CONTEXT_REVOKED"): await repo.guard(dependent_run)
    evidence=await repo.evidence(a,dependent_run.run_id)
    assert evidence["body"]["status"]=="INVALID" and evidence["body"]["coverage"]=="PARTIAL"
    assert "SEED_BINDING_MISSING" in evidence["body"]["gaps"]
    assert "用户偏好" not in json.dumps(evidence,ensure_ascii=False) and a.user_id not in json.dumps(evidence)
    assert await repo.evidence(a,dependent_run.run_id)==evidence

async def memory_crash_worker(phase, dsn, run_id, output_directory):
    from pathlib import Path
    import os
    from core.persistence.database import Database, DatabaseConfig
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.runtime.agent_loop.context import ContextPolicyV1
    from core.runtime.model_invocation import ModelAdapterResponse
    from core.runtime.recovery_coordinator import RecoveryCoordinator
    from tests._agent_loop_crash_worker import BoundaryRepository, BusinessReceiptTool
    from tests.test_agent_loop_runtime import assembly, execute, native, ObservationTool
    directory=Path(output_directory)
    access=MemoryAccessContext.from_dict(json.loads((directory/"access.json").read_text(encoding="utf8")))
    database=Database(DatabaseConfig(url=dsn,use_null_pool=True))
    control=DurableRunControlService(database,lease_seconds=1)
    repository=BoundaryRepository(database,control,"observation" if phase=="scoped-memory-crash" else "complete")
    tool=BusinessReceiptTool(directory,"scoped")
    class Provider:
        def supports_native_tool_calling(self): return True
        def invoke_request(self,messages,**kwargs):
            with (directory/"memory-models.jsonl").open("a",encoding="utf8") as out: out.write("dispatch\n")
            assert any("用户偏好使用中文" in m.get("content","") for m in messages)
            return native("loop_a","scoped-call")
    factory,_,_,_=await assembly(database,directory,[],tools=[tool,ObservationTool("loop_b")],
        context_model=Provider(),context_policy=ContextPolicyV1().to_dict(),context_window=32768,
        repository=repository,control=control)
    if phase=="scoped-memory-crash":
        result,_ = await execute(factory,run_id=run_id,persist=True,memory_access_context=access)
        image=await repository.load(run_id)
        raise AssertionError(("observation crash missing",result.error_code,result.safe_message,[(m.state,m.safe_error) for m in image.models],[(s.status,s.safe_error) for s in image.steps]))
    async def recovered(rid,lease,image):
        scope=await factory.create_rehydrated_run_scope(image,lease=lease,run_id=rid,persist=True)
        try:
            result=await scope.execute()
            assert result.status.value=="FAILED",(result.error_code,result.safe_message)
            assert result.error_code=="MEMORY_CONTEXT_REVOKED",(result.error_code,result.safe_message)
        finally: await scope.close(abort=True)
    recovery=RecoveryCoordinator(repository,control,recovered,instance_id="scoped-recovery")
    assert await recovery.scan_once()==1
    await asyncio.gather(*tuple(recovery._local_tasks))
    image=await repository.load(run_id)
    assert image.root.status=="FAILED",(image.root.last_recovery_error,image.root.status)
    assert image.loop_frames[0].stop_code=="MEMORY_CONTEXT_REVOKED"
    await database.dispose()


@pytest.mark.asyncio
async def test_frozen_memory_revocation_stops_real_process_recovery(clean_database,request,tmp_path,monkeypatch):
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.runtime.execution_aggregate import canonical_payload_digest as digest
    from tests.test_agent_loop_runtime import run_recovery_window_worker
    repo=MemoryLifecycleRepository(clean_database); a=await actor(clean_database)
    op,_,_=await candidate(repo,a)
    memory_id=(await repo.commit_candidates(a,op.operation_id))["records"][0]["record_id"]
    (tmp_path/"access.json").write_text(json.dumps(a.to_dict()),encoding="utf8")
    run_id="memory-crash-"+uuid4().hex
    await run_recovery_window_worker(request,tmp_path,"scoped-memory-crash",run_id,73)
    control=DurableRunControlService(clean_database,lease_seconds=1)
    runtime=DurableExecutionRepository(clean_database,control)
    before=await runtime.load(run_id)
    assert len(before.models)==1 and len(before.context_snapshots)==1
    assert len(before.tool_invocations)==1 and before.tool_invocations[0].state=="COMMITTED"
    assert before.loop_iterations[0].state=="OBSERVED"
    stable={"seed":before.loop_frames[0].seed_digest,"snapshot":before.context_snapshots[0].snapshot_digest,
        "decision":before.loop_iterations[0].decision_digest,"observation":before.loop_iterations[0].observation_digest,
        "tool":before.tool_invocations[0].committed_result_digest,"cost":before.root.budget_consumed}
    await repo.mutate(a,a.scope_id("USER"),memory_id,expected_version=1,command_id="revoke-frozen",action="REVOKE")
    await asyncio.sleep(1.1)
    await run_recovery_window_worker(request,tmp_path,"scoped-memory-recover",run_id,0)
    after=await runtime.load(run_id)
    assert after.root.status=="FAILED" and after.loop_frames[0].stop_code=="MEMORY_CONTEXT_REVOKED"
    assert len(after.models)==1 and len(after.tool_invocations)==1
    assert stable=={"seed":after.loop_frames[0].seed_digest,"snapshot":after.context_snapshots[0].snapshot_digest,
        "decision":after.loop_iterations[0].decision_digest,"observation":after.loop_iterations[0].observation_digest,
        "tool":after.tool_invocations[0].committed_result_digest,"cost":after.root.budget_consumed}
    assert (tmp_path/"memory-models.jsonl").read_text().splitlines()==["dispatch"]
    assert len((tmp_path/"dispatch.jsonl").read_text().splitlines())==1
    evidence=await repo.evidence(a,run_id)
    assert evidence["body"]["coverage"]=="COMPLETE",evidence["body"]["gaps"]
    assert evidence["body"]["reason"]=="MEMORY_CONTEXT_REVOKED" and evidence["body"]["snapshot_refs"]
    assert not any(secret in json.dumps(evidence,ensure_ascii=False) for secret in ["用户偏好",a.user_id,a.tenant_id])
    # STARTED 已提交才失去资格：保持 UNKNOWN + 原 Suspension，不能伪装失败/未执行。
    from core.runtime.agent_loop.contracts import LoopSuspended
    from tests._agent_loop_crash_worker import BusinessReceiptTool
    from tests.test_agent_loop_runtime import assembly,execute,native,ObservationTool
    fresh,_,_=await candidate(repo,a,key="unknown-boundary")
    fresh_id=(await repo.commit_candidates(a,fresh.operation_id))["records"][0]["record_id"]
    boundary=tmp_path/"started-boundary"; boundary.mkdir()
    tool=BusinessReceiptTool(boundary,"scoped")
    factory,unknown_runtime,unknown_model,_=await assembly(clean_database,boundary,[native("loop_a","unknown-call")],tools=[tool,ObservationTool("loop_b")])
    service=factory.services.durable_tool_invocation
    original_start=service.start
    async def revoke_after_started(**kwargs):
        result=await original_start(**kwargs)
        await repo.mutate(a,a.scope_id("USER"),fresh_id,expected_version=1,command_id="after-started",action="REVOKE")
        return result
    monkeypatch.setattr(service,"start",revoke_after_started)
    scope=await factory.create_run_scope("core_router","调用 loop_a 和 loop_b，完成后回答",execution_policy="durable-agent-loop.v1",persist=True,memory_access_context=a)
    try:
        try:
            unexpected=await scope.execute()
        except LoopSuspended as exc:
            assert exc.reason=="TOOL_OUTCOME_UNKNOWN"
        else:
            failed=await unknown_runtime.load(scope.run_id)
            pytest.fail(str((unexpected.error_code,failed.loop_frames[0].stop_code,[(r.state,r.last_safe_error_code) for r in failed.tool_invocations])))
        pending=await unknown_runtime.load(scope.run_id)
        assert pending.root.status=="ACTIVE" and pending.loop_frames[0].phase=="WAIT_TOOL"
        assert pending.tool_invocations[0].state=="UNKNOWN" and len(unknown_model.requests)==1
        assert not (boundary/"dispatch.jsonl").exists()
        partial=(await repo.evidence(a,scope.run_id))["body"]
        assert partial["coverage"]=="PARTIAL" and "RUN_NOT_TERMINAL" in partial["gaps"]
    finally: await scope.close(abort=True)


async def formation_crash_worker(phase,dsn,run_id,output_directory):
    from pathlib import Path
    import os
    from core.persistence.database import Database,DatabaseConfig
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.persistence.memory import PostgresMemoryManager,PostgresMemoryManagerBridge,PostgresAdvancedMemoryStore,PostgresAdvancedMemoryStoreBridge
    from core.persistence.sync_bridge import SyncPersistenceBridge
    from core.runtime.agent_loop.context import ContextPolicyV1
    from core.runtime.model_invocation import ModelAdapterResponse
    from core.runtime.recovery_coordinator import RecoveryCoordinator
    from tests.test_agent_loop_runtime import assembly
    from tests.test_semantic_memory_formation import remember
    directory=Path(output_directory)
    access=MemoryAccessContext.from_dict(json.loads((directory/"access.json").read_text(encoding="utf8")))
    database=Database(DatabaseConfig(url=dsn,use_null_pool=True))
    control=DurableRunControlService(database,lease_seconds=1)
    class CompletedBoundary(DurableExecutionRepository):
        async def finish_model_attempt(self,*args,**kwargs):
            result=await super().finish_model_attempt(*args,**kwargs)
            image=await self.load(run_id)
            if phase=="scoped-formation-result" and image.models[-1].purpose=="FORMATION" and image.models[-1].state=="COMPLETED":
                os._exit(84)
            return result
    runtime=CompletedBoundary(database,control)
    bridge=SyncPersistenceBridge(asyncio.get_running_loop())
    memory=PostgresMemoryManagerBridge(PostgresMemoryManager(database),bridge)
    memory.advanced_store=PostgresAdvancedMemoryStoreBridge(PostgresAdvancedMemoryStore(database),bridge)
    proposal=json.dumps({"schema_version":1,"candidates":[remember(category="PROJECT_STABLE_FACT",text="项目数据库统一使用PostgreSQL。",
        value="PostgreSQL",predicate="REGISTERED",pid="project.database",excerpt="以后项目数据库统一使用PostgreSQL。",reason="EXPLICIT_CONSTRAINT")]},ensure_ascii=False)
    def scripted(messages):
        with (directory/"formation-models.jsonl").open("a",encoding="utf8") as out: out.write("dispatch\n")
        is_formation="长期记忆候选提取器" in messages[0]["content"]
        return ModelAdapterResponse(proposal if is_formation else "已收到长期约束",finish_reason="stop",tool_call_count=0)
    factory,_,model,_=await assembly(database,directory,[scripted]*3,memory_manager=memory,repository=runtime,control=control,
        context_policy=ContextPolicyV1().to_dict(),owner="formation-worker")
    def invoke_request(messages,*,max_tokens,generation_options,run_context):
        return model.invoke(messages,max_tokens=max_tokens)
    model.invoke_request=invoke_request
    if phase=="scoped-formation-result":
        scope=await factory.create_run_scope("core_router","以后项目数据库统一使用PostgreSQL。",execution_policy="durable-agent-loop.v1",
            run_id=run_id,persist=True,memory_access_context=access)
        await scope.execute()
        raise AssertionError("formation completion boundary missing")
    async def recovered(rid,lease,image):
        scope=await factory.create_rehydrated_run_scope(image,lease=lease,run_id=rid,persist=True)
        try:
            assert scope.coordinator._step_completion_owner._semantic_memory_formation is not None
            result=await scope.execute()
            assert result.status.value=="SUCCEEDED",(result.error_code,result.safe_message)
        finally: await scope.close(abort=True)
    recovery=RecoveryCoordinator(runtime,control,recovered,instance_id="formation-recovery")
    assert await recovery.scan_once()==1
    await asyncio.gather(*tuple(recovery._local_tasks))
    image=await runtime.load(run_id)
    assert image.root.status=="SUCCEEDED",(image.root.status,image.root.last_recovery_error)
    assert len(await memory.async_store.lifecycle.retrieve(access,[access.scope_id("USER")]))==1
    await database.dispose()


@pytest.mark.asyncio
async def test_delivered_exchange_formation_uses_existing_model_owner(clean_database,tmp_path,request):
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.runtime.agent_loop.context_evidence import capture_context_evidence
    from tests.test_agent_loop_runtime import run_recovery_window_worker
    access=await actor(clean_database)
    (tmp_path/"access.json").write_text(json.dumps(access.to_dict()),encoding="utf8")
    rid="formation-crash-"+uuid4().hex
    repo=MemoryLifecycleRepository(clean_database)
    runtime=DurableExecutionRepository(clean_database,DurableRunControlService(clean_database,lease_seconds=1))
    await run_recovery_window_worker(request,tmp_path,"scoped-formation-result",rid,84)
    before=await runtime.load(rid)
    assert [m.purpose for m in before.models]==["DECISION","FORMATION"]
    assert before.models[-1].state=="COMPLETED" and before.root.budget_consumed["model_calls"]==2
    receipt=before.loop_frames[0].final_delivery_receipt
    assert receipt and not await repo.retrieve(access,[access.scope_id("USER")])
    await asyncio.sleep(1.1)
    await run_recovery_window_worker(request,tmp_path,"scoped-formation-recover",rid,0)
    image=await runtime.load(rid)
    records=await repo.retrieve(access,[access.scope_id("USER")]); assert len(records)==1
    assert len(image.models)==2 and image.root.budget_consumed==before.root.budget_consumed
    assert image.loop_frames[0].final_delivery_receipt==receipt
    assert (tmp_path/"formation-models.jsonl").read_text().splitlines()==["dispatch","dispatch"]
    async with clean_database.session() as session:
        operation=await session.get(MemoryOperationRow,image.models[-1].memory_operation_id)
    assert operation.state=="COMMITTED" and await repo.commit_candidates(access,operation.operation_id)==operation.receipt
    context=(await capture_context_evidence(runtime,rid))["body"]
    assert context["coverage"]=="COMPLETE",context["gaps"]
    assert [m["purpose"] for m in context["models"]]==["DECISION"]
    memory=(await repo.evidence(access,rid))["body"]
    assert any(o["operation_ref"]==operation.operation_id for o in memory["operations"])
    assert memory["coverage"]=="COMPLETE",memory["gaps"]
    # 新用户陈述可独立更正；不能继承将被 supersede 的旧值而自我失效。
    from core.persistence.memory import PostgresMemoryManager,PostgresMemoryManagerBridge,PostgresAdvancedMemoryStore,PostgresAdvancedMemoryStoreBridge
    from core.persistence.sync_bridge import SyncPersistenceBridge
    from core.runtime.agent_loop.context import ContextPolicyV1
    from core.runtime.model_invocation import ModelAdapterResponse
    from tests.test_agent_loop_runtime import assembly
    from tests.test_semantic_memory_formation import remember
    bridge=SyncPersistenceBridge(asyncio.get_running_loop())
    manager=PostgresMemoryManagerBridge(PostgresMemoryManager(clean_database),bridge)
    manager.advanced_store=PostgresAdvancedMemoryStoreBridge(PostgresAdvancedMemoryStore(clean_database),bridge)
    new_query="以后项目数据库改为SQLite。"
    def correction(messages):
        assert "PostgreSQL" not in messages[-1]["content"] and "已更正回答" not in messages[-1]["content"]
        assert new_query in messages[-1]["content"]
        return ModelAdapterResponse(json.dumps({"schema_version":1,"candidates":[remember(category="PROJECT_STABLE_FACT",
            text="项目数据库统一改为SQLite。",value="SQLite",predicate="REGISTERED",pid="project.database",excerpt=new_query)]},ensure_ascii=False))
    factory,second_runtime,second_model,_=await assembly(clean_database,tmp_path,
        [ModelAdapterResponse("已更正回答",finish_reason="stop",tool_call_count=0),correction],memory_manager=manager,
        context_policy=ContextPolicyV1().to_dict(),owner="correction-owner")
    second_model.invoke_request=lambda messages,**kw: second_model.invoke(messages,max_tokens=kw["max_tokens"])
    scope=await factory.create_run_scope("core_router",new_query,persist=True,memory_access_context=access,execution_policy="durable-agent-loop.v1")
    try: second_result=await scope.execute(); second_id=scope.run_id
    finally: await scope.close(abort=True)
    assert second_result.status.value=="SUCCEEDED",second_result.error_code
    visible=await repo.retrieve(access,[access.scope_id("USER")])
    assert len(visible)==1 and visible[0]["version"]==2 and "SQLite" in visible[0]["text"]
    assert len(second_model.requests)==2
    async with clean_database.session() as session:
        old=await session.get(LongTermMemoryRow,records[0]["memory_id"])
        new=await session.get(LongTermMemoryRow,visible[0]["memory_id"])
        old_exchange,new_exchange=old.origin_exchange_id,new.origin_exchange_id
        assert old.lifecycle_status=="SUPERSEDED" and any(d["kind"]=="USER_MESSAGE" for d in new.source_dependencies)
    exchange={"id":old_exchange}
    await repo.delete_exchange(access,exchange["id"],expected_version=1,command_id="remove-exchange")
    assert (await repo.append_exchange(access,run_id=rid,user_text="不应重建",final_text="不应重建"))["state"]=="SKIPPED_REVOKED"
    assert len(await repo.retrieve(access,[access.scope_id("USER")]))==1  # 独立新用户来源保留。
    await repo.delete_exchange(access,new_exchange,expected_version=1,command_id="remove-new-exchange")
    assert not await repo.retrieve(access,[access.scope_id("USER")])
    assert (await runtime.load(rid)).loop_frames[0].final_delivery_receipt==receipt


@pytest.mark.asyncio
async def test_http_principal_session_application_factory_and_stateless_boundary(clean_database,tmp_path,monkeypatch):
    from types import SimpleNamespace
    from starlette.requests import Request
    from pydantic import ValidationError
    import server
    from core.auth import Principal, AuthorizationService
    from core.agent_platform.application import AgentApplicationService
    from core.chat_service import ChatService
    from core.runtime.agent_registry import AgentRegistry
    from core.runtime.model_invocation import ModelAdapterResponse
    from core.persistence.memory import PostgresMemoryManager, PostgresMemoryManagerBridge
    from core.persistence.sync_bridge import SyncPersistenceBridge
    from tests.test_agent_loop_runtime import assembly, execute
    a=await actor(clean_database,session="selected-session")
    principal=Principal(UUID(a.user_id),a.user_id,frozenset({"USER"}),"synthetic-jti",datetime.now(UTC),
        datetime.now(UTC)+timedelta(minutes=5),"HUMAN",frozenset(),a.tenant_id)
    memory=PostgresMemoryManagerBridge(PostgresMemoryManager(clean_database),SyncPersistenceBridge(asyncio.get_running_loop()))
    factory,runtime,model,_=await assembly(clean_database,tmp_path,[ModelAdapterResponse("身份链路完成",finish_reason="stop",tool_call_count=0),
        ModelAdapterResponse("无记忆链路完成",finish_reason="stop",tool_call_count=0)],memory_manager=memory)
    registrations=[factory._agent_registry.resolve(name) for name in factory._agent_registry.agent_ids]
    registry=AgentRegistry(replace(r,definition=replace(r.definition,business_options={**r.definition.business_options,
        "execution_policy":"durable-agent-loop.v1"})) if r.agent_id==a.agent_id else r for r in registrations)
    factory._agent_registry=registry; factory._router.agent_registry=registry
    chat=ChatService(factory._router,coordinated_runtime_factory=factory)
    app_service=AgentApplicationService(chat,SimpleNamespace(require_entry=lambda name: SimpleNamespace(
        definition=registry.resolve(name).definition,workflow=None)))
    monkeypatch.setattr(server,"require_service",lambda:chat)
    monkeypatch.setattr(server,"require_agent_application_service",lambda:app_service)
    app=SimpleNamespace(state=SimpleNamespace(authorization_service=AuthorizationService(clean_database)))
    request=Request({"type":"http","app":app,"state":{"principal":principal}})
    rid=str(uuid4())
    body=server.RuntimeExecuteRequest(agent_id=a.agent_id,query="本次只验证身份传递",run_id=rid,timeout_seconds=20,session_id=a.session_id)
    response=await server.runtime_execute_endpoint(body,request)
    actual = json.loads(response.body) if hasattr(response,"body") else response.model_dump()
    assert actual.get("status")=="SUCCEEDED",actual
    binding=await memory.async_store.lifecycle.binding(rid)
    assert binding.access==a.to_dict() and binding.mode=="SCOPED"
    assert len(await memory.async_store.lifecycle.history(a,a.scope_id("SESSION")))==1
    with pytest.raises(ValidationError):
        server.RuntimeExecuteRequest.model_validate({**body.model_dump(),"tenant_id":"spoof","user_id":str(uuid4())})
    with pytest.raises(MemoryContextError): MemoryAccessContext.from_principal(None,a.agent_id)
    result,stateless_run=await execute(factory,persist=False)
    assert result.status.value=="SUCCEEDED"
    anonymous=await memory.async_store.lifecycle.binding(stateless_run)
    assert anonymous.mode=="STATELESS" and anonymous.access is None and anonymous.manifest==[]
    assert len(model.requests)==2

@pytest.mark.asyncio
async def test_preplanner_admission_survives_lease_takeover_without_redispatch(clean_database,tmp_path):
    from core.runtime.memory_context_guard import MemoryContextGuard
    from core.runtime.model_invocation import ModelAdapterResponse
    from core.runtime import TaskCapabilityRequirements
    from core.runtime.run_control import OwnershipLost
    from tests.test_agent_loop_runtime import assembly
    control=DurableRunControlService(clean_database,lease_seconds=1)
    factory,runtime,model,_=await assembly(clean_database,tmp_path,[ModelAdapterResponse("pre-root result")],control=control)
    scope=await factory.create_run_scope("core_router","本次检查 pre-root 准入",persist=False,execution_policy="fixed-plan.v1")
    context=scope.run_context
    def invoke(factory, scope):
        return factory._router._invoke_model_contract(agent_id="core_router",user_query="合成请求",
            messages=[{"role":"user","content":"合成 pre-root 请求"}],context_requirements=None,
            run_context=scope.run_context,capability_requirements=TaskCapabilityRequirements(),max_tokens=64,event_emitter=scope.event_emitter)
    recovered_scope=None
    try:
        await asyncio.to_thread(invoke,factory,scope)
        repo=MemoryLifecycleRepository(clean_database)
        binding=await repo.binding(context.run_id)
        assert binding.admissions[-1]["state"]=="COMPLETED" and binding.admissions[-1]["result_digest"]
        assert await runtime.load(context.run_id) is None
        old=context.durable_lease
        await asyncio.sleep(1.1)
        new_factory,_,new_model,_=await assembly(clean_database,tmp_path,[],control=control,repository=runtime,owner="new-preplan-owner")
        recovered_scope=await new_factory.create_run_scope("core_router","本次检查 pre-root 准入",run_id=context.run_id,
            persist=False,execution_policy="fixed-plan.v1")
        with pytest.raises(OwnershipLost): await repo.guard(old)
        with pytest.raises(MemoryContextError,match="MEMORY_CONTEXT_UNVERIFIABLE"):
            await asyncio.to_thread(invoke,new_factory,recovered_scope)
        assert len(model.requests)==1 and not new_model.requests
        assert scope.budget_ledger.snapshot().committed_usage.model_calls==1
        assert (await repo.binding(context.run_id)).status=="INVALID"
    finally:
        if recovered_scope: await recovered_scope.close(abort=True)
        await scope.close(abort=True)


@pytest.mark.asyncio
async def test_revocation_after_final_decision_blocks_publication_and_evidence_is_bound(clean_database,tmp_path):
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.runtime.model_invocation import ModelAdapterResponse
    from tests.test_agent_loop_runtime import assembly,execute
    repo=MemoryLifecycleRepository(clean_database); access=await actor(clean_database)
    op,_,_=await candidate(repo,access)
    memory_id=(await repo.commit_candidates(access,op.operation_id))["records"][0]["record_id"]
    control=DurableRunControlService(clean_database,lease_seconds=10)
    class RevokeBeforePublish(DurableExecutionRepository):
        async def accept_loop_decision(self,*args,**kwargs):
            row=await super().accept_loop_decision(*args,**kwargs)
            await repo.mutate(access,access.scope_id("USER"),memory_id,expected_version=1,
                command_id="after-final-decision",action="REVOKE")
            return row
    runtime=RevokeBeforePublish(clean_database,control)
    factory,_,model,_=await assembly(clean_database,tmp_path,[ModelAdapterResponse("不得发布的合成 Final",finish_reason="stop",tool_call_count=0)],
        repository=runtime,control=control)
    result,rid=await execute(factory,persist=True,memory_access_context=access)
    image=await runtime.load(rid)
    assert result.status.value=="FAILED" and result.error_code=="MEMORY_CONTEXT_REVOKED",(result.error_code,result.safe_message)
    assert image.loop_iterations[0].decision_kind=="FINAL" and image.loop_iterations[0].decision_digest
    assert image.loop_frames[0].final_delivery_receipt is None
    assert len(model.requests)==1 and image.root.budget_consumed["model_calls"]==1
    evidence=(await repo.evidence(access,rid))["body"]
    assert evidence["coverage"]=="COMPLETE",evidence["gaps"]
    assert len(evidence["model_invocations"])==1 and evidence["model_invocations"][0]["purpose"]=="DECISION"
    assert evidence["status"]=="INVALID" and evidence["reason"]=="MEMORY_CONTEXT_REVOKED"
