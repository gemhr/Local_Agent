from dataclasses import FrozenInstanceError, replace

import pytest

from core.runtime.agent_registry import (
    AgentRegistry,
    AgentRegistryError,
    AgentRegistryErrorCode,
    DEFAULT_AGENT_REGISTRY,
)
from core.runtime.planning import OutputPolicy


def test_default_registry_has_unique_expected_agents_and_shared_legacy_metadata() -> None:
    assert DEFAULT_AGENT_REGISTRY.agent_ids == (
        "core_router",
        "data_analyst",
        "code_expert",
        "knowledge_expert",
        "synthesis_agent",
    )
    assert len(DEFAULT_AGENT_REGISTRY.agent_ids) == len(set(DEFAULT_AGENT_REGISTRY.agent_ids))
    assert tuple(DEFAULT_AGENT_REGISTRY.legacy_display_config()) == DEFAULT_AGENT_REGISTRY.agent_ids[:-1]
    assert DEFAULT_AGENT_REGISTRY.delegated_specialist_ids() == (
        "data_analyst",
        "code_expert",
        "knowledge_expert",
    )
    adapter_ids = tuple(
        DEFAULT_AGENT_REGISTRY.resolve(agent_id).execution_adapter_id
        for agent_id in DEFAULT_AGENT_REGISTRY.agent_ids
    )
    assert adapter_ids == (
        "core_router_adapter",
        "data_analyst_adapter",
        "code_expert_adapter",
        "knowledge_expert_adapter",
        "synthesis_agent_adapter",
    )
    assert len(adapter_ids) == len(set(adapter_ids))


def test_entry_delegated_and_synthesis_policies_are_distinct() -> None:
    core = DEFAULT_AGENT_REGISTRY.require_entry("core_router")
    knowledge = DEFAULT_AGENT_REGISTRY.require_delegated("knowledge_expert")
    code = DEFAULT_AGENT_REGISTRY.require_delegated("code_expert")
    synthesis = DEFAULT_AGENT_REGISTRY.synthesis_registration()
    assert core.model_direct_allowed and not core.delegation_allowed
    assert knowledge.entry_allowed and knowledge.allows_single_delegated_passthrough
    assert knowledge.delegated_output_policy is OutputPolicy.INTERNAL
    assert code.entry_allowed and not code.allows_single_delegated_passthrough
    assert synthesis.agent_id == "synthesis_agent"
    assert synthesis.synthesis_only and not synthesis.entry_allowed
    assert synthesis.delegated_output_policy is OutputPolicy.FINAL_SYNTHESIS


def test_unknown_disabled_and_synthesis_entry_fail_with_stable_codes() -> None:
    with pytest.raises(AgentRegistryError) as unknown:
        DEFAULT_AGENT_REGISTRY.resolve("not_registered")
    assert unknown.value.error_code is AgentRegistryErrorCode.UNKNOWN_AGENT
    with pytest.raises(AgentRegistryError) as entry:
        DEFAULT_AGENT_REGISTRY.require_entry("synthesis_agent")
    assert entry.value.error_code is AgentRegistryErrorCode.ENTRY_AGENT_NOT_ALLOWED
    disabled = replace(DEFAULT_AGENT_REGISTRY.resolve("code_expert"), enabled=False)
    registry = AgentRegistry((disabled,))
    with pytest.raises(AgentRegistryError) as captured:
        registry.resolve("code_expert")
    assert captured.value.error_code is AgentRegistryErrorCode.AGENT_DISABLED


def test_registry_and_registrations_are_immutable_and_repr_safe() -> None:
    registration = DEFAULT_AGENT_REGISTRY.resolve("knowledge_expert")
    with pytest.raises(FrozenInstanceError):
        registration.entry_allowed = False
    with pytest.raises(AttributeError):
        DEFAULT_AGENT_REGISTRY.agent_ids = ()
    with pytest.raises(AttributeError):
        DEFAULT_AGENT_REGISTRY._ordered_ids = ()
    rendered = repr(DEFAULT_AGENT_REGISTRY)
    assert "secret" not in rendered.lower()
    assert "instruction" not in rendered.lower()
    assert "run_id" not in rendered.lower()


def test_registry_rejects_duplicate_and_invalid_policy_registration() -> None:
    core = DEFAULT_AGENT_REGISTRY.resolve("core_router")
    with pytest.raises(AgentRegistryError) as duplicate:
        AgentRegistry((core, core))
    assert duplicate.value.error_code is AgentRegistryErrorCode.DUPLICATE_AGENT
    with pytest.raises(ValueError):
        replace(
            DEFAULT_AGENT_REGISTRY.resolve("synthesis_agent"),
            entry_allowed=True,
        )
    with pytest.raises(ValueError):
        replace(core, agent_id="INVALID-ID")
    with pytest.raises(ValueError):
        replace(core, execution_adapter_id="unsafe adapter id")
    with pytest.raises(ValueError):
        replace(
            DEFAULT_AGENT_REGISTRY.resolve("code_expert"),
            delegated_output_policy=OutputPolicy.FINAL_SYNTHESIS,
        )
