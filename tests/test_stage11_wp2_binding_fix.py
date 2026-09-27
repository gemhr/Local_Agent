from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest

import server
from core.agent_platform.business_tool_adapter import compile_business_tool_registration
from core.agent_platform.contracts import AgentDefinition, AgentRegistration, BusinessToolDefinition
from core.agent_platform.registry import AgentRegistrationBundle, compile_agent_catalog
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.execution_aggregate import plan_payload
from core.runtime.plan_fingerprint import PlanFingerprinter
from core.runtime.runtime_factory import CoordinatedRuntimeFactory
from core.runtime.tool_discovery import ToolCatalog, ToolDiscovery
from core.runtime.tool_governance import (
    ToolGovernanceContext, ToolGovernanceOutcome, ToolPolicy, builtin_tool_permission_seed,
)
from core.runtime.tool_registry import ToolDescriptor, ToolRegistration
from mcp.adapter import McpBackedToolAdapter
from tests._recovery_fixtures import recovery_plan
from tests._runtime_assembly_fixtures import FakeRouter, make_services


def handler_a(arguments, context):
    return "A"


def handler_b(arguments, context):
    return "B"


def binding_catalog(*, handler=handler_a, schema_type="string", profile="default", permitted=True):
    tool = BusinessToolDefinition(
        name="binding_lookup", description="Lookup", input_schema={"type": "object", "properties": {
            "query": {"type": schema_type}}}, handler=handler,
        granted_agent_ids=frozenset({"binding_worker"}), side_effect_kind="READ_ONLY", idempotency="IDEMPOTENT",
    )
    definitions = (
        AgentDefinition(agent_id="binding_entry", agent_version="1", display_name="Entry", role="entry", instructions="Answer."),
        AgentDefinition(agent_id="binding_worker", agent_version="1", display_name="Worker", role="worker",
                        instructions="Answer.", delegation_allowed=True, model_profile_id=profile,
                        allowed_tools=frozenset({tool.name}) if permitted else frozenset()),
    )
    return compile_agent_catalog(AgentRegistrationBundle(
        registrations=tuple(AgentRegistration(item) for item in definitions), tools=(tool,),
        tool_grants={tool.name: frozenset({"binding_worker"})},
    ), actual_model_profile_ids=frozenset({"default", "alternate"}),
        builtin_registrations=tuple(DEFAULT_AGENT_REGISTRY.resolve(name)
                                    for name in DEFAULT_AGENT_REGISTRY.agent_ids)).agent_registry


def test_handler_binding_identity_is_stable_and_unsupported_callables_fail_closed():
    first = binding_catalog().resolve("binding_worker").toolset_identity
    assert first == binding_catalog().resolve("binding_worker").toolset_identity
    assert first != binding_catalog(handler=handler_b).resolve("binding_worker").toolset_identity
    definition = BusinessToolDefinition(name="callable_lookup", description="Lookup", input_schema={"type": "object"},
                                       handler=partial(handler_a), side_effect_kind="READ_ONLY")
    with pytest.raises(ValueError, match="handler_binding_id"):
        compile_business_tool_registration(definition)
    explicit = compile_business_tool_registration(replace(definition, handler_binding_id="tests.callable_lookup.v1"))
    assert explicit.adapter.handler_binding_id == "tests.callable_lookup.v1"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"handler": handler_b}, {"schema_type": "integer"},
                                  {"profile": "alternate"}, {"permitted": False}],
                         ids=["handler", "schema", "provider", "permission"])
async def test_recovery_rejects_performer_binding_drift_with_entry_and_versions_unchanged(change):
    old, current = binding_catalog(), binding_catalog(**change)
    plan = recovery_plan()
    plan = replace(plan, steps=(replace(plan.steps[0], preferred_agent="binding_worker"),))
    now = datetime.now(UTC)
    entry = old.resolve("binding_entry")
    facts = {name: old.resolve(name).binding_identity() for name in ("binding_entry", "binding_worker")}
    root = SimpleNamespace(
        run_id="binding-recovery", plan_payload=plan_payload(plan),
        plan_fingerprint=PlanFingerprinter.fingerprint(plan),
        resume_input={"entry_agent_id": "binding_entry", "resolved_agent_id": "binding_entry",
                      "agent_version": "1", "workflow_id": None, "workflow_version": None,
                      **entry.binding_identity(), "performer_versions": {name: "1" for name in facts},
                      "performer_identities": facts},
        status="ACTIVE", stop_reason=None, final_result_binding=None,
        absolute_deadline=now + timedelta(minutes=5), created_at=now, updated_at=now,
        budget_totals={"max_model_calls": 4}, budget_reserved={}, budget_consumed={},
    )
    assert entry.binding_identity() == current.resolve("binding_entry").binding_identity()
    image = SimpleNamespace(root=root, steps=(), models=())
    scope = await CoordinatedRuntimeFactory(FakeRouter(), make_services(), agent_registry=old).create_rehydrated_run_scope(
        image, lease=(root.run_id, "worker-b"), run_id=root.run_id
    )
    await scope.close()
    with pytest.raises(ValueError, match="Recovery performer binding identity mismatch"):
        await CoordinatedRuntimeFactory(FakeRouter(), make_services(), agent_registry=current).create_rehydrated_run_scope(
            image, lease=(root.run_id, "worker-b"), run_id=root.run_id
        )


def test_offline_mcp_permissions_converge_and_post_compile_policy_cannot_widen():
    from core.runtime.retry import OperationIdempotency
    from core.runtime.tool_contract import ToolExecutionSpec, ToolSideEffectKind

    spec = ToolExecutionSpec(tool_name="binding_mcp_lookup", side_effect_kind=ToolSideEffectKind.NONE,
                            idempotency=OperationIdempotency.READ_ONLY)
    tool = ToolRegistration(ToolDescriptor(spec.tool_name, "Lookup"), McpBackedToolAdapter(
        spec=spec, server_id="binding_server", remote_name="lookup", input_schema={"type": "object"},
        session_resolver=lambda: None, request_timeout_seconds=10,
    ))
    tools = server._populate_tool_registry((tool,), freeze=False)
    definitions = tuple(AgentDefinition(
        agent_id=name, agent_version="1", display_name=name, role="lookup", instructions="Answer.",
        allowed_tools=frozenset({spec.tool_name}) if name == "mcp_yes" else frozenset(),
    ) for name in ("mcp_yes", "mcp_no"))
    policy = ToolPolicy(spec.tool_name, frozenset({"core_router", "mcp_yes", "mcp_no"}))
    seeds = dict(builtin_tool_permission_seed(tools.registered_names, frozenset(DEFAULT_AGENT_REGISTRY.agent_ids)))
    seeds[spec.tool_name] = policy.allowed_agent_ids
    catalog = compile_agent_catalog(AgentRegistrationBundle(
        registrations=tuple(AgentRegistration(item) for item in definitions),
    ), builtin_registrations=tuple(DEFAULT_AGENT_REGISTRY.resolve(name) for name in DEFAULT_AGENT_REGISTRY.agent_ids),
        actual_tool_names=tools.registered_names,
        actual_tool_registrations={item.descriptor.name: item for item in tools.startup_registrations},
        platform_tool_permission_seed=seeds, tool_permission_limits={spec.tool_name: policy.allowed_agent_ids})
    tools.freeze()
    service = server._build_tool_governance(tools, catalog.agent_registry, mcp_policies=(policy,))
    discovery = ToolDiscovery(ToolCatalog(tools), governance=service)
    for name, expected in (("mcp_no", False), ("mcp_yes", True)):
        record = catalog.agent_registry.resolve(name)
        assert (spec.tool_name in record.actual_allowed_tools) is expected
        decision = service.authorize_tool(ToolGovernanceContext(name, "probe", "step"), tool)
        assert (decision.outcome is ToolGovernanceOutcome.ALLOW) is expected
        assert bool(discovery.discover_tools(spec.tool_name, name)) is expected
    # 同一业务 Agent，不编译该 Tool 时摘要不包含 MCP binding/schema 变化。
    changed_tools = {item.descriptor.name: item for item in tools.startup_registrations}
    changed_tools[spec.tool_name] = ToolRegistration(tool.descriptor, McpBackedToolAdapter(
        spec=spec, server_id="different_server", remote_name="lookup", input_schema={"type": "object"},
        session_resolver=lambda: None, request_timeout_seconds=10))
    changed = compile_agent_catalog(AgentRegistrationBundle(registrations=tuple(AgentRegistration(x) for x in definitions)),
        actual_tool_names=tools.registered_names, actual_tool_registrations=changed_tools,
        platform_tool_permission_seed=seeds, tool_permission_limits={spec.tool_name: policy.allowed_agent_ids})
    assert catalog.agent_registry.resolve("mcp_no").toolset_identity == changed.agent_registry.resolve("mcp_no").toolset_identity
    assert catalog.agent_registry.resolve("mcp_yes").toolset_identity != changed.agent_registry.resolve("mcp_yes").toolset_identity
    # 即使有人在内部装配时重新放宽 policy，Authority 仍核最终编译权限。
    from core.runtime.tool_governance import ToolPolicyCatalog, ToolGovernanceService
    widened = ToolPolicyCatalog(tool_registry=tools, agent_registry=catalog.agent_registry)
    widened.register(policy)
    for item in tools.startup_registrations:
        if item.descriptor.name != spec.tool_name:
            widened.register(ToolPolicy(item.descriptor.name, frozenset()))
    widened.freeze()
    guarded = ToolGovernanceService(widened, catalog.agent_registry)
    assert guarded.authorize_tool(ToolGovernanceContext("mcp_no", "probe", "step"), tool).outcome is ToolGovernanceOutcome.DENY
