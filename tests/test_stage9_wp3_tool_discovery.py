from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.agent_router import AgentRouter
from core.persistence.models import RunControlRow
from core.runtime.budget import BudgetLedger, RunBudget
from core.runtime.context import RunContext
from core.runtime.retry import OperationIdempotency
from core.runtime.tool_adapters import LegacyStringToolAdapter
from core.runtime.tool_contract import ToolExecutionSpec, ToolSideEffectKind
from core.runtime.tool_discovery import (
    ToolCatalog,
    ToolDiscovery,
    ToolResolutionSnapshot,
    ToolSnapshotCompatibilityCode,
    ToolSnapshotCompatibilityError,
    create_tool_snapshot,
    hydrate_tool_snapshot,
)
from core.runtime.tool_governance import (
    ToolGovernanceError,
    ToolGovernanceErrorCode,
    ToolGovernanceOutcome,
)
from core.runtime.tool_registry import (
    ToolDescriptor,
    ToolRegistration,
    ToolRegistry,
    ToolRegistryError,
)
from core.runtime.tool_snapshot_store import (
    PostgresToolResolutionSnapshotStore,
    ToolSnapshotStoreError,
)
from mcp.adapter import McpBackedToolAdapter


def _registration(name: str, description: str) -> ToolRegistration:
    return ToolRegistration(
        descriptor=ToolDescriptor(name=name, description=description),
        adapter=LegacyStringToolAdapter(tool_name=name, function=lambda _: "ok"),
    )


def _registry(*registrations: ToolRegistration) -> ToolRegistry:
    registry = ToolRegistry()
    for registration in registrations:
        registry.register(registration)
    registry.freeze()
    return registry


def _mcp_registration(
    name: str,
    *,
    server_id: str = "server-a",
    remote_name: str = "remote_tool",
    schema: dict | None = None,
) -> ToolRegistration:
    adapter = McpBackedToolAdapter(
        spec=ToolExecutionSpec(
            tool_name=name,
            side_effect_kind=ToolSideEffectKind.NONE,
            idempotency=OperationIdempotency.READ_ONLY,
        ),
        server_id=server_id,
        remote_name=remote_name,
        input_schema=schema or {"type": "object", "properties": {}},
        session_resolver=lambda: None,
        request_timeout_seconds=1.0,
    )
    return ToolRegistration(
        descriptor=ToolDescriptor(name=name, description="MCP status tool"),
        adapter=adapter,
    )


async def _insert_run(database, run_id: str) -> None:
    async with database.transaction() as session:
        session.add(RunControlRow(run_id=run_id))
        await session.flush()


def test_discovery_returns_deterministic_top_k_and_empty_without_match() -> None:
    registrations = tuple(
        _registration(f"excel_tool_{index:02d}", "Analyze Excel workbook")
        for index in range(100)
    )
    discovery = ToolDiscovery(ToolCatalog(_registry(*registrations)), top_k=10)

    first = discovery.discover_tools("excel", "core_router")
    second = discovery.discover_tools("excel", "core_router")

    assert tuple(item.descriptor.name for item in first) == tuple(
        item.descriptor.name for item in second
    )
    assert len(first) == 10
    assert discovery.discover_tools("no such capability", "core_router") == ()


def test_discovery_filters_denied_tools_before_ranking() -> None:
    allowed = _registration("read_excel", "Read Excel file")
    denied = _registration("write_excel", "Write Excel file")

    class Governance:
        def authorize_tool(self, context, registration):
            return SimpleNamespace(
                outcome=(
                    ToolGovernanceOutcome.ALLOW
                    if registration.descriptor.name == "read_excel"
                    else ToolGovernanceOutcome.DENY
                )
            )

    discovery = ToolDiscovery(
        ToolCatalog(_registry(allowed, denied)), top_k=10, governance=Governance()
    )
    assert tuple(item.descriptor.name for item in discovery.discover_tools("excel", "agent")) == (
        "read_excel",
    )


def test_snapshot_keeps_old_registration_and_schema_digest() -> None:
    old = _registration("read_excel", "Read Excel v1")
    old_registry = _registry(old)
    snapshot = create_tool_snapshot("run-1", old_registry.registrations())

    new = _registration("read_excel", "Read Excel v2")
    new_registry = _registry(new)

    assert isinstance(snapshot, ToolResolutionSnapshot)
    assert snapshot.resolve("read_excel") is old
    assert snapshot.tools[0].descriptor_digest != create_tool_snapshot(
        "run-2", new_registry.registrations()
    ).tools[0].descriptor_digest
    assert snapshot.run_id == "run-1"


def test_builtin_and_mcp_style_registrations_share_one_catalog() -> None:
    builtin = _registration("read_file", "Read a local file")
    mcp = _registration("mcp_status", "Read status from MCP server")
    catalog = ToolCatalog(_registry(builtin, mcp))

    assert tuple(item.descriptor.name for item in catalog.list_available_tools()) == (
        "read_file",
        "mcp_status",
    )
    assert tuple(item.descriptor.name for item in ToolDiscovery(catalog).discover_tools("status", "agent")) == (
        "mcp_status",
    )


@pytest.mark.asyncio
async def test_snapshot_is_postgres_durable_create_once_and_loadable_by_run_id(
    clean_database,
) -> None:
    await _insert_run(clean_database, "run-durable")
    registry = _registry(_registration("read_excel", "Read Excel workbook"))
    discovery = ToolDiscovery(ToolCatalog(registry))
    snapshot = discovery.create_tool_snapshot(
        "run-durable",
        discovery.discover_tools("excel", "core_router"),
        selection_query="excel",
    )

    await PostgresToolResolutionSnapshotStore(clean_database).save(snapshot)
    loaded = await PostgresToolResolutionSnapshotStore(clean_database).load(
        "run-durable"
    )

    assert loaded is not None
    assert loaded.to_dict() == snapshot.to_dict()
    hydrated = hydrate_tool_snapshot(loaded, registry)
    assert hydrated.resolve("read_excel") is registry.require("read_excel")
    changed = create_tool_snapshot(
        "run-durable", (), registry_digest=ToolCatalog(registry).catalog_digest
    )
    with pytest.raises(ToolSnapshotStoreError):
        await PostgresToolResolutionSnapshotStore(clean_database).save(changed)


def test_existing_run_detects_schema_and_provider_drift() -> None:
    old = _mcp_registration(
        "mcp_status", schema={"type": "object", "properties": {"id": {"type": "string"}}}
    )
    snapshot = create_tool_snapshot("run-drift", (old,))

    schema_v2 = _mcp_registration(
        "mcp_status", schema={"type": "object", "properties": {"id": {"type": "integer"}}}
    )
    with pytest.raises(ToolSnapshotCompatibilityError) as schema_error:
        hydrate_tool_snapshot(snapshot, _registry(schema_v2))
    assert schema_error.value.error_code is ToolSnapshotCompatibilityCode.SCHEMA_DRIFT

    other_provider = _mcp_registration("mcp_status", server_id="server-b")
    with pytest.raises(ToolSnapshotCompatibilityError) as provider_error:
        hydrate_tool_snapshot(snapshot, _registry(other_provider))
    assert provider_error.value.error_code is ToolSnapshotCompatibilityCode.PROVIDER_DRIFT

    with pytest.raises(ToolSnapshotCompatibilityError) as missing_error:
        hydrate_tool_snapshot(snapshot, _registry())
    assert missing_error.value.error_code is ToolSnapshotCompatibilityCode.TOOL_MISSING


def test_mcp_generation_is_not_durable_identity_but_schema_is() -> None:
    first = _mcp_registration("mcp_status")
    snapshot = create_tool_snapshot("run-mcp", (first,))
    reconnected = _mcp_registration("mcp_status")

    hydrated = hydrate_tool_snapshot(snapshot, _registry(reconnected))

    assert hydrated.resolve("mcp_status") is reconnected
    item = snapshot.tools[0]
    assert item.provider_kind == "mcp"
    assert item.provider_identity == "server-a"
    assert item.remote_tool_id == "remote_tool"
    assert "generation" not in snapshot.to_dict()["tools"][0]


def test_new_catalog_tool_is_invisible_to_existing_run_but_discoverable_by_new_run() -> None:
    tool_a = _registration("tool_a", "Common capability")
    old_snapshot = create_tool_snapshot("run-old", (tool_a,))
    tool_b = _registration("tool_b", "New analytics capability")
    expanded_registry = _registry(tool_a, tool_b)

    hydrated = hydrate_tool_snapshot(old_snapshot, expanded_registry)

    assert hydrated.resolve("tool_b") is None
    assert tuple(
        item.descriptor.name
        for item in ToolDiscovery(ToolCatalog(expanded_registry)).discover_tools(
            "analytics", "core_router"
        )
    ) == ("tool_b",)


def _execution_boundary_router(registry, governance, proposed_tool: str) -> AgentRouter:
    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    router.tool_governance_service = governance
    router.tool_execution_service = SimpleNamespace(
        execute_sync=lambda **_: (_ for _ in ()).throw(
            AssertionError("Tool execution must not start")
        )
    )
    router._build_messages = lambda **_: [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "query"},
    ]
    router._plan_tool_call = lambda *_args, **_kwargs: (proposed_tool, "")
    return router


def test_tool_outside_snapshot_is_rejected_before_execution() -> None:
    tool_a = _registration("tool_a", "Tool A")
    tool_b = _registration("tool_b", "Tool B")
    registry = _registry(tool_a, tool_b)
    context = RunContext.create(entry_agent_id="core_router")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    context.attach_tool_resolution_snapshot(
        create_tool_snapshot(context.run_id, (tool_a,))
    )
    router = _execution_boundary_router(
        registry,
        SimpleNamespace(),
        "tool_b",
    )

    with pytest.raises(ToolRegistryError):
        router._prepare_answer_messages("core_router", "query", run_context=context)


def test_snapshot_does_not_bypass_current_permission_revocation() -> None:
    tool = _registration("tool_a", "Tool A")
    registry = _registry(tool)
    context = RunContext.create(entry_agent_id="core_router")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    context.attach_tool_resolution_snapshot(
        create_tool_snapshot(context.run_id, (tool,))
    )
    governance = SimpleNamespace(
        authorize_tool=lambda *_: SimpleNamespace(
            outcome=ToolGovernanceOutcome.DENY,
            safe_error_code=ToolGovernanceErrorCode.PERMISSION_DENIED.value,
        )
    )
    router = _execution_boundary_router(registry, governance, "tool_a")

    with pytest.raises(ToolGovernanceError) as denied:
        router._prepare_answer_messages("core_router", "query", run_context=context)
    assert denied.value.error_code is ToolGovernanceErrorCode.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_snapshot_persistence_failure_prevents_context_hydration() -> None:
    tool = _registration("tool_a", "Tool A")
    registry = _registry(tool)

    class FailingStore:
        async def load(self, _run_id):
            return None

        async def save(self, _snapshot):
            raise ToolSnapshotStoreError("database unavailable")

    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    router.tool_discovery = ToolDiscovery(ToolCatalog(registry))
    router.tool_snapshot_store = FailingStore()
    context = RunContext.create(entry_agent_id="core_router")

    with pytest.raises(ToolSnapshotStoreError):
        await router.prepare_tool_resolution_snapshot(context, "core_router", "tool")
    assert context.tool_resolution_snapshot is None
