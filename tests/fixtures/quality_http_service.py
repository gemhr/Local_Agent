"""公开质量验收 Producer：真实 Application/PG/HTTP；不读取 Go Ground Truth。"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests.conftest  # 只加载既有未提交的测试环境，不输出凭据。
from tests._pg_fixtures import test_database_url, _alembic
from tests.test_agent_loop_runtime import assembly, native, ObservationTool
from tests.test_agent_batch_handoff import batch, handoff
from tests.test_scoped_memory_lifecycle import actor, candidate
from core.runtime.model_invocation import ModelAdapterResponse
from core.runtime.budget import BudgetUsage
from core.runtime.tool_adapters import ToolAdapterResponse


class PublicTool(ObservationTool):
    def __init__(self, name):
        super().__init__(name)
        self.spec = replace(self.spec, max_output_bytes=32768)

    def llm_input_schema(self):
        return {"type": "object", "properties": {"mode": {"type": "string", "enum": ["LARGE", "SMALL"]}}, "additionalProperties": False}

    def build_invocation(self, argument_text):
        from core.runtime.tool_contract import ToolInvocation
        args = json.loads(argument_text)
        if set(args) - {"mode"} or args.get("mode", "SMALL") not in {"SMALL", "LARGE"}:
            raise ValueError("PUBLIC_ARGUMENTS_INVALID")
        return ToolInvocation.create(tool_name=self.spec.tool_name, arguments=args)

    def invoke_once(self, invocation, context):
        context.raise_if_cancelled()
        self.calls.append(invocation.invocation_id)
        material = "route_code=AB7 " + "public old read-only fact. " * 750 if invocation.arguments.get("mode") == "LARGE" else "public read result"
        return ToolAdapterResponse(material, "text/plain", "public result")


class PublicProvider:
    def __init__(self, variant):
        self.variant, self.requests = variant, []

    def supports_native_tool_calling(self):
        return True

    def invoke_request(self, messages, *, max_tokens, generation_options, run_context):
        if hasattr(self, "audit_directory"):
            with (self.audit_directory / (self.variant + "-models.jsonl")).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"run_id": run_context.run_id, "purpose": run_context.loop_model_purpose}) + "\n")
        self.requests.append((run_context.run_id, run_context.loop_model_purpose))
        purpose = run_context.loop_model_purpose
        iteration = run_context.loop_iteration_no
        if purpose == "COMPACTION":
            data = json.loads(messages[-1]["content"])
            value = "AB8" if self.variant == "regression" else "AB7"
            response = ModelAdapterResponse(json.dumps({"version": 1, "groups": [
                {"source_ref": item["source_ref"], "facts": ["route_code=" + value], "decisions": [], "open_items": []}
                for item in data]}), finish_reason="stop", tool_call_count=0)
        elif getattr(run_context, "delegation_id", None):
            response = native("loop_a", "child-read") if iteration == 1 else ModelAdapterResponse(
                "public wrong child" if self.variant == "regression" else "public child result", finish_reason="stop", tool_call_count=0)
        else:
            text = "\n".join(m.get("content", "") for m in messages if isinstance(m.get("content"), str))
            if "case=compaction" in text:
                response = (native("loop_a", "large-read", {"mode": "LARGE"}) if iteration == 1 else
                    native("loop_b", "small-read-b") if iteration == 2 else native("loop_a", "small-read-a") if iteration == 3 else
                    ModelAdapterResponse("public compaction final", finish_reason="stop", tool_call_count=0))
            elif "case=handoff" in text:
                response = handoff() if iteration == 1 else ModelAdapterResponse("public parent result", finish_reason="stop", tool_call_count=0)
            elif "case=batch" in text:
                response = batch("loop_a", "loop_b") if iteration == 1 else ModelAdapterResponse("public batch result", finish_reason="stop", tool_call_count=0)
                if iteration == 1 and self.variant == "regression":
                    from core.runtime.model_invocation import NativeToolCall
                    call = response.native_tool_calls[0]
                    calls = (replace(call, arguments_json='{"mode":"SMALL"}'), *response.native_tool_calls[1:])
                    assistant = {"role": "assistant", "content": None, "tool_calls": [{"id": c.provider_tool_call_id,
                        "type": "function", "function": {"name": c.tool_name, "arguments": c.arguments_json}} for c in calls]}
                    response = replace(response, native_tool_calls=calls, assistant_message=assistant)
            elif "case=memory" in text:
                response = ModelAdapterResponse("delivery_code=ZX9", finish_reason="stop", tool_call_count=0)
            else:
                names = ("loop_b", "loop_a") if self.variant == "regression" else ("loop_a", "loop_b")
                response = native(names[iteration-1], f"multi-{iteration}") if iteration <= 2 else ModelAdapterResponse("public loop result", finish_reason="stop", tool_call_count=0)
        # 受控 Provider 的 usage 是本夹具的公开确定性计数，非远程账单或真实 tokenizer 声明。
        if response.native_tool_call is not None:
            response = replace(response, native_tool_calls=(response.native_tool_call,), native_tool_call=None)
        return replace(response, actual_usage=BudgetUsage(input_tokens=10, output_tokens=2, total_tokens=12))


async def serve(directory, project, drain=False):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    import uvicorn
    from sqlalchemy import select
    from core.agent_platform.application import AgentApplicationService
    from core.agent_platform.agent_quality_evaluation import register_agent_quality_evaluation
    from core.agent_platform.quality_configuration import runtime_build_digest, quality_runtime_config_digest
    from core.auth import Principal, EVALUATION_EXECUTE_SCOPE, AuthorizationService
    from core.chat_service import ChatService
    from core.persistence.database import Database, DatabaseConfig
    from core.persistence.models import UserRow, RuntimeAgentEvaluationReceiptRow
    from core.persistence.memory import PostgresMemoryManager, PostgresMemoryManagerBridge
    from core.persistence.sync_bridge import SyncPersistenceBridge
    from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
    from core.runtime.agent_registry import AgentRegistry
    from core.runtime.agent_loop.context import pinned_loop_config, _pin_value
    from core.runtime.execution_aggregate import canonical_payload_digest
    from core.runtime.generation_evidence import FinalAnswerEvidenceV1
    migration = await asyncio.to_thread(_alembic, "upgrade", "head")
    if migration.returncode:
        raise RuntimeError("QUALITY_TEST_SCHEMA_UNAVAILABLE")
    database = Database(DatabaseConfig(url=test_database_url(), use_null_pool=True))
    owner = await actor(database)
    uid = uuid4()
    async with database.transaction() as session:
        session.add(UserRow(id=uid, subject=str(uid), tenant_id=owner.tenant_id, principal_kind="SERVICE"))
    lifecycle = MemoryLifecycleRepository(database)
    sid = await lifecycle.configure_project(tenant_id=owner.tenant_id, project_id=project, agent_id="core_router",
        grants={owner.user_id: ["READ", "WRITE", "PROMOTE", "REVOKE"], str(uid): ["READ"]}, ttl_seconds=3600)
    refs = {}
    for label, value in (("allowed", "ZX9"), ("forbidden", "ZX8")):
        op, _, _ = await candidate(lifecycle, owner, key="quality." + label, text="public delivery_code=" + value)
        await lifecycle.commit_candidates(owner, op.operation_id)
        row = next(r for r in await lifecycle.retrieve(owner, [owner.scope_id("USER")]) if "delivery_code="+value in r["text"])
        promoted = await lifecycle.promote(owner, row["memory_id"], sid, expected_version=1, command_id="quality." + label)
        refs[label] = canonical_payload_digest(promoted["record_id"])
        if label == "forbidden":
            await lifecycle.mutate(owner, sid, promoted["record_id"], expected_version=1, command_id="quality.revoke", action="REVOKE")
    principal = Principal(uid, str(uid), frozenset({"SERVICE"}), "public-quality", datetime.now(UTC),
        datetime.now(UTC)+timedelta(minutes=30), "SERVICE", frozenset({EVALUATION_EXECUTE_SCOPE}), owner.tenant_id)
    app, configs = FastAPI(), {}
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    memory = PostgresMemoryManagerBridge(PostgresMemoryManager(database), SyncPersistenceBridge(asyncio.get_running_loop()))
    for variant in (("baseline", "drain") if drain else ("baseline", "candidate", "regression", "unknown", "invalid")):
        provider = PublicProvider(variant)
        tools = [PublicTool(n) for n in ("loop_a", "loop_b")]
        if drain:
            from tests._agent_loop_crash_worker import BusinessReceiptTool
            tools = [BusinessReceiptTool(directory, "normal"), PublicTool("loop_b")]
            provider.audit_directory = directory
        factory, repository, _, _ = await assembly(database, directory, [], tools=tools,
            owner="quality-"+variant, memory_manager=memory, context_model=provider,
            context_policy={"version": 2, "trigger_percent": 45, "target_percent": 35}, context_window=40000, delegation_target="reader")
        router = factory._router
        registry = AgentRegistry(replace(r, definition=replace(r.definition, agent_version="quality-"+variant))
            for r in (factory._agent_registry.resolve(n) for n in factory._agent_registry.agent_ids))
        factory._agent_registry = registry; router.agent_registry = registry
        definition = registry.resolve("core_router").definition
        query = "public loop_a loop_b quality"
        snapshot = router.tool_discovery.create_tool_snapshot(str(uuid4()), router.tool_discovery.discover_tools(query, "core_router"), selection_query=query)
        pin = pinned_loop_config(router, "core_router", SimpleNamespace(tool_resolution_snapshot=snapshot,
            execution_policy="durable-agent-loop.v2", memory_effect_policy="SERVICE_PROJECT_READ_ONLY.v1", memory_access_context=None))
        manifest = {"schema_version": "agent-quality-execution-manifest.v1", "runtime_build_digest": runtime_build_digest(),
            "public_fixture_version": "public-quality-fixture.v1", "memory_effect_policy": "SERVICE_PROJECT_READ_ONLY.v1", "agents": {"core_router": {
                "agent_version": definition.agent_version, "execution_policy": "durable-agent-loop.v2", "definition_digest": canonical_payload_digest(_pin_value(definition)),
                "runtime_config_digest": quality_runtime_config_digest(pin)}}}
        profile = {"id": "public-quality", "version": 1, "policy": "agent-quality-capture.v1", "tool_aliases": {"loop_a": "read_a", "loop_b": "read_b"},
            "parameter_paths": {"loop_a": ["mode"]}, "fact_keys": ["delivery_code", "route_code"]}
        path = directory / (variant + ".json")
        path.write_text(json.dumps({"manifest": manifest, "capture_profile": profile}), encoding="utf-8")
        service = AgentApplicationService(ChatService(router, coordinated_runtime_factory=factory),
            SimpleNamespace(require_entry=lambda name, registry=registry: SimpleNamespace(definition=registry.resolve(name).definition, workflow=None)))
        child = FastAPI(); child.state.loop_execution_repository = repository
        def middleware(variant):
            async def authenticate(request, call_next):
                if request.headers.get("Authorization") != "Bearer public-quality-test-token":
                    return JSONResponse(status_code=401, content={"code": "AUTH_INVALID_TOKEN"})
                request.state.principal = principal
                if drain and request.method == "POST":
                    with (directory / (variant + "-posts.jsonl")).open("a", encoding="utf-8") as stream:
                        stream.write("{}\n")
                response = await call_next(request)
                if drain and request.method == "POST" and variant == "drain":
                    async with database.session() as session:
                        receipt = (await session.execute(select(RuntimeAgentEvaluationReceiptRow).where(
                            RuntimeAgentEvaluationReceiptRow.execution_manifest["agents"]["core_router"]["agent_version"].astext == "quality-drain"))).scalar_one()
                    assert receipt.sealed_response is not None
                    import sqlite3
                    with sqlite3.connect(str(directory / "business.sqlite")) as business:
                        business_receipts = business.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
                    (directory / "response-held.json").write_text(json.dumps({"run_id": receipt.remote_run_id,
                        "sealed_digest": receipt.sealed_digest, "business_receipts": business_receipts}), encoding="utf-8")
                    while not (directory / "release-response").exists():
                        await asyncio.sleep(0.01)
                if request.method == "POST" and variant == "unknown":
                    # 模拟响应丢失：真实 Producer 已完成并封存，但消费者只看到202。
                    async with database.session() as session:
                        receipt = (await session.execute(select(RuntimeAgentEvaluationReceiptRow).where(
                            RuntimeAgentEvaluationReceiptRow.execution_manifest["agents"]["core_router"]["agent_version"].astext == "quality-unknown")
                            .order_by(RuntimeAgentEvaluationReceiptRow.received_at.desc()))).scalars().first()
                    return JSONResponse(status_code=202, content={"protocol_version": "localagent-agent-quality-evaluation-execute.v1",
                        "run_id": receipt.remote_run_id, "status": "ADMITTED", "capture_status": "NOT_READY"})
                if request.method == "POST" and variant == "invalid":
                    return JSONResponse(status_code=200, content={"protocol_version": "unsupported-quality.v99"})
                return response
            return authenticate
        child.middleware("http")(middleware(variant))
        async def bind(request, **kwargs):
            await AuthorizationService(database).bind_new(principal, "RUN", kwargs["run_id"])
        async def authorize(request, run_id, action, *, required_scope):
            await AuthorizationService(database).authorize(request.state.principal, "RUN", run_id, action, required_scope=required_scope)
        def final_capture(*, output, result):
            if result.status.value != "SUCCEEDED":
                return "FAILED", "FINAL_UNAVAILABLE", None
            return "COMPLETE", None, FinalAnswerEvidenceV1.from_delivered_output(run_id=result.run_id, content=output).to_wire_dict()
        register_agent_quality_evaluation(child, settings=SimpleNamespace(quality_evaluation_project_id=project,
            quality_evaluation_service_principal_id=str(uid), quality_evaluation_configuration_path=str(path)),
            require_service=lambda service=service: service, bind_run=bind, authorize_run=authorize, final_answer_capture=final_capture)
        app.mount("/" + variant, child)
        configs[variant] = {"base_url": f"http://127.0.0.1:{port}/{variant}", "token_env": "PUBLIC_QUALITY_TEST_TOKEN", "trusted_project_id": project,
            "max_response_bytes": 2*1024*1024, "call_milliseconds": 90000, "execution_manifest": manifest, "capture_profile": profile}
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    (directory / "ready.json").write_text(json.dumps({"configs": configs, "memory_refs": refs}), encoding="utf-8")
    try:
        await task
    finally:
        await database.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--drain", action="store_true")
    args = parser.parse_args()
    asyncio.run(serve(args.directory, args.project_id, args.drain))
