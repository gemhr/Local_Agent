"""Skill 读取边界、持久 Loop 回填和显式启用的真实 C1。"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
import json
import os
import pytest
from core.skills.catalog import SkillCatalog, freeze_skills, skill_instructions
from core.skills.tool_adapter import SkillTool, TOOL_NAMES
from core.agent_platform.ci_diagnosis import definition
from core.runtime.tool_adapters import ToolAdapterInvocationError

ROOT = Path(__file__).resolve().parents[1]


def test_reader_identity_visibility_and_path(tmp_path):
    import shutil
    shutil.copytree(ROOT / "skills", tmp_path / "skills")
    catalog = SkillCatalog.discover([tmp_path / "skills"])
    pin = freeze_skills(catalog, definition(), TOOL_NAMES)
    assert set(pin) == {"ci-failure-investigation", "ci-environment-check"}
    assert freeze_skills(catalog, definition(), ["load_skill"]) == {}
    context = NS(raise_if_cancelled=lambda: None, run_context=NS(loop_skill_identity=pin, loop_frame_id="frame", loop_skill_agent_id=definition().agent_id))
    load, read = (SkillTool(n, catalog) for n in TOOL_NAMES)
    result = json.loads(load.invoke_once(load.build_invocation('{"name":"ci-failure-investigation"}'), context).content)
    assert catalog.get(result["name"]).body in result["content"]
    args = dict(name="ci-failure-investigation", relative_path="references/failure-evidence-guide.md")
    assert json.loads(read.invoke_once(read.build_invocation(json.dumps(args)), context).content)["content"]
    for invalid in [dict(name="feature-impact-analysis"), dict(name="ci-failure-investigation", relative_path="../../AGENTS.md"), dict(name="ci-failure-investigation", relative_path=str(ROOT / "AGENTS.md"))]:
        tool = read if "relative_path" in invalid else load
        with pytest.raises(ToolAdapterInvocationError):
            tool.invoke_once(tool.build_invocation(json.dumps(invalid)), context)
    path = catalog.get(args["name"]).package_path / args["relative_path"]
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ToolAdapterInvocationError):
        read.invoke_once(read.build_invocation(json.dumps(args)), context)
    prompt = skill_instructions(pin)
    assert "feature-impact-analysis" not in prompt and catalog.get(args["name"]).body not in prompt


async def run_case(database, tmp_path, model, *, real=False):
    from core.agent_platform.registry import AgentRegistrationBundle
    from core.agent_platform.contracts import AgentRegistration
    from core.agent_platform.application import AgentApplicationService, ExecutionRequest
    from core.agent_platform.ci_diagnosis_tools import CIDiagnosisTool, TOOL_NAMES as CI_TOOLS
    from core.agent_platform.ci_diagnosis import validate_report
    from core.chat_service import ChatService
    from core.incidents.service import IncidentService
    from core.persistence.sync_bridge import SyncPersistenceBridge
    from core.persistence.models import ObjectOwnershipRow, RuntimeLoopActionRow
    from core.runtime.memory_scope import MemoryAccessContext
    from core.runtime.model_selection import ModelProfile, ModelProfileId, ModelCostProfile
    from tests.test_agent_loop_runtime import assembly
    from tests.test_ci_incident_diagnosis import user
    from sqlalchemy import select
    from uuid import uuid4
    catalog = SkillCatalog.discover([ROOT / "skills"])
    incident_service = IncidentService(database)
    principal = await user(database)
    instance, _ = await incident_service.create_scenario(principal, "report_pipeline_failure", str(uuid4()))
    execution, incident, _ = await incident_service.execute(principal, instance.instance_id, str(uuid4()))
    investigation, _ = await incident_service.admit(principal, incident.incident_id, str(uuid4()))
    async with database.transaction() as session:
        session.add(ObjectOwnershipRow(object_type="RUN", object_id=investigation.run_id, owner_user_id=principal.user_id, tenant_id=principal.tenant_id))
    bridge = SyncPersistenceBridge(asyncio.get_running_loop())
    tools = [SkillTool(n, catalog) for n in TOOL_NAMES] + [CIDiagnosisTool(n, incident_service, bridge) for n in CI_TOOLS]
    agent = definition()
    agent = replace(agent, business_options={**agent.business_options, "loop_policy": {**agent.business_options["loop_policy"], "total_tokens": 262144, "cost_units": 262144, "max_tool_calls": 10}})
    bundle = AgentRegistrationBundle(registrations=(AgentRegistration(agent),), tool_grants={t.spec.tool_name: frozenset({agent.agent_id}) for t in tools})
    profile = ModelProfile(ModelProfileId.LOCAL_FAST, 1000000, 4096, True, True, True, True, 1, 1,
        ModelCostProfile(ModelProfileId.LOCAL_FAST, real, fixed_call_cost_units=1, estimated_latency_ms=1),
        real, "skills-c1", supports_native_tool_calling=True, provider_kind="deepseek" if real else "synthetic", model_identity=getattr(model, "model_name", "controlled"),
        request_counting_mode="BOUNDED_ESTIMATE", request_counting_version="utf8-byte-upper-bound.v1", request_framing_bound=64,
        request_message_framing_bound=16, output_accounting_contract="bounded-visible-output.v1")
    from core.persistence.memory import PostgresMemoryManager, PostgresMemoryManagerBridge
    factory, repository, _, _ = await assembly(database, tmp_path, [], tools=tools, context_model=model, business_bundle=bundle, model_profile=profile, max_tokens=4096, memory_manager=PostgresMemoryManagerBridge(PostgresMemoryManager(database), bridge))
    router = factory.router if hasattr(factory, "router") else factory._router
    router.skill_catalog = catalog
    application = AgentApplicationService(ChatService(router, coordinated_runtime_factory=factory), router.agent_registry)
    access = MemoryAccessContext.from_principal(principal, agent.agent_id, investigation.session_id)
    events = [event async for event in application.stream(ExecutionRequest(agent_id=agent.agent_id,
        input="请调查该 CI 执行失败，使用授权工具取证。incident_id=" + incident.incident_id + " execution_id=" + incident.execution_id,
        run_id=investigation.run_id, session_id=investigation.session_id, timeout_seconds=180),
        retrieval_cache_authz_domain=principal.authz_domain_id, memory_access_context=access)]
    image = await repository.load(investigation.run_id)
    assert image.root.status == "SUCCEEDED", (image.root.status, [(e.error_code, e.safe_message) for e in events if e.error_code])
    async with database.session() as session:
        actions = (await session.execute(select(RuntimeLoopActionRow).where(RuntimeLoopActionRow.run_id == investigation.run_id))).scalars().all()
    frame = image.loop_frames[0]
    committed = [t for t in image.tool_invocations if t.state == "COMMITTED"]
    assert {t.tool_name for t in committed} >= set(TOOL_NAMES) | set(CI_TOOLS)
    skill = catalog.get("ci-failure-investigation")
    first = json.dumps(model.requests[0], ensure_ascii=False)
    assert "ci-failure-investigation" in first and "ci-environment-check" in first and "feature-impact-analysis" not in first
    assert skill.body not in first
    returned = []
    for request in model.requests[1:]:
        for message in request:
            if message.get("role") == "tool":
                payload = json.loads(message["content"])
                try:
                    returned.append(json.loads(payload["content"]))
                except (KeyError, TypeError, ValueError):
                    pass
    loaded = next(p for p in returned if p.get("relative_path") == "SKILL.md")
    assert loaded["content"] == (skill.package_path / "SKILL.md").read_bytes().decode("utf-8")
    reference = next(p for p in returned if p.get("relative_path") == "references/failure-evidence-guide.md")
    assert reference["content"] == (skill.package_path / reference["relative_path"]).read_bytes().decode("utf-8")
    payloads = [json.loads(t.committed_result["output"]["content"]) for t in committed if t.tool_name in TOOL_NAMES]
    assert any(p["relative_path"] == "references/failure-evidence-guide.md" for p in payloads)
    raw = image.steps[0].typed_result_payload["content"]
    report = validate_report(raw, investigation, incident, execution, image, actions) if real else None
    evidence = dict(run_id=investigation.run_id, frame_id=frame.frame_id, agent_id=agent.agent_id,
        skill_identity=frame.pinned_config["skills"], status=image.root.status,
        invocations=[dict(tool=t.tool_name, invocation_id=t.invocation_id, state=t.state, result_digest=t.committed_result_digest) for t in committed],
        snapshots=[dict(iteration=i.iteration_no, state=i.state, observation_digest=i.observation_digest) for i in image.loop_iterations], report=report,
        request_links=getattr(model, "request_links", []),
        request_count=len(model.requests), model=getattr(model, "model_name", "controlled"))
    (tmp_path / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return evidence


@pytest.mark.asyncio
async def test_committed_skill_and_reference_reach_next_request(clean_database, tmp_path):
    from tests.test_agent_batch_handoff import Provider
    from core.runtime.model_invocation import ModelAdapterResponse, NativeToolCall
    def call(name, args):
        raw = json.dumps(args)
        return ModelAdapterResponse("", native_tool_calls=(NativeToolCall(name, name, raw),), tool_call_count=1, finish_reason="tool_calls",
            assistant_message={"role":"assistant", "content":None,"tool_calls":[{"id":name,"type":"function","function":{"name":name,"arguments":raw}}]})
    model = Provider([call("load_skill", {"name":"ci-failure-investigation"}), call("read_skill_resource", {"name":"ci-failure-investigation","relative_path":"references/failure-evidence-guide.md"}),
        call("ci_get_execution_context", {}), lambda messages: call("ci_read_execution_evidence", {"evidence_id":json.loads(json.loads([m for m in messages if m.get("role")=="tool"][-1]["content"])["content"])["evidence"][0]["evidence_id"]}),
        ModelAdapterResponse("controlled final", finish_reason="stop", tool_call_count=0)])
    await run_case(clean_database, tmp_path, model)


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("LOCAL_AGENT_SKILL_REAL_C1") != "1", reason="仅显式授权运行真实 DeepSeek C1")
async def test_deepseek_real_c1(clean_database, tmp_path):
    from core.llm_engine import RemoteLLMEngine
    from core.runtime.model_invocation import GeneratorModelAdapter
    class Capture(GeneratorModelAdapter):
        def __init__(self, engine):
            super().__init__(engine)
            self.requests = []
            self.request_links = []
            self.model_name = engine.model_name
        async def ainvoke(self, messages, **kwargs):
            self.requests.append(deepcopy(messages))
            from core.runtime.execution_aggregate import canonical_payload_digest
            run = kwargs["run_context"]
            self.request_links.append(dict(frame_id=run.loop_frame_id, iteration=run.loop_iteration_no,
                snapshot_id=run.loop_context_snapshot_id, request_digest=canonical_payload_digest(messages),
                model_attempt_ref=run.loop_model_attempt_ref))
            return await super().ainvoke(messages, **kwargs)
    engine = RemoteLLMEngine("https://api.deepseek.com", os.getenv("LOCAL_AGENT_REMOTE_MODEL_NAME", "deepseek-flash"), api_key=os.environ["LOCAL_AGENT_REMOTE_API_KEY"],
        timeout_seconds=120, verify_tls=True, enable_thinking=False, provider_kind="deepseek")
    try:
        evidence = await run_case(clean_database, tmp_path, Capture(engine), real=True)
        (ROOT / ".ai/handoff/stage18_skill/51_wp2_c1_evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        await engine.aclose()


def test_child_seed_uses_own_visibility_and_shared_structured_contract():
    from core.feature_impact.agent import definition as feature_definition
    from core.runtime.agent_loop.handoff import prepare_handoff
    from core.runtime.agent_loop.contracts import LoopDecisionV2
    from core.runtime.model_invocation import ModelAdapterResponse
    catalog = SkillCatalog.discover([ROOT / "skills"])
    child = feature_definition()
    pin = freeze_skills(catalog, child, TOOL_NAMES)
    frame = NS(frame_role="ROOT", seed_digest="parent", context_seed={"protocol_mode":"structured"}, pinned_config={"delegation_targets":{
        child.agent_id: {"config":{"security_prompt":"子职责", "skills":pin}, "tools":list(TOOL_NAMES)}}})
    decision = NS(target_agent_id=child.agent_id, task="分析变更影响")
    frozen = prepare_handoff(None, frame, decision, None)
    text = frozen["seed"]["messages"][0]["content"]
    assert "feature-impact-analysis" in text and "ci-failure-investigation" not in text
    assert catalog.get("feature-impact-analysis").body not in text
    assert freeze_skills(catalog, child, []) == {}
    response = ModelAdapterResponse(json.dumps({"kind":"TOOL_BATCH", "calls":[{"tool_name":"load_skill", "arguments":{"name":"feature-impact-analysis"}}]}), finish_reason="stop", tool_call_count=0)
    parsed = LoopDecisionV2.parse(response, protocol_mode="structured", synthetic_id="structured")
    invocation = SkillTool("load_skill", catalog).build_invocation(json.dumps(parsed.calls[0]["arguments"]))
    assert invocation.arguments["name"] == "feature-impact-analysis"


def test_reader_policies_preserve_explicit_grants():
    from core.agent_platform.registry import AgentRegistrationBundle, compile_agent_catalog
    from core.agent_platform.contracts import AgentRegistration
    from core.runtime.tool_registry import ToolRegistry
    from core.runtime.tool_governance import ToolPolicyCatalog, ToolGovernanceService, ToolGovernanceContext, ToolGovernanceOutcome
    from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
    from core.skills.tool_adapter import register_tools, register_policies
    catalog = SkillCatalog.discover([ROOT / "skills"])
    registry = ToolRegistry()
    register_tools(registry, catalog)
    registry.freeze()
    agent = replace(definition(), allowed_tools=frozenset(TOOL_NAMES))
    grants = {name:frozenset({agent.agent_id}) for name in TOOL_NAMES}
    compiled = compile_agent_catalog(AgentRegistrationBundle(registrations=(AgentRegistration(agent),), tool_grants=grants),
        builtin_registrations=tuple(DEFAULT_AGENT_REGISTRY.resolve(n) for n in DEFAULT_AGENT_REGISTRY.agent_ids),
        actual_tool_names=registry.registered_names, actual_tool_registrations={n:registry.require(n) for n in registry.registered_names}, platform_tool_permission_seed={})
    policies = ToolPolicyCatalog(tool_registry=registry, agent_registry=compiled.agent_registry)
    register_policies(policies, grants)
    policies.freeze()
    service = ToolGovernanceService(policies, compiled.agent_registry)
    for name in TOOL_NAMES:
        allowed = service.authorize_tool(ToolGovernanceContext(principal_agent_id=agent.agent_id, run_id="run",step_id="step"), registry.require(name))
        denied = service.authorize_tool(ToolGovernanceContext(principal_agent_id="core_router", run_id="run",step_id="step"), registry.require(name))
        assert allowed.outcome is ToolGovernanceOutcome.ALLOW
        assert denied.outcome is not ToolGovernanceOutcome.ALLOW
