"""共同查询合同与原 Runtime/真实隔离 PG 的少量定向验证。"""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from core.agent_platform import ci_status as status
from core.agent_platform.application import AgentApplicationService, AgentApplicationError, ExecutionRequest
from core.agent_platform.contracts import AgentRegistration
from core.agent_platform.registry import AgentRegistrationBundle
from core.auth import ObjectAuthorizationService
from core.chat_service import ChatService
from core.incidents.contracts import CIError, VerificationIntent
from core.incidents.verification import create_verification
from core.persistence.sync_bridge import SyncPersistenceBridge
from core.runtime.agent_adapter_factory import AgentExecutionRequest
from core.runtime.invocation_bindings import InvocationRole
from core.runtime.model_invocation import ModelAdapterResponse
from core.runtime.planning import ExecutionKind, TaskCapabilityRequirements
from core.runtime.tool_adapters import ToolAdapterInvocationError
from core.runtime.tool_contract import ToolInvocation
from core.runtime.tool_registry import ToolDescriptor, ToolRegistration
from tests.test_agent_loop_runtime import assembly, native, DecisionProvider
from tests.test_ci_incident_resolution import setup_incident


class QueryProvider(DecisionProvider):
    def invoke_request(self, messages, *, max_tokens, generation_options, run_context):
        return self.invoke(messages, max_tokens=max_tokens)


async def setup_query(database, tmp_path, responses=()):
    incidents, principal, instance, execution, incident = await setup_incident(database)
    tool = status.CIStatusTool(incidents, SyncPersistenceBridge(asyncio.get_running_loop()))
    bundle = AgentRegistrationBundle(registrations=(AgentRegistration(status.definition()),),
        tool_grants={status.TOOL_NAME: frozenset({status.AGENT_ID})})
    factory, repo, model, _ = await assembly(database, tmp_path, responses, business_bundle=bundle, context_policy={},
        context_model=QueryProvider(responses),
        tool_registrations=[ToolRegistration(ToolDescriptor(name=status.TOOL_NAME, description="CI 状态"), tool)])
    from core.runtime.tool_snapshot_store import PostgresToolResolutionSnapshotStore
    factory._router.tool_snapshot_store = PostgresToolResolutionSnapshotStore(database)
    app = AgentApplicationService(ChatService(factory._router, coordinated_runtime_factory=factory), factory._agent_registry)
    query = dict(task_type="CI_EXECUTION_STATUS", incident_id=incident.incident_id, execution_id=execution.execution_id)
    return NS(incidents=incidents, principal=principal, instance=instance, execution=execution, incident=incident,
        tool=tool, factory=factory, repo=repo, model=model, app=app, query=query,
        authorization=ObjectAuthorizationService(database))


@pytest.mark.asyncio
async def test_trusted_routing_and_options_do_not_override(clean_database, tmp_path):
    q = await setup_query(clean_database, tmp_path)
    request = ExecutionRequest(agent_id=status.AGENT_ID, input=json.dumps(q.query), options={"execution_policy": "fixed-plan.v1"})
    with pytest.raises(AgentApplicationError, match="BUSINESS_OPTION_UNKNOWN"):
        q.app._resolve_request(request)
    with pytest.raises(ValueError, match="TRUSTED_BINDING_REQUIRED"):
        await q.factory.create_run_scope(status.AGENT_ID, json.dumps(q.query), persist=False)
    trusted = await status.freeze_route(q.incidents, q.principal, q.query, "FAST_PATH")
    with pytest.raises(AgentApplicationError, match="CI_STATUS_INPUT_INVALID"):
        await q.app.execute(ExecutionRequest(agent_id=status.AGENT_ID,
            input=json.dumps({**q.query, "execution_id": str(uuid4())})), adaptive_route=trusted)
    for mode, policy in status.MODES.items():
        route = await status.freeze_route(q.incidents, q.principal, q.query, mode)
        scope = await q.factory.create_run_scope(status.AGENT_ID, json.dumps(q.query), adaptive_route=route)
        try:
            assert scope.run_context.execution_policy == policy
            assert scope.run_context.adaptive_route == route
            assert len(scope.coordinator.plan.steps) == 1
            assert scope.coordinator.plan.steps[0].execution_kind is ExecutionKind.AGENT
        finally:
            await scope.close(abort=True)
    # 原来没有 adaptive 字段的普通请求仍走原 fixed-plan 默认。
    scope = await q.factory.create_static_run_scope("core_router", "普通请求", persist=False)
    old_run_id = scope.run_id
    try:
        assert scope.run_context.execution_policy == "fixed-plan.v1" and scope.run_context.adaptive_route is None
        await scope.coordinator._initialize_durable_execution()
    finally:
        await scope.close(abort=True)
    assert "adaptive_route" not in (await q.repo.load(old_run_id)).root.resume_input
    lease = await q.factory.services.durable_run_control.claim(old_run_id, "old-query-test")
    image = await q.repo.prepare_recovery(lease)
    scope = await q.factory.create_rehydrated_run_scope(image, lease=lease)
    try:
        assert scope.run_context.execution_policy == "fixed-plan.v1" and scope.run_context.adaptive_route is None
    finally:
        await scope.close(abort=True)


@pytest.mark.asyncio
async def test_common_scope_original_verification_and_untrusted_arguments(clean_database, tmp_path):
    q = await setup_query(clean_database, tmp_path)
    assert (await status.freeze_route(q.incidents, q.principal, q.query, "FAST_PATH"))["query"] == q.query
    batch = await create_verification(q.incidents, q.principal, q.incident.incident_id,
        VerificationIntent(request_id=uuid4(), expected_state_revision=1, mode="SINGLE", case_ids=[q.execution.case_id]))
    linked = batch["items"][0]["execution_id"]
    await status.freeze_route(q.incidents, q.principal, {**q.query, "execution_id": linked}, "AGENT_LOOP")
    unrelated, _, _ = await q.incidents.execute(q.principal, q.instance.instance_id, str(uuid4()))
    with pytest.raises(CIError, match="CI_NOT_FOUND"):
        await status.freeze_route(q.incidents, q.principal, {**q.query, "execution_id": unrelated.execution_id}, "FAST_PATH")
    with pytest.raises(CIError, match="CI_HUMAN_REQUIRED"):
        from core.runtime.memory_scope import MemoryAccessContext
        await status.freeze_route(q.incidents, MemoryAccessContext.from_principal(q.principal, status.AGENT_ID), q.query, "FAST_PATH")
    with pytest.raises(ToolAdapterInvocationError):
        q.tool.build_invocation(json.dumps({"execution_id": unrelated.execution_id}))
    assert q.tool.spec.side_effect_kind.value == "NONE" and q.tool.spec.idempotency.value == "READ_ONLY"
    route = await status.freeze_route(q.incidents, q.principal, q.query, "FAST_PATH")
    scope = await q.factory.create_run_scope(status.AGENT_ID, json.dumps(q.query), adaptive_route=route)
    await q.authorization.bind_new(q.principal, "RUN", scope.run_id)
    try:
        assert (await status.query_bound_status(q.incidents, scope.run_context))["summary"] == q.execution.summary
        from datetime import datetime, timezone
        from core.persistence.models import UserRow
        async with clean_database.transaction() as session:
            current = await session.get(UserRow, q.principal.user_id)
            current.disabled_at = datetime.now(timezone.utc)
        with pytest.raises(CIError, match="CI_STATUS_SCOPE_DENIED"):
            await status.query_bound_status(q.incidents, scope.run_context)
    finally:
        await scope.close(abort=True)


@pytest.mark.asyncio
async def test_fast_path_zero_model_governed_tool_and_delivery(clean_database, tmp_path, monkeypatch):
    q = await setup_query(clean_database, tmp_path)
    service = status.CIStatusService(q.incidents, q.app, q.authorization)
    result = await service.execute(q.principal, q.incident.incident_id, q.execution.execution_id)
    assert result.status.value == "SUCCEEDED", (result.error_code, result.safe_message)
    assert result.business_output_valid is True
    output = json.loads(result.output)
    assert output["summary"] == q.execution.summary and output["state_revision"] == q.execution.state_revision
    assert not q.model.requests
    image = await q.repo.load(result.run_id)
    assert image.root.execution_policy == "fixed-plan.v1" and image.root.status == "SUCCEEDED"
    assert not image.loop_frames and not image.models
    assert len(image.tool_invocations) == 1 and image.tool_invocations[0].state == "COMMITTED"
    feed = await q.factory.services.client_event_feed.read_after(result.run_id, 0, 200)
    terminals = [e for e in feed if e.event_type == "run.completed"]
    assert len(terminals) == 1 and terminals[0].payload["business_output_valid"] is True
    assert json.loads(terminals[0].payload["output"]) == output
    assert q.factory._agent_registry.resolve(status.AGENT_ID).execution_adapter_id == status.ADAPTER_ID
    from core.runtime.tool_governance import ToolGovernanceError, ToolGovernanceErrorCode
    governance = q.factory._router.tool_governance_service
    original_authorize = type(governance).authorize_tool
    denied_steps = []
    def deny(self, context, registration):
        if context.step_id == "answer":
            denied_steps.append(context.step_id)
            raise ToolGovernanceError(ToolGovernanceErrorCode.PERMISSION_DENIED, "拒绝测试")
        return original_authorize(self, context, registration)
    monkeypatch.setattr(type(governance), "authorize_tool", deny)
    denied = await service.execute(q.principal, q.incident.incident_id, q.execution.execution_id)
    assert denied.status.value == "FAILED" and denied.output is None
    assert not (await q.repo.load(denied.run_id)).tool_invocations
    assert denied_steps == ["answer"]


@pytest.mark.asyncio
async def test_loop_baseline_uses_same_tool_and_schema_without_forced_rounds(clean_database, tmp_path):
    q = await setup_query(clean_database, tmp_path)
    expected = dict(schema_version="ci-status.v1", kind="STATUS", incident_id=q.incident.incident_id,
        execution_id=q.execution.execution_id, state_revision=q.execution.state_revision, summary=q.execution.summary)
    single = native(status.TOOL_NAME, "status-call")
    batch = replace(single, native_tool_call=None, native_tool_calls=(single.native_tool_call,))
    q.model.responses[:] = [batch, ModelAdapterResponse(json.dumps(expected), finish_reason="stop", tool_call_count=0)]
    result = await status.CIStatusService(q.incidents, q.app, q.authorization, mode="AGENT_LOOP").execute(
        q.principal, q.incident.incident_id, q.execution.execution_id)
    assert result.status.value == "SUCCEEDED", (result.error_code, result.safe_message)
    assert result.business_output_valid and json.loads(result.output) == expected
    assert len(q.model.requests) == 2
    image = await q.repo.load(result.run_id)
    assert image.root.execution_policy == "durable-agent-loop.v2" and len(image.loop_frames) == 1
    assert len(image.tool_invocations) == 1 and image.tool_invocations[0].tool_name == status.TOOL_NAME
    assert image.tool_invocations[0].state == "COMMITTED"
    fast = await status.CIStatusService(q.incidents, q.app, q.authorization).execute(
        q.principal, q.incident.incident_id, q.execution.execution_id)
    assert fast.status.value == "SUCCEEDED" and json.loads(fast.output) == json.loads(result.output)
    assert len(q.model.requests) == 2


@pytest.mark.asyncio
async def test_frozen_recovery_replays_committed_read_and_preserves_deadline(clean_database, tmp_path, monkeypatch):
    q = await setup_query(clean_database, tmp_path)
    route = await status.freeze_route(q.incidents, q.principal, q.query, "FAST_PATH")
    scope = await q.factory.create_run_scope(status.AGENT_ID, json.dumps(q.query), adaptive_route=route, timeout_seconds=60)
    run_id = scope.run_id
    await q.authorization.bind_new(q.principal, "RUN", run_id)
    request = AgentExecutionRequest(step_id="answer", agent_id=status.AGENT_ID, instruction=json.dumps(q.query),
        invocation_role=InvocationRole.ENTRY, execution_kind=ExecutionKind.AGENT, input_type="text",
        capability_requirements=TaskCapabilityRequirements(), event_emitter=scope.coordinator.event_emitter.for_step("answer"),
        business_definition=status.definition())
    # 在合法 root 初始化后执行真实 Tool，停在 StepResult 提交之前；不改写持久状态。
    try:
        await scope.coordinator._initialize_durable_execution()
        answer = await asyncio.to_thread(status.CIStatusAdapter(q.factory._router).execute, request, scope.run_context)
        image = await q.repo.load(run_id)
        assert image.tool_invocations[0].state == "COMMITTED"
        deadline = image.root.absolute_deadline
    finally:
        await scope.close(abort=True)
    def no_read(*args, **kwargs):
        raise AssertionError("COMMITTED 查询不得再次读取")
    monkeypatch.setattr(q.tool, "invoke_once", no_read)
    monkeypatch.setattr(status, "freeze_route", no_read)
    lease = await q.factory.services.durable_run_control.claim(run_id, "query-recovery")
    recovered = await q.repo.prepare_recovery(lease)
    scope = await q.factory.create_rehydrated_run_scope(recovered, lease=lease)
    try:
        assert scope.run_context.adaptive_route == route
        assert scope.run_context.data.deadline_at == deadline
        result = await scope.execute()
        assert result.status.value == "SUCCEEDED", result
    finally:
        await scope.close(abort=True)
    image = await q.repo.load(run_id)
    assert len(image.tool_invocations) == 1 and image.tool_invocations[0].committed_result["output"]["content"] == answer.content
    assert not q.model.requests and not image.models
    # 未确认窗口使用原 STARTED/UNKNOWN 合同，不能伪造 COMMITTED 或重新执行。
    scope = await q.factory.create_run_scope(status.AGENT_ID, json.dumps(q.query), adaptive_route=route)
    await q.authorization.bind_new(q.principal, "RUN", scope.run_id)
    try:
        await scope.coordinator._initialize_durable_execution()
        from core.runtime.tool_execution import ToolExecutionFailed
        from core.runtime.tool_contract import canonical_json_digest
        from uuid import NAMESPACE_URL, uuid5
        invocation = ToolInvocation.create(tool_name=status.TOOL_NAME, arguments={}, invocation_id=str(uuid5(
            NAMESPACE_URL, f"ci-status:{scope.run_id}:answer:{canonical_json_digest(q.query)}")))
        durable = q.factory.services.durable_tool_invocation
        await durable.prepare(lease=scope.run_context.durable_lease, step_id="answer", invocation=invocation, tool_name=status.TOOL_NAME)
        await durable.start(lease=scope.run_context.durable_lease, invocation_id=invocation.invocation_id)
        await durable.unknown(lease=scope.run_context.durable_lease, invocation_id=invocation.invocation_id, reason="UNCONFIRMED_READ")
        with pytest.raises(ToolExecutionFailed):
            await asyncio.to_thread(status.CIStatusAdapter(q.factory._router).execute,
                request, scope.run_context)
        unknown = await q.repo.load(scope.run_id)
        assert unknown.tool_invocations[0].state == "UNKNOWN" and unknown.tool_invocations[0].committed_result is None
    finally:
        await scope.close(abort=True)
