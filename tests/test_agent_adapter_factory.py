"""AgentAdapterFactory contract: immutable symbol resolution, no run data."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.runtime import (
    AgentAdapterError,
    AgentAdapterErrorCode,
    AgentAdapterFactory,
    AgentRouterSingleAgentAdapter,
    SynthesisAgentAdapter,
)
from core.runtime.agent_registry import (
    AgentRegistration,
    AgentRegistry,
    DEFAULT_AGENT_REGISTRY,
    ResultContentType,
)
from core.runtime.planning import OutputPolicy


def default_adapter_map():
    router = object()
    return (
        ("core_router_adapter", AgentRouterSingleAgentAdapter(router)),
        ("data_analyst_adapter", AgentRouterSingleAgentAdapter(router)),
        ("code_expert_adapter", AgentRouterSingleAgentAdapter(router)),
        ("knowledge_expert_adapter", AgentRouterSingleAgentAdapter(router)),
        ("synthesis_agent_adapter", SynthesisAgentAdapter(router)),
    )


def test_all_enabled_registry_adapter_ids_resolve() -> None:
    factory = AgentAdapterFactory(DEFAULT_AGENT_REGISTRY, default_adapter_map())
    for agent_id in DEFAULT_AGENT_REGISTRY.agent_ids:
        registration = DEFAULT_AGENT_REGISTRY.resolve(agent_id)
        adapter = factory.resolve(registration.execution_adapter_id)
        assert adapter is not None
        assert callable(getattr(adapter, "execute", None))


def test_adapter_resolves_by_symbol_and_is_shared() -> None:
    factory = AgentAdapterFactory(DEFAULT_AGENT_REGISTRY, default_adapter_map())
    first = factory.resolve("code_expert_adapter")
    second = factory.resolve("code_expert_adapter")
    assert first is second
    assert "code_expert_adapter" in factory.adapter_ids


def test_unknown_adapter_fails_closed() -> None:
    factory = AgentAdapterFactory(DEFAULT_AGENT_REGISTRY, default_adapter_map())
    with pytest.raises(AgentAdapterError) as exc_info:
        factory.resolve("not_registered_adapter")
    assert exc_info.value.error_code is AgentAdapterErrorCode.UNKNOWN_ADAPTER


def test_duplicate_adapter_id_fails() -> None:
    router = object()
    adapters = (
        ("dup_adapter", AgentRouterSingleAgentAdapter(router)),
        ("dup_adapter", AgentRouterSingleAgentAdapter(router)),
    )
    with pytest.raises(AgentAdapterError) as exc_info:
        AgentAdapterFactory(DEFAULT_AGENT_REGISTRY, adapters)
    assert exc_info.value.error_code is AgentAdapterErrorCode.DUPLICATE_ADAPTER


def test_factory_immutable() -> None:
    factory = AgentAdapterFactory(DEFAULT_AGENT_REGISTRY, default_adapter_map())
    with pytest.raises(AttributeError):
        factory._adapters = {}  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        factory._adapters["code_expert_adapter"] = object()  # type: ignore[index]


def test_factory_holds_no_run_data() -> None:
    factory = AgentAdapterFactory(DEFAULT_AGENT_REGISTRY, default_adapter_map())
    rendered = repr(factory)
    assert "run" not in rendered.lower() or "run_id" not in rendered
    for adapter_id in factory.adapter_ids:
        adapter = factory.resolve(adapter_id)
        for forbidden in ("run_id", "agent_state", "result", "user_query"):
            assert not hasattr(adapter, forbidden), (
                f"{adapter_id} 不应持有 {forbidden}"
            )


def test_registry_with_unresolvable_adapter_fails_at_construction() -> None:
    registration = AgentRegistration(
        agent_id="custom_agent",
        execution_adapter_id="missing_adapter",
        display_name="Custom",
        role="role",
        avatar="avatar.png",
        enabled=True,
        entry_allowed=False,
        entry_output_policy=OutputPolicy.INTERNAL,
        model_direct_allowed=False,
        delegation_allowed=True,
        delegated_output_policy=OutputPolicy.INTERNAL,
        allows_single_delegated_passthrough=False,
        synthesis_only=False,
        supports_parallel=False,
        accepted_input_types=frozenset({"text"}),
        produced_result_types=frozenset({ResultContentType.TEXT}),
        capabilities=frozenset({"custom"}),
    )
    registry = AgentRegistry((registration,))
    with pytest.raises(AgentAdapterError) as exc_info:
        AgentAdapterFactory(
            registry,
            (("other_adapter", AgentRouterSingleAgentAdapter(object())),),
        )
    assert (
        exc_info.value.error_code
        is AgentAdapterErrorCode.ADAPTER_NOT_RESOLVABLE
    )


def test_multiple_adapter_ids_may_share_one_adapter_class() -> None:
    router = object()
    factory = AgentAdapterFactory(
        DEFAULT_AGENT_REGISTRY,
        (
            ("code_expert_adapter", AgentRouterSingleAgentAdapter(router)),
            ("data_analyst_adapter", AgentRouterSingleAgentAdapter(router)),
            ("knowledge_expert_adapter", AgentRouterSingleAgentAdapter(router)),
            ("core_router_adapter", AgentRouterSingleAgentAdapter(router)),
            ("synthesis_agent_adapter", SynthesisAgentAdapter(router)),
        ),
    )
    assert type(factory.resolve("code_expert_adapter")) is type(
        factory.resolve("data_analyst_adapter")
    )


def test_driver_has_no_agent_specific_branching_evidence() -> None:
    source = (
        Path("core/runtime/multi_agent_driver.py")
        .read_text(encoding="utf-8")
    )
    for pattern in (
        'agent_id == "',
        '"code_expert"',
        '"knowledge_expert"',
        '"data_analyst"',
        '"core_router"',
    ):
        assert pattern not in source, (
            f"MultiAgentDriver 不应包含按 Agent 名称的分支: {pattern}"
        )
