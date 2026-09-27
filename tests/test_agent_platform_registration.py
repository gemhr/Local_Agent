import pytest

from core.agent_platform.contracts import (
    AgentDefinition,
    AgentRegistration,
    BusinessToolDefinition,
    ExecutionBinding,
    WorkflowDefinition,
    WorkflowTask,
)
from core.agent_platform.registry import (
    AgentRegistrationBundle,
    AgentRegistrationCompileError,
    compile_agent_catalog,
)
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY


def test_registration_compiles_definition_and_effective_tool_grants() -> None:
    definition = AgentDefinition(
        agent_id="billing_helper",
        agent_version="1.0.0",
        display_name="Billing Helper",
        role="billing questions",
        instructions="Answer billing questions.",
        allowed_tools=frozenset({"lookup_invoice"}),
        model_profile_id="chat",
    )
    tool = BusinessToolDefinition(
        name="lookup_invoice",
        description="Look up an invoice.",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
        handler=lambda args, context: args["id"], handler_binding_id="tests.lookup_id.v1",
        granted_agent_ids=frozenset({"billing_helper", "other_agent"}),
        side_effect_kind="READ_ONLY", idempotency="IDEMPOTENT",
    )
    catalog = compile_agent_catalog(AgentRegistrationBundle(
        registrations=(AgentRegistration(definition),),
        tools=(tool,),
        tool_grants={"lookup_invoice": frozenset({"billing_helper"})},
    ), actual_model_profile_ids=frozenset({"chat"}))

    runtime_entry = catalog.agent_registry.resolve("billing_helper")
    assert runtime_entry.definition is definition
    assert runtime_entry.execution_adapter_id == "agent_router_adapter"
    assert runtime_entry.actual_allowed_tools == frozenset({"lookup_invoice"})
    assert catalog.tool_grants["lookup_invoice"] == frozenset({"billing_helper"})
    assert "Answer billing questions" not in repr(definition)


def test_registration_fails_closed_for_duplicate_unknown_binding_and_bad_version() -> None:
    definition = AgentDefinition(
        agent_id="billing_helper", agent_version="1.0", display_name="Billing",
        role="billing", instructions="Answer.", model_profile_id="missing",
    )
    bundle = AgentRegistrationBundle(
        registrations=(AgentRegistration(definition), AgentRegistration(definition)),
    )
    with pytest.raises(AgentRegistrationCompileError, match="agent_id 重复"):
        compile_agent_catalog(bundle)

    valid = AgentDefinition(
        agent_id="billing_helper", agent_version="1.0", display_name="Billing",
        role="billing", instructions="Answer.", model_profile_id="missing",
        allowed_tools=frozenset({"unknown_tool"}),
    )
    with pytest.raises(AgentRegistrationCompileError, match="未知 model profile"):
        compile_agent_catalog(AgentRegistrationBundle(
            registrations=(AgentRegistration(valid),), model_profile_ids=frozenset({"missing"}),
        ))

    with pytest.raises(ValueError, match="agent_version"):
        AgentDefinition(agent_id="billing_helper", agent_version="", display_name="Billing",
                        role="billing", instructions="Answer.")

    tool = BusinessToolDefinition(
        name="lookup_invoice", description="Lookup.", input_schema={"type": "object"},
        handler=lambda args, context: None, handler_binding_id="tests.noop.v1",
        granted_agent_ids=frozenset({"billing_helper"}),
            side_effect_kind="READ_ONLY", idempotency="IDEMPOTENT",
    )
    asks_for_tool = AgentDefinition(
        agent_id="billing_helper", agent_version="1.0", display_name="Billing",
        role="billing", instructions="Answer.", allowed_tools=frozenset({"lookup_invoice"}),
    )
    with pytest.raises(AgentRegistrationCompileError, match="权限未获平台注册授权"):
        compile_agent_catalog(AgentRegistrationBundle(
            registrations=(AgentRegistration(asks_for_tool),), tools=(tool,),
        ))

    with pytest.raises(AgentRegistrationCompileError, match="grant 引用了未知 Tool"):
        compile_agent_catalog(AgentRegistrationBundle(
            registrations=(), tool_grants={"ghost_tool": frozenset({"billing_helper"})},
        ))


def test_provider_references_use_composition_inventory_and_reject_unbound_profiles() -> None:
    from core.agent_platform.registry import AgentRegistrationBundle

    definition = AgentDefinition(
        agent_id="billing_helper", agent_version="1", display_name="Billing",
        role="billing", instructions="Answer.", model_profile_id="remote",
    )
    bundle = AgentRegistrationBundle(
        registrations=(AgentRegistration(definition),),
        model_profile_ids=frozenset({"remote"}),  # bundle 自报不能证明实际装配。
    )
    with pytest.raises(AgentRegistrationCompileError, match="未知 model profile"):
        compile_agent_catalog(bundle, actual_model_profile_ids=frozenset({"default"}))
    catalog = compile_agent_catalog(bundle, actual_model_profile_ids=frozenset({"default", "remote"}))
    assert catalog.agent_registry.resolve("billing_helper").definition is definition

    for field_name, value, message in (
        ("retrieval_profile_id", "retrieval", "未支持 Agent retrieval"),
        ("memory_profile_id", "memory", "未支持 Agent memory"),
    ):
        unsupported = AgentDefinition(
            agent_id="billing_helper", agent_version="1", display_name="Billing",
            role="billing", instructions="Answer.", **{field_name: value},
        )
        with pytest.raises(AgentRegistrationCompileError, match=message):
            compile_agent_catalog(AgentRegistrationBundle(
                registrations=(AgentRegistration(unsupported),),
                retrieval_profile_ids=frozenset({"retrieval"}),
                memory_profile_ids=frozenset({"memory"}),
            ))


def test_business_values_are_deeply_immutable_and_workflow_shape_is_limited() -> None:
    options = {"limits": {"max_rows": 5}}
    definition = AgentDefinition(
        agent_id="billing_helper", agent_version="1.0", display_name="Billing",
        role="billing", instructions="Answer.", business_options=options,
    )
    options["limits"]["max_rows"] = 99
    assert definition.business_options["limits"]["max_rows"] == 5
    with pytest.raises(TypeError):
        definition.business_options["limits"]["max_rows"] = 10

    with pytest.raises(ValueError, match="synthesis_required"):
        WorkflowDefinition(
            workflow_id="billing", workflow_version="1", tasks=(
                WorkflowTask("lookup", "billing_helper", "Find invoice."),
                WorkflowTask("summarize", "billing_helper", "Summarize invoice."),
            ),
        )

    with pytest.raises(ValueError, match="单个 delegated task"):
        WorkflowDefinition(
            workflow_id="single", workflow_version="1",
            tasks=(WorkflowTask("lookup", "billing_helper", "Find invoice."),),
        )

    with pytest.raises(ValueError, match="workflow binding"):
        ExecutionBinding(kind="workflow", reference="bad id")


def test_schema_rejects_unsupported_keywords_recursively() -> None:
    with pytest.raises(ValueError, match="不支持的 schema keyword: minLength"):
        AgentDefinition(
            agent_id="billing_helper", agent_version="1.0", display_name="Billing",
            role="billing", instructions="Answer.", input_schema={"type": "string", "minLength": 2},
        )
    with pytest.raises(ValueError, match="不支持的 schema keyword: minimum"):
        BusinessToolDefinition(
            name="lookup_invoice", description="Lookup.",
            input_schema={"type": "object", "properties": {"amount": {"type": "number", "minimum": 0}}},
            handler=lambda args, context: args,
        )


def test_schema_accepts_supported_subset() -> None:
    definition = AgentDefinition(
        agent_id="billing_helper", agent_version="1.0", display_name="Billing",
        role="billing", instructions="Answer.",
        input_schema={"type": "object", "properties": {"id": {"type": "string", "enum": ["a", "b"]}},
                      "required": ["id"], "additionalProperties": False},
    )
    assert definition.input_schema["required"] == ("id",)


def test_builtin_seeds_enter_same_catalog_with_explicit_identity_and_shared_adapter() -> None:
    catalog = compile_agent_catalog(
        AgentRegistrationBundle(registrations=()),
        builtin_registrations=tuple(
            DEFAULT_AGENT_REGISTRY.resolve(agent_id)
            for agent_id in DEFAULT_AGENT_REGISTRY.agent_ids
        ),
    )
    registration = catalog.agent_registry.resolve("code_expert")
    assert registration.definition.agent_version == "builtin-1"
    assert registration.execution_adapter_id == "agent_router_adapter"
    assert catalog.agent_registry.resolve("synthesis_agent").execution_adapter_id == "synthesis_agent_adapter"
    assert catalog.agent_registry.resolve("core_router").definition.execution_binding.kind == "dynamic"


def test_registered_workflow_compiles_delegation_and_parallel_fanout_permissions() -> None:
    workflow = WorkflowDefinition(
        workflow_id="support_review", workflow_version="1",
        tasks=(
            WorkflowTask("lookup", "lookup_agent", "Find the account."),
            WorkflowTask("policy", "policy_agent", "Check applicable policy."),
        ),
        synthesis_required=True,
    )
    definitions = (
        AgentDefinition(
            agent_id="coordinator", agent_version="1", display_name="Coordinator",
            role="coordinate", instructions="Coordinate support requests.",
            execution_binding=ExecutionBinding(kind="workflow", reference="support_review"),
        ),
        AgentDefinition(
            agent_id="lookup_agent", agent_version="1", display_name="Lookup",
            role="lookup", instructions="Find account details.",
            entry_allowed=False, delegation_allowed=True,
        ),
        AgentDefinition(
            agent_id="policy_agent", agent_version="1", display_name="Policy",
            role="policy", instructions="Check support policy.",
            entry_allowed=False, delegation_allowed=True,
        ),
    )
    catalog = compile_agent_catalog(AgentRegistrationBundle(
        registrations=tuple(AgentRegistration(item) for item in definitions),
        workflows=(workflow,),
    ), builtin_registrations=tuple(
        DEFAULT_AGENT_REGISTRY.resolve(agent_id)
        for agent_id in DEFAULT_AGENT_REGISTRY.agent_ids
    ))

    assert catalog.agent_registry.resolve("coordinator").workflow is workflow
    for agent_id in ("lookup_agent", "policy_agent"):
        compiled = catalog.agent_registry.resolve(agent_id)
        assert compiled.delegation_allowed
        assert compiled.supports_parallel
        assert compiled.delegated_output_policy.value == "INTERNAL"


def test_toolset_identity_tracks_schema_and_final_permissions_stably() -> None:
    def compile_identity(*, second_tool=False, schema_field="invoice_id"):
        names = {"lookup_invoice"}
        if second_tool:
            names.add("list_invoices")
        definitions = tuple(
            BusinessToolDefinition(
                name=name,
                description=name,
                input_schema={
                    "type": "object",
                    "properties": {schema_field if name == "lookup_invoice" else "limit": {"type": "string"}},
                    "required": [schema_field if name == "lookup_invoice" else "limit"],
                },
                handler=lambda args, context: args, handler_binding_id="tests.echo.v1",
                granted_agent_ids=frozenset({"billing_helper"}),
                side_effect_kind="READ_ONLY",
                idempotency="IDEMPOTENT",
            )
            for name in sorted(names)
        )
        definition = AgentDefinition(
            agent_id="billing_helper", agent_version="1", display_name="Billing",
            role="billing", instructions="Answer.", allowed_tools=frozenset(names),
        )
        catalog = compile_agent_catalog(AgentRegistrationBundle(
            registrations=(AgentRegistration(definition),),
            tools=definitions,
            tool_grants={name: frozenset({"billing_helper"}) for name in names},
        ))
        registration = catalog.agent_registry.resolve("billing_helper")
        assert registration.actual_allowed_tools == frozenset(names)
        return registration.toolset_identity

    first = compile_identity()
    assert first == compile_identity()
    assert first != compile_identity(schema_field="invoice_number")
    assert first != compile_identity(second_tool=True)
