"""质量评测核心验收；只使用隔离 PG 和公开合成身份。"""
import asyncio
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from core.auth import Principal, EVALUATION_EXECUTE_SCOPE
from core.persistence.repositories.agent_quality import AgentQualityReceiptRepository
from core.persistence.errors import PersistenceError
from core.runtime.agent_loop.quality_contract import catalog_digest


@pytest.mark.asyncio
async def test_quality_receipt_concurrent_admission_and_immutable_seal(clean_database):
    now = datetime.now(UTC)
    principal = Principal(uuid4(), "public-quality-service", frozenset({"SERVICE"}), "synthetic-token",
        now, now + timedelta(minutes=5), "SERVICE", frozenset({EVALUATION_EXECUTE_SCOPE}), "synthetic-tenant")
    repository = AgentQualityReceiptRepository(clean_database)
    run, request = str(uuid4()), str(uuid4())
    correlation = {"run_id": run, "attempt_id": run, "request_id": request,
        "evaluation_project_id": str(uuid4()), "evaluation_run_id": str(uuid4()),
        "query_digest": catalog_digest("public synthetic query")}
    kwargs = dict(correlation=correlation, principal=principal, manifest={"public": True}, profile={"public": True})
    admissions = await asyncio.gather(*(repository.admit(**kwargs) for _ in range(4)))
    assert sum(winner for winner, _ in admissions) == 1
    assert all(row.sealed_response is None for _, row in admissions)
    with pytest.raises(ValueError, match="QUALITY_REQUEST_CONFLICT"):
        await repository.admit(**{**kwargs, "correlation": {**correlation, "query_digest": catalog_digest("changed")}})
    with pytest.raises(ValueError, match="QUALITY_REQUEST_CONFLICT"):
        await repository.admit(**{**kwargs, "correlation": {**correlation, "run_id": str(uuid4())}})
    first = {"schema_version": "public-test-seal.v1", "coverage": "PARTIAL", "nullable": None}
    async with clean_database.transaction() as session:
        sealed = await repository.seal_in_transaction(session, run, first)
    async with clean_database.transaction() as session:
        assert await repository.seal_in_transaction(session, run, {**first, "coverage": "COMPLETE"}) == sealed
    winner, row = await repository.admit(**kwargs)
    assert not winner and row.sealed_response == sealed and row.sealed_digest == catalog_digest(first)
    with pytest.raises(PersistenceError, match="DATABASE_OPERATION_FAILED"):
        async with clean_database.transaction() as session:
            await session.execute(text("UPDATE runtime_agent_evaluation_receipts SET sealed_response = '{}' WHERE remote_run_id = :run"), {"run": run})
    assert (await repository.read(run)).sealed_response == sealed
    from core.runtime.run_control import DurableRunControlService
    existing_run = str(uuid4())
    control = DurableRunControlService(clean_database)
    lease = await control.claim(existing_run, "existing-public-owner")
    with pytest.raises(ValueError, match="QUALITY_REQUEST_CONFLICT"):
        await repository.admit(**{**kwargs, "correlation": {**correlation, "run_id": existing_run,
            "attempt_id": existing_run, "request_id": str(uuid4())}})
    await control.release(lease)


@pytest.mark.asyncio
async def test_quality_real_python_http_and_go_consumer(clean_database, tmp_path):
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from core.agent_platform.application import AgentApplicationService
    from core.agent_platform.agent_quality_evaluation import register_agent_quality_evaluation
    from core.agent_platform.quality_configuration import runtime_build_digest
    from core.chat_service import ChatService
    from core.persistence.models import UserRow
    from core.persistence.memory import PostgresMemoryManager, PostgresMemoryManagerBridge
    from core.persistence.sync_bridge import SyncPersistenceBridge
    from core.runtime.agent_registry import AgentRegistry
    from core.runtime.agent_loop.context import pinned_loop_config, _pin_value
    from core.runtime.execution_aggregate import canonical_payload_digest
    from core.runtime.model_invocation import ModelAdapterResponse
    from core.runtime.generation_evidence import FinalAnswerEvidenceV1
    from core.runtime.memory_scope import MemoryAccessContext, MemoryContextError
    from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
    from tests.test_scoped_memory_lifecycle import actor, candidate
    from tests.test_agent_loop_runtime import assembly, native, DecisionProvider
    database = clean_database
    tenant = "00000000-0000-0000-0000-000000000001"
    uid, project = uuid4(), str(uuid4())
    async with database.transaction() as session:
        session.add(UserRow(id=uid, subject=str(uid), tenant_id=tenant, principal_kind="SERVICE"))
    now = datetime.now(UTC)
    principal = Principal(uid, str(uid), frozenset({"SERVICE"}), "public-token", now,
        now + timedelta(minutes=5), "SERVICE", frozenset({EVALUATION_EXECUTE_SCOPE}), tenant)
    lifecycle = MemoryLifecycleRepository(database)
    owner = await actor(database)
    project_scope = await lifecycle.configure_project(tenant_id=tenant, project_id=project, agent_id="core_router",
        grants={owner.user_id: ["READ", "WRITE", "PROMOTE", "DELETE", "REVOKE"], str(uid): ["READ"]}, ttl_seconds=3600)
    for key, value in (("delivery.allowed", "ZX9"), ("delivery.forbidden", "ZX8")):
        operation, _, _ = await candidate(lifecycle, owner, key=key, text="public delivery_code=" + value)
        await lifecycle.commit_candidates(owner, operation.operation_id)
        rows = await lifecycle.retrieve(owner, [owner.scope_id("USER")])
        source = next(r for r in rows if "delivery_code=" + value in r["text"])
        promoted = await lifecycle.promote(owner, source["memory_id"], project_scope, expected_version=1, command_id=key)
        if value == "ZX8":
            await lifecycle.mutate(owner, project_scope, promoted["record_id"], expected_version=1, command_id="revoke-forbidden", action="REVOKE")
    service_access = MemoryAccessContext.from_principal(principal, agent_id="core_router", session_id="private-denial-check")
    with pytest.raises(MemoryContextError, match="MEMORY_PRIVATE_SCOPE_DENIED"):
        await lifecycle.private_scopes(service_access)
    assert len(await lifecycle.retrieve(service_access, [project_scope])) == 1
    def first(messages):
        material = json.dumps(messages, ensure_ascii=False)
        assert "delivery_code=ZX9" in material and "delivery_code=ZX8" not in material
        return native("loop_a", "public-a")
    memory = PostgresMemoryManagerBridge(PostgresMemoryManager(database), SyncPersistenceBridge(asyncio.get_running_loop()))
    class ContextDecisionProvider(DecisionProvider):
        def invoke_request(self, messages, *, max_tokens, generation_options, run_context):
            return self.invoke(messages, max_tokens=max_tokens)
    provider = ContextDecisionProvider([first, native("loop_b", "public-b"),
        ModelAdapterResponse("public quality final", finish_reason="stop", tool_call_count=0)])
    factory, repository, model, tools = await assembly(database, tmp_path,
        [], memory_manager=memory, context_policy={"version": 1}, context_model=provider)
    registrations = [factory._agent_registry.resolve(name) for name in factory._agent_registry.agent_ids]
    registry = AgentRegistry(replace(r, definition=replace(r.definition,
        business_options={**r.definition.business_options, "execution_policy": "durable-agent-loop.v1"}))
        if r.agent_id == "core_router" else r for r in registrations)
    factory._agent_registry = registry
    router = factory._router
    router.agent_registry = registry
    definition = registry.resolve("core_router").definition
    query = "public loop_a loop_b synthetic quality"
    candidates = router.tool_discovery.discover_tools(query, "core_router")
    snapshot = router.tool_discovery.create_tool_snapshot(str(uuid4()), candidates, selection_query=query)
    pin = pinned_loop_config(router, "core_router", SimpleNamespace(tool_resolution_snapshot=snapshot,
        execution_policy="durable-agent-loop.v1", memory_effect_policy="SERVICE_PROJECT_READ_ONLY.v1"))
    manifest = {"schema_version": "agent-quality-execution-manifest.v1", "runtime_build_digest": runtime_build_digest(),
        "public_fixture_version": "public-quality-fixture.v1", "memory_effect_policy": "SERVICE_PROJECT_READ_ONLY.v1", "agents": {"core_router": {
            "agent_version": definition.agent_version, "execution_policy": "durable-agent-loop.v1",
            "definition_digest": canonical_payload_digest(_pin_value(definition)), "runtime_config_digest": canonical_payload_digest(pin)}}}
    profile = {"id": "public-quality", "version": 1, "policy": "agent-quality-capture.v1",
        "tool_aliases": {"loop_a": "read_a", "loop_b": "read_b"}, "parameter_paths": {}, "fact_keys": ["delivery_code"]}
    deployment = tmp_path / "managed-quality.json"
    deployment.write_text(json.dumps({"manifest": manifest, "capture_profile": profile}), encoding="utf-8")
    service = AgentApplicationService(ChatService(router, coordinated_runtime_factory=factory),
        SimpleNamespace(require_entry=lambda name: SimpleNamespace(definition=registry.resolve(name).definition, workflow=None)))
    app = FastAPI()
    app.state.loop_execution_repository = repository
    @app.middleware("http")
    async def identity(request, call_next):
        if request.headers.get("Authorization") != "Bearer public-quality-test-token":
            return JSONResponse(status_code=401, content={"code": "AUTH_INVALID_TOKEN"})
        request.state.principal = principal
        return await call_next(request)
    async def bind(request, **kwargs):
        from core.auth import AuthorizationService
        auth = AuthorizationService(database)
        await auth.bind_new(principal, "RUN", kwargs["run_id"])
    async def authorize(request, run_id, action, *, required_scope):
        from core.auth import AuthorizationService
        await AuthorizationService(database).authorize(request.state.principal, "RUN", run_id, action, required_scope=required_scope)
    from core.auth import AuthError
    @app.exception_handler(AuthError)
    async def forbidden(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code})
    observed_codes = []
    def final_capture(*, output, result):
        observed_codes.append(result.error_code)
        if result.status.value != "SUCCEEDED":
            return "FAILED", "FINAL_UNAVAILABLE", None
        answer = FinalAnswerEvidenceV1.from_delivered_output(run_id=result.run_id, content=output)
        return "COMPLETE", None, answer.to_wire_dict()
    register_agent_quality_evaluation(app, settings=SimpleNamespace(quality_evaluation_project_id=project,
        quality_evaluation_service_principal_id=str(uid), quality_evaluation_configuration_path=str(deployment)),
        require_service=lambda: service, bind_run=bind, authorize_run=authorize, final_answer_capture=final_capture)
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        # Go 的配置和请求只含公开输入，Ground Truth 不经 Execute 传给 AgentCore。
        config = {"base_url": f"http://127.0.0.1:{port}", "token_env": "PUBLIC_QUALITY_TEST_TOKEN",
            "trusted_project_id": project, "max_response_bytes": 2*1024*1024, "call_milliseconds": 45000,
            "http": {"connect_milliseconds": 1000, "response_header_milliseconds": 45000,
                "max_idle_connections": 2, "max_idle_connections_per_host": 2, "idle_connection_milliseconds": 1000},
            "execution_manifest": manifest, "capture_profile": profile}
        # HTTPConfig 的稳定字段由 Go 自己补齐，避免 Python 仿造 Consumer 默认值。
        config.pop("http")
        path = tmp_path / "go-quality.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        env = {**os.environ, "AGENT_QUALITY_LIVE_CONFIG": str(path), "PUBLIC_QUALITY_TEST_TOKEN": "public-quality-test-token"}
        proc = await asyncio.create_subprocess_exec("go", "test", "./internal/provider", "-run", "^TestAgentQualityLiveHTTP$", "-count=1",
            cwd=str(Path(__file__).resolve().parents[2] / "AgentEvalOps" / "go-backend"), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output, _ = await proc.communicate()
        assert proc.returncode == 0, output.decode("utf-8", errors="replace") + " runtime_codes=" + repr(observed_codes)
        assert len(model.requests) == 3 and all(len(t.calls) == 1 for t in tools)
        from sqlalchemy import select, func
        from core.persistence.models import RuntimeAgentEvaluationReceiptRow, MessageExchangeRow
        async with database.session() as session:
            receipts = tuple((await session.execute(select(RuntimeAgentEvaluationReceiptRow))).scalars())
            assert len(receipts) == 1
            run_id = receipts[0].remote_run_id
            assert await session.scalar(select(func.count()).select_from(MessageExchangeRow).where(MessageExchangeRow.run_id == run_id)) == 0
        image = await repository.load(run_id)
        assert image.root.status == "SUCCEEDED" and all(m.purpose != "FORMATION" for m in image.models)
        assert image.root.resume_input["agent_loop"]["persist"] is True
        assert image.root.resume_input["agent_loop"]["memory_effect_policy"] == "SERVICE_PROJECT_READ_ONLY.v1"
        effects = json.loads(receipts[0].sealed_response)["bundle"]["observations"]["memory"]["effects"]
        assert effects["conversation"]["status"] == effects["formation"]["status"] == "NOT_ATTEMPTED"
        assert effects["formation"]["reason"] == "DISABLED_BY_POLICY" and effects["formation_operation_count"] == 0
        components = json.loads(receipts[0].sealed_response)["bundle"]["components"]
        assert all(c["availability"] == "AVAILABLE" for c in components)
        assert [c["schema_version"] for c in components] == ["context-evidence.v1", "loop-evidence.v1", "memory-lifecycle-evidence.v1"]
        import httpx
        payload = {k: v for k, v in receipts[0].correlation.items() if k != "query_digest"}
        payload["query"] = query
        duplicate = '{"query":"duplicate",' + json.dumps(payload)[1:]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, headers={"Authorization": "Bearer public-quality-test-token"}) as client:
            assert (await client.get(f"/api/runtime/runs/{run_id}/agent-quality-evidence/v1")).status_code == 200
            assert (await client.post("/api/runtime/evaluation-execute/agent-quality/v1", content=duplicate,
                headers={"Content-Type": "application/json"})).status_code == 422
            from core.persistence.models import ObjectOwnershipRow
            async with database.transaction() as session:
                ownership = await session.get(ObjectOwnershipRow, ("RUN", run_id))
                await session.delete(ownership)
            assert (await client.get(f"/api/runtime/runs/{run_id}/agent-quality-evidence/v1")).status_code in {403,404}
            assert (await client.post("/api/runtime/evaluation-execute/agent-quality/v1", json=payload)).status_code in {403,404}
        assert len(model.requests) == 3
    finally:
        server.should_exit = True
        await serving
