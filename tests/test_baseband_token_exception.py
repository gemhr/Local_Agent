"""本地例外的真实 Root 写入、摘要伪造拒绝与 Profile/预算一致性。"""
import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace as NS
from uuid import uuid4
import pytest
from tests.test_ci_incident_diagnosis import user
from tests.test_agent_loop_runtime import assembly
from core.auth import AuthorizationService
from core.incidents.service import IncidentService
from core.agent_platform.baseband_investigator import definition
from core.agent_platform.registry import AgentRegistrationBundle
from core.agent_platform.contracts import AgentRegistration
from core.agent_platform.ci_diagnosis_tools import CIDiagnosisTool, TOOL_NAMES
from core.persistence.sync_bridge import SyncPersistenceBridge
from core.persistence.memory import PostgresMemoryManager, PostgresMemoryManagerBridge
from core.runtime.model_selection import ModelProfile, ModelProfileId, ModelCostProfile
from core.runtime.memory_scope import MemoryAccessContext
from core.runtime.model_context import assess_request
from core.runtime.execution_aggregate import ExecutionRootInput, canonical_payload_digest as digest
from core.runtime.agent_loop.context import pinned_loop_config
from core.runtime.request_counting_admission import context_record, context_eligible, persisted_eligible, reference


async def prepared(database, tmp_path, *, wrong_profile=False):
    actor = await user(database)
    service = IncidentService(database)
    instance, _ = await service.create_scenario(actor, "baseband_input_mismatch", str(uuid4()))
    execution, incident, _ = await service.execute(actor, instance.instance_id, str(uuid4()))
    row, _ = await service.admit(actor, incident.incident_id, str(uuid4()))
    agent = definition()
    access = MemoryAccessContext.from_principal(actor, agent.agent_id, row.session_id)
    auth = AuthorizationService(database)
    await auth.bind_new(actor, "RUN", row.run_id)
    await auth.bind_new(actor, "CONVERSATION", access.conversation_id)
    bridge = SyncPersistenceBridge(asyncio.get_running_loop())
    tools = [CIDiagnosisTool(n, service, bridge) for n in TOOL_NAMES]
    bundle = AgentRegistrationBundle(registrations=(AgentRegistration(agent),),
        tool_grants={n:frozenset({agent.agent_id}) for n in TOOL_NAMES})
    profile = ModelProfile(ModelProfileId.REMOTE_ADVANCED, 1000000, 4096, True, True, True, True, 1, 1,
        ModelCostProfile(ModelProfileId.REMOTE_ADVANCED, True, fixed_call_cost_units=1, estimated_latency_ms=1),
        True, "baseband-admission-test", supports_native_tool_calling=True, provider_kind="deepseek",
        model_identity="other-model" if wrong_profile else "deepseek-flash")
    factory, repo, model, _ = await assembly(database, tmp_path, [], tools=tools, business_bundle=bundle,
        model_profile=profile, max_tokens=4096,
        memory_manager=PostgresMemoryManagerBridge(PostgresMemoryManager(database), bridge))
    query = "请调查该 CI 执行，使用授权工具取证。incident_id=" + incident.incident_id + " execution_id=" + execution.execution_id
    scope = await factory.create_run_scope(agent.agent_id, query, run_id=row.run_id, session_id=row.session_id,
        memory_access_context=access, persist=True, timeout_seconds=30)
    context = scope.run_context
    record = context_record(context)
    config = pinned_loop_config(factory._router, agent.agent_id, context)
    seed = dict(messages=[dict(role="user", content=query)])
    resume = dict(resolved_agent_id=agent.agent_id, agent_version="1.0.0", session_id=row.session_id, user_query=query,
        agent_loop=dict(e2e_test_admission=record, pinned_config=config, context_seed=seed, loop_policy=context.loop_policy.to_dict()))
    root = ExecutionRootInput(row.run_id, resume, factory._router.build_single_agent_plan(agent.agent_id, query),
        context.data.deadline_at, asdict(context.budget_ledger.budget), {}, {}, execution_policy="durable-agent-loop.v2")
    return scope, repo, root, record, profile, model


@pytest.mark.asyncio
async def test_local_repository_requires_active_experiment(clean_database, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_PROFILE", "TEST")
    monkeypatch.setenv("LOCAL_AGENT_STAGE21_BASEBAND_TOKEN_EXCEPTION", "1")
    scope, repo, root, _, _, model = await prepared(clean_database, tmp_path)
    try:
        for environment, enabled in (("TEST", "0"), ("PRODUCTION", "1")):
            monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_PROFILE", environment)
            monkeypatch.setenv("LOCAL_AGENT_STAGE21_BASEBAND_TOKEN_EXCEPTION", enabled)
            with pytest.raises(ValueError, match="E2E_ADMISSION_ROOT_MISMATCH"):
                await repo.initialize(root, lease=scope.run_context.durable_lease)
            assert await repo.load(root.run_id) is None
        assert not model.requests
    finally:
        await scope.close(abort=True)


@pytest.mark.asyncio
async def test_local_repository_rejects_rehashed_business_forgery(clean_database, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_PROFILE", "TEST")
    monkeypatch.setenv("LOCAL_AGENT_STAGE21_BASEBAND_TOKEN_EXCEPTION", "1")
    scope, repo, root, _, _, model = await prepared(clean_database, tmp_path)
    try:
        for changed in ("user_id", "root_query_digest", "input_digest"):
            resume = deepcopy(root.resume_input)
            record = resume["agent_loop"]["e2e_test_admission"]
            if changed == "root_query_digest":
                resume["user_query"] += " unrelated request"
                record["authorization"][changed] = digest(resume["user_query"])
            else:
                record["authorization"][changed] = str(uuid4()) if changed == "user_id" else "0" * 64
            record["admission_digest"] = digest({k:v for k,v in record.items() if k != "admission_digest"})
            with pytest.raises(ValueError, match="BASEBAND_LOCAL_ADMISSION"):
                await repo.initialize(replace(root, resume_input=resume), lease=scope.run_context.durable_lease)
            assert await repo.load(root.run_id) is None
        assert not model.requests
    finally:
        await scope.close(abort=True)


@pytest.mark.asyncio
async def test_local_valid_profile_and_persisted_write_controls(clean_database, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_AGENT_ENVIRONMENT_PROFILE", "TEST")
    monkeypatch.setenv("LOCAL_AGENT_STAGE21_BASEBAND_TOKEN_EXCEPTION", "1")
    wrong, _, _, record, _, model = await prepared(clean_database, tmp_path, wrong_profile=True)
    try:
        budget = wrong.run_context.budget_ledger.budget
        assert record is None and budget.max_total_tokens == 32768 and budget.max_cost_units == 65536
        assert not model.requests
    finally:
        await wrong.close(abort=True)
    scope, repo, root, record, profile, model = await prepared(clean_database, tmp_path)
    try:
        budget = scope.run_context.budget_ledger.budget
        assert budget.max_total_tokens is None and budget.max_cost_units is None
        assert budget.max_model_calls == budget.max_tool_calls == 8 and budget.max_elapsed_seconds == 180
        assert scope.run_context.remaining_seconds() <= 30
        await repo.initialize(root, lease=scope.run_context.durable_lease)
        image = await repo.load(root.run_id)
        assert image is not None
        frame = image.loop_frames[0]
        assessment = assess_request([dict(role="user", content="test")], {}, 4096, profile)
        assert not assessment.reliable and not assessment.admissible
        assert context_eligible(assessment, scope.run_context, [profile], frame=frame, purpose="DECISION")
        assert not context_eligible(replace(assessment, required_window=1000001), scope.run_context, [profile], frame=frame, purpose="DECISION")
        stored = {**asdict(assessment), "e2e_test_admission":reference(record)}
        assert persisted_eligible(stored, image.root, frame)
        monkeypatch.setenv("LOCAL_AGENT_STAGE21_BASEBAND_TOKEN_EXCEPTION", "0")
        assert not persisted_eligible(stored, image.root, frame)
        assert persisted_eligible(stored, image.root, frame, for_write=False)
        with pytest.raises(ValueError, match="BASEBAND_LOCAL_MODEL_ADMISSION_INVALID"):
            await repo.start_model_attempt(scope.run_context.durable_lease, step_id=root.plan.steps[0].step_id,
                attempt=1, model_attempt=1, request_digest="0"*64, provider_kind="deepseek", profile_identity="deepseek-flash")
        assert not model.requests
    finally:
        await scope.close(abort=True)
