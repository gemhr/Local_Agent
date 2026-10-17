"""生产停机与质量 Receipt 的有限组合验收；使用真实隔离 PostgreSQL。"""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, func


@pytest.mark.asyncio
async def test_lifespan_shutdown_fences_recovery_and_quality_receipt(clean_database, monkeypatch, tmp_path):
    import server
    from fastapi import FastAPI
    from core.auth import Principal, EVALUATION_EXECUTE_SCOPE
    from core.persistence.models import UserRow, RunControlRow, RuntimeAgentEvaluationReceiptRow
    from core.runtime.recovery_coordinator import RecoveryCoordinator
    from core.runtime.health import resolve_application_diagnostic
    from core.agent_platform.agent_quality_evaluation import register_agent_quality_evaluation
    from core.runtime.agent_loop.quality_contract import catalog_digest
    from core.agent_platform.quality_configuration import runtime_build_digest, check_agent
    from core.runtime.execution_aggregate import canonical_payload_digest
    from core.runtime.agent_loop.context import _pin_value

    class Model:
        def generate(self, *args, **kwargs):
            raise AssertionError("shutdown must not invoke provider")
        def close(self):
            pass
    monkeypatch.setattr(server, "LocalLLMEngine", lambda **kwargs: Model())
    monkeypatch.setattr(server, "VectorDBManager", lambda **kwargs: (_ for _ in ()).throw(ValueError("synthetic optional KB unavailable")))
    monkeypatch.setattr(server, "settings", replace(server.settings, llm_backend="local",
        database_url=clean_database.config.url, knowledge_base_required=False,
        embedding_model_path=str(tmp_path / "missing-embedding"), chroma_dir=str(tmp_path / "chroma"),
        snapshot_store_enabled=False))
    for key in ("LOCAL_AGENT_STAGE13_DELIVERY_ENABLED", "LOCAL_AGENT_STAGE13_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    # 禁止自动扫描；仍由真实 lifespan 装配原 Coordinator，测试显式驱动竞争窗口。
    async def idle_start(self):
        pass
    monkeypatch.setattr(RecoveryCoordinator, "start", idle_start)
    app = FastAPI()
    lifetime = server.lifespan(app)
    await lifetime.__aenter__()
    services = app.state.runtime_services
    recovery = app.state.recovery_coordinator
    repository = recovery.repository
    control = recovery.run_control
    ids = ["shutdown-" + uuid4().hex for _ in range(3)]
    original_claim = control.claim
    claim_returned, release_claim = asyncio.Event(), asyncio.Event()
    discovered, release_discovery = asyncio.Event(), asyncio.Event()
    claimed, executed, reconciled = [], [], []
    async def candidates(**kwargs):
        return ids[:2]
    async def claim(run_id, owner):
        result = await original_claim(run_id, owner)
        claimed.append(run_id)
        claim_returned.set()
        await release_claim.wait()
        return result
    async def reconciliation_candidates(**kwargs):
        discovered.set()
        await release_discovery.wait()
        return [SimpleNamespace(run_id=ids[2])]
    monkeypatch.setattr(repository, "stale_run_ids", candidates)
    monkeypatch.setattr(repository, "reconciliation_candidates", reconciliation_candidates)
    monkeypatch.setattr(control, "claim", claim)
    recovery.rehydrate_and_execute = lambda *args: executed.append(args)
    async def reconcile(candidate):
        reconciled.append(candidate)
    recovery.configure_reconciliation(reconcile)
    scan = asyncio.create_task(recovery.scan_once())
    reconciliation = asyncio.create_task(recovery.reconciliation_scan_once())
    await asyncio.wait_for(claim_returned.wait(), 5)
    await asyncio.wait_for(discovered.wait(), 5)

    # 首个异步清理（delivery close）挂 barrier，状态必须已由 lifespan 发布。
    cleaning, finish_cleanup = asyncio.Event(), asyncio.Event()
    # 默认生产配置的第一个 await 是原 Recovery stop；不手动关 Gate。
    original_stop = recovery.stop
    async def stop():
        assert services.lifecycle_state.value == "SHUTTING_DOWN"
        assert services.admission_gate.state.value == "DRAINING"
        assert recovery.stopping
        cleaning.set()
        await finish_cleanup.wait()
        await original_stop()
    monkeypatch.setattr(recovery, "stop", stop)
    closing = asyncio.create_task(lifetime.__aexit__(None, None, None))
    await asyncio.wait_for(cleaning.wait(), 5)
    diagnostic = resolve_application_diagnostic(services)
    assert diagnostic.status.value == "DRAINING"
    assert not services.admission_gate.accepts_new_runs

    uid, project = uuid4(), str(uuid4())
    tenant = "00000000-0000-0000-0000-000000000001"
    async with clean_database.transaction() as session:
        session.add(UserRow(id=uid, subject=str(uid), tenant_id=tenant, principal_kind="SERVICE"))
    now = datetime.now(UTC)
    principal = Principal(uid, str(uid), frozenset({"SERVICE"}), "public-shutdown", now,
        now + timedelta(minutes=5), "SERVICE", frozenset({EVALUATION_EXECUTE_SCOPE}), tenant)
    service = app.state.agent_application_service
    definition = service._registry.require_entry("core_router").definition
    from core.agent_platform.application import AgentApplicationService
    definition = replace(definition, business_options={**definition.business_options, "execution_policy": "durable-agent-loop.v1"})
    service = AgentApplicationService(service._chat_service, SimpleNamespace(require_entry=lambda name:
        SimpleNamespace(definition=definition, workflow=None)))
    manifest = {"schema_version": "agent-quality-execution-manifest.v1", "runtime_build_digest": runtime_build_digest(),
        "public_fixture_version": "public-shutdown.v1", "memory_effect_policy": "SERVICE_PROJECT_READ_ONLY.v1",
        "agents": {"core_router": {"agent_version": definition.agent_version, "execution_policy": "durable-agent-loop.v1",
            "definition_digest": canonical_payload_digest(_pin_value(definition)), "runtime_config_digest": "a" * 64}}}
    profile = {"id": "shutdown", "version": 1, "policy": "agent-quality-capture.v1", "tool_aliases": {}, "parameter_paths": {}, "fact_keys": []}
    path = tmp_path / "managed.json"
    path.write_text(json.dumps({"manifest": manifest, "capture_profile": profile}), encoding="utf-8")
    quality = FastAPI()
    quality.state.loop_execution_repository = repository
    @quality.middleware("http")
    async def identity(request, call_next):
        request.state.principal = principal
        return await call_next(request)
    async def forbidden_io(*args, **kwargs):
        raise AssertionError("closed admission must not execute or bind")
    register_agent_quality_evaluation(quality, settings=SimpleNamespace(quality_evaluation_project_id=project,
        quality_evaluation_service_principal_id=str(uid), quality_evaluation_configuration_path=str(path)),
        require_service=lambda: service, bind_run=forbidden_io, authorize_run=forbidden_io, final_answer_capture=forbidden_io)
    rid = str(uuid4())
    payload = {"agent_id": "core_router", "query": "public shutdown", "run_id": rid, "attempt_id": rid,
        "request_id": str(uuid4()), "evaluation_project_id": project, "evaluation_run_id": str(uuid4()),
        "case_id": str(uuid4()), "case_version": "v1", "case_digest": "a" * 64,
        "execution_manifest_digest": catalog_digest(manifest), "capture_profile_id": profile["id"],
        "capture_profile_version": 1, "capture_profile_digest": catalog_digest(profile), "timeout_milliseconds": 10000}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=quality), base_url="http://test", trust_env=False) as client:
        response = await client.post("/api/runtime/evaluation-execute/agent-quality/v1", json=payload)
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "RUNTIME_SHUTTING_DOWN"
    release_claim.set(); release_discovery.set()
    assert await scan == 0 and await reconciliation == 0
    assert claimed == ids[:1] and not executed and not reconciled
    async with clean_database.session() as session:
        lease_row = await session.get(RunControlRow, ids[0])
        assert lease_row.owner_id is None and lease_row.state == "ACTIVE"
        assert await session.scalar(select(func.count()).select_from(RuntimeAgentEvaluationReceiptRow)) == 0
    finish_cleanup.set()
    await asyncio.wait_for(closing, 15)
    report = await app.state.runtime_shutdown_coordinator.shutdown()
    assert report.orchestration_completed
    assert services.admission_gate.state.value == "CLOSED"


async def _quality_child_window(database, request, directory, *, revoke):
    import os
    import sqlite3
    import sys
    from pathlib import Path
    from core.persistence.models import UserRow, MemoryOperationRow, MessageExchangeRow
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
    from core.persistence.repositories.agent_quality import AgentQualityReceiptRepository
    from core.runtime.run_control import DurableRunControlService
    from tests.test_scoped_memory_lifecycle import actor, candidate
    lifecycle = MemoryLifecycleRepository(database)
    owner = await actor(database)
    uid, project, rid = uuid4(), str(uuid4()), str(uuid4())
    async with database.transaction() as session:
        session.add(UserRow(id=uid, subject=str(uid), tenant_id=owner.tenant_id, principal_kind="SERVICE"))
    sid = await lifecycle.configure_project(tenant_id=owner.tenant_id, project_id=project, agent_id="core_router",
        grants={owner.user_id: ["READ", "WRITE", "PROMOTE", "DELETE", "REVOKE"], str(uid): ["READ"]}, ttl_seconds=3600)
    operation, _, _ = await candidate(lifecycle, owner, key="recovery.source", text="public delivery_code=ZX9")
    record = (await lifecycle.commit_candidates(owner, operation.operation_id))["records"][0]["record_id"]
    promoted = await lifecycle.promote(owner, record, sid, expected_version=1, command_id="recovery.project")
    (directory / "identity.json").write_text(json.dumps({"user_id": str(uid), "project_id": project,
        "tenant_id": owner.tenant_id, "run_id": rid}), encoding="utf-8")
    async def worker(phase, expected):
        process = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).parent / "fixtures/quality_recovery_worker.py"),
            phase, request.getfixturevalue("pg_url"), str(directory), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        try:
            out, err = await asyncio.wait_for(process.communicate(), 45)
        except TimeoutError:
            process.kill(); await process.wait(); raise
        assert process.returncode == expected, out.decode("utf-8") + err.decode("utf-8")
    await worker("start", 85)
    repository = DurableExecutionRepository(database, DurableRunControlService(database))
    before = await repository.load(rid)
    child = next(f for f in before.loop_frames if f.frame_role == "CHILD")
    assert child.phase == "RESULT_READY" and len(before.models) == 3
    assert len(before.tool_invocations) == 2 and all(t.state == "COMMITTED" for t in before.tool_invocations)
    assert before.context_snapshots and all(f.final_delivery_receipt is None for f in before.loop_frames)
    receipts = AgentQualityReceiptRepository(database)
    receipt = await receipts.read(rid)
    assert receipt is not None and receipt.sealed_response is None
    async with database.session() as session:
        formation_ids = set((await session.scalars(select(MemoryOperationRow.operation_id).where(MemoryOperationRow.kind == "FORMATION"))).all())
    if revoke:
        await lifecycle.mutate(owner, sid, promoted["record_id"], expected_version=1, command_id="recovery.revoke", action="REVOKE")
    await asyncio.sleep(1.1)
    await worker("revoked" if revoke else "resume", 0)
    after = await repository.load(rid)
    assert after.root.status == ("FAILED" if revoke else "SUCCEEDED")
    assert len(after.models) == (3 if revoke else 4)
    assert [(t.invocation_id, t.state, t.committed_result_digest) for t in before.tool_invocations] == [(t.invocation_id, t.state, t.committed_result_digest) for t in after.tool_invocations]
    assert [(f.frame_id, f.seed_digest) for f in before.loop_frames] == [(f.frame_id, f.seed_digest) for f in after.loop_frames]
    old_snapshots = {s.snapshot_id: (s.snapshot_digest, s.payload) for s in before.context_snapshots}
    assert all(old_snapshots[s.snapshot_id] == (s.snapshot_digest, s.payload) for s in after.context_snapshots if s.snapshot_id in old_snapshots)
    assert all(after.root.budget_operations[key] == value for key, value in before.root.budget_operations.items())
    assert after.root.budget_consumed["model_calls"] == (3 if revoke else 4)
    assert after.root.budget_consumed["tool_calls"] == 2
    assert len((directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()) == (3 if revoke else 4)
    assert len((directory / "dispatch.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    with sqlite3.connect(str(directory / "business.sqlite")) as business:
        assert business.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    from core.persistence.models import RuntimeAgentDelegationRow
    async with database.session() as session:
        delegation = await session.get(RuntimeAgentDelegationRow, child.delegation_id)
        assert delegation.result["content"] == "public child final"
        assert delegation.observed is (not revoke)
    sealed = await receipts.read(rid)
    assert sealed.sealed_response == (directory / "sealed.json").read_text(encoding="utf-8")
    assert (await receipts.read(rid)).sealed_digest == sealed.sealed_digest
    wire = json.loads(sealed.sealed_response)
    assert wire["bundle"]["coverage"] == ("PARTIAL" if revoke else "COMPLETE"), wire["bundle"]["gaps"]
    root = next(f for f in after.loop_frames if f.frame_role == "ROOT")
    assert (root.final_delivery_receipt is None) is revoke
    from core.runtime.client_event_feed import PostgresClientEventFeed
    events = await PostgresClientEventFeed(database).read_after(rid, 0, 200)
    assert sum(e.event_type == "output.delta" for e in events) == (0 if revoke else 1)
    async with database.session() as session:
        assert set((await session.scalars(select(MemoryOperationRow.operation_id).where(MemoryOperationRow.kind == "FORMATION"))).all()) == formation_ids
        assert await session.scalar(select(func.count()).select_from(MessageExchangeRow).where(MessageExchangeRow.run_id == rid)) == 0
    assert not any(m.purpose == "FORMATION" for m in after.models)


@pytest.mark.asyncio
async def test_quality_handoff_child_result_recovers_once(clean_database, request, tmp_path):
    await _quality_child_window(clean_database, request, tmp_path, revoke=False)


@pytest.mark.asyncio
async def test_quality_handoff_project_revocation_blocks_resume(clean_database, request, tmp_path):
    await _quality_child_window(clean_database, request, tmp_path, revoke=True)
