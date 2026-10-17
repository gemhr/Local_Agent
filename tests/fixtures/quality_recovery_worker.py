"""共享 exit85 的质量 Receipt/PROJECT Memory 恢复进程。"""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests.conftest


async def run(phase, dsn, directory):
    import httpx
    from fastapi import FastAPI
    from core.auth import Principal, EVALUATION_EXECUTE_SCOPE, AuthorizationService
    from core.agent_platform.application import AgentApplicationService
    from core.agent_platform.agent_quality_evaluation import register_agent_quality_evaluation
    from core.agent_platform.quality_configuration import runtime_build_digest, quality_runtime_config_digest
    from core.chat_service import ChatService
    from core.persistence.database import Database, DatabaseConfig
    from core.persistence.memory import PostgresMemoryManager, PostgresMemoryManagerBridge
    from core.persistence.sync_bridge import SyncPersistenceBridge
    from core.persistence.repositories.execution import DurableExecutionRepository
    from core.runtime.run_control import DurableRunControlService
    from core.runtime.recovery_coordinator import RecoveryCoordinator
    from core.runtime.agent_loop.context import pinned_loop_config, _pin_value
    from core.runtime.execution_aggregate import canonical_payload_digest
    from core.runtime.agent_loop.quality_contract import catalog_digest
    from core.runtime.generation_evidence import FinalAnswerEvidenceV1
    from core.runtime.budget import BudgetUsage
    from core.runtime.model_invocation import ModelAdapterResponse
    from tests.test_agent_loop_runtime import assembly
    from tests.test_agent_batch_handoff import Provider, batch, handoff
    from tests._agent_loop_crash_worker import BusinessReceiptTool
    from tests.fixtures.quality_http_service import PublicTool
    directory = Path(directory)
    info = json.loads((directory / "identity.json").read_text(encoding="utf-8"))
    database = Database(DatabaseConfig(url=dsn, use_null_pool=True))
    control = DurableRunControlService(database, lease_seconds=1)
    class Boundary(DurableExecutionRepository):
        async def commit_child_result(self, *args, **kwargs):
            result = await super().commit_child_result(*args, **kwargs)
            if phase == "start":
                os._exit(85)
            return result
    repository = Boundary(database, control)
    responses = [handoff(), batch("loop_b", "loop_a"),
        ModelAdapterResponse("public child final", finish_reason="stop", tool_call_count=0),
        ModelAdapterResponse("public parent final", finish_reason="stop", tool_call_count=0)]
    class Script(Provider):
        def invoke_request(self, messages, **kwargs):
            log = directory / "provider.jsonl"
            count = len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0
            with log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"ordinal": count, "purpose": kwargs["run_context"].loop_model_purpose}) + "\n")
            assert count < len(responses), "unexpected provider redispatch"
            return replace(responses[count], actual_usage=BudgetUsage(input_tokens=10, output_tokens=2, total_tokens=12))
    memory = PostgresMemoryManagerBridge(PostgresMemoryManager(database), SyncPersistenceBridge(asyncio.get_running_loop()))
    factory, _, _, _ = await assembly(database, directory, [], context_model=Script([]),
        context_policy={"version": 2}, delegation_target="reader", repository=repository, control=control,
        tools=[BusinessReceiptTool(directory, "normal"), PublicTool("loop_b")], memory_manager=memory, owner="quality-recovery")
    router = factory._router
    definition = factory._agent_registry.resolve("core_router").definition
    query = "public loop_a loop_b handoff reader"
    tool_snapshot = router.tool_discovery.create_tool_snapshot(str(uuid4()), router.tool_discovery.discover_tools(query, "core_router"), selection_query=query)
    pin = pinned_loop_config(router, "core_router", SimpleNamespace(tool_resolution_snapshot=tool_snapshot,
        execution_policy="durable-agent-loop.v2", memory_effect_policy="SERVICE_PROJECT_READ_ONLY.v1", memory_access_context=None))
    manifest = {"schema_version": "agent-quality-execution-manifest.v1", "runtime_build_digest": runtime_build_digest(),
        "public_fixture_version": "public-recovery.v1", "memory_effect_policy": "SERVICE_PROJECT_READ_ONLY.v1",
        "agents": {"core_router": {"agent_version": definition.agent_version, "execution_policy": "durable-agent-loop.v2",
            "definition_digest": canonical_payload_digest(_pin_value(definition)), "runtime_config_digest": quality_runtime_config_digest(pin)}}}
    profile = {"id": "recovery", "version": 1, "policy": "agent-quality-capture.v1", "tool_aliases": {"loop_a": "write_a", "loop_b": "read_b"},
        "parameter_paths": {}, "fact_keys": ["delivery_code"]}
    path = directory / "managed.json"
    path.write_text(json.dumps({"manifest": manifest, "capture_profile": profile}), encoding="utf-8")
    now = datetime.now(UTC)
    principal = Principal(UUID(info["user_id"]), info["user_id"], frozenset({"SERVICE"}), "public-recovery", now,
        now + timedelta(minutes=5), "SERVICE", frozenset({EVALUATION_EXECUTE_SCOPE}), info["tenant_id"])
    service = AgentApplicationService(ChatService(router, coordinated_runtime_factory=factory),
        SimpleNamespace(require_entry=lambda name: SimpleNamespace(definition=factory._agent_registry.resolve(name).definition, workflow=None)))
    app = FastAPI(); app.state.loop_execution_repository = repository
    @app.middleware("http")
    async def identity(request, call_next):
        request.state.principal = principal
        return await call_next(request)
    async def bind(request, **kwargs):
        await AuthorizationService(database).bind_new(principal, "RUN", kwargs["run_id"])
    async def authorize(request, run_id, action, *, required_scope):
        await AuthorizationService(database).authorize(principal, "RUN", run_id, action, required_scope=required_scope)
    def final_capture(*, output, result):
        return ("COMPLETE", None, FinalAnswerEvidenceV1.from_delivered_output(run_id=result.run_id, content=output).to_wire_dict())
    register_agent_quality_evaluation(app, settings=SimpleNamespace(quality_evaluation_project_id=info["project_id"],
        quality_evaluation_service_principal_id=info["user_id"], quality_evaluation_configuration_path=str(path)),
        require_service=lambda: service, bind_run=bind, authorize_run=authorize, final_answer_capture=final_capture)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False) as client:
        if phase == "start":
            rid = info["run_id"]
            payload = {"agent_id": "core_router", "query": query, "run_id": rid, "attempt_id": rid,
                "request_id": str(uuid4()), "evaluation_project_id": info["project_id"], "evaluation_run_id": str(uuid4()),
                "case_id": str(uuid4()), "case_version": "v1", "case_digest": "a" * 64,
                "execution_manifest_digest": catalog_digest(manifest), "capture_profile_id": profile["id"],
                "capture_profile_version": 1, "capture_profile_digest": catalog_digest(profile), "timeout_milliseconds": 120000}
            response = await client.post("/api/runtime/evaluation-execute/agent-quality/v1", json=payload)
            raise AssertionError("missing exit85: " + response.text)
        async def recover(rid, lease, image):
            scope = await factory.create_rehydrated_run_scope(image, lease=lease, run_id=rid, persist=True)
            try:
                result = await scope.execute()
                assert result.status.value == ("FAILED" if phase == "revoked" else "SUCCEEDED"), (result.error_code, result.safe_message)
            finally:
                await scope.close(abort=True)
        recovery = RecoveryCoordinator(repository, control, recover)
        assert await recovery.scan_once() == 1
        await asyncio.gather(*tuple(recovery._local_tasks))
        response = await client.get("/api/runtime/runs/" + info["run_id"] + "/agent-quality-evidence/v1")
        assert response.status_code == 200, response.text
        again = await client.get("/api/runtime/runs/" + info["run_id"] + "/agent-quality-evidence/v1")
        assert again.content == response.content
        (directory / "sealed.json").write_bytes(response.content)
    await database.dispose()


if __name__ == "__main__":
    asyncio.run(run(*sys.argv[1:]))
