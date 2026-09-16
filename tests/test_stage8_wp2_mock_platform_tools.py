"""Stage8-WP2 Typed Mock 平台与统一 Tool Runtime 接入测试。"""

import json
from datetime import UTC, datetime
import uuid

import pytest

from core.runtime import BudgetLedger, RunBudget, ToolExecutionService, ToolExecutionStatus, ToolSideEffectState, create_run_context
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.approval import (
    ApprovalDecisionValue,
    ApprovalRequest,
    ApprovalStatus,
    compute_invocation_binding_digest,
)
from core.runtime.context import RunContext
from core.runtime.durable_approval import DurableApprovalService
from core.runtime.retry import OperationIdempotency
from core.runtime.run_control import DurableRunControlService
from core.runtime.tool_contract import safe_key_digest
from core.runtime.tool_governance import ToolGovernanceContext, ToolGovernanceOutcome, ToolGovernanceService, ToolPolicyCatalog, register_default_tool_policies
from core.runtime.tool_idempotency import DurableToolInvocationService, ToolInvocationState
from core.runtime.tool_registry import ToolRegistry
from core.stage8.platforms import DeterministicMockPlatform, FeatureContextBuilder, build_stage8_tool_adapters
from tools.registry import build_builtin_tool_registrations


def _context():
    context, _ = create_run_context(entry_agent_id="core_router", timeout_seconds=2)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=4)))
    return context


def _governance(registry):
    catalog = ToolPolicyCatalog(tool_registry=registry, agent_registry=DEFAULT_AGENT_REGISTRY)
    register_default_tool_policies(catalog)
    catalog.freeze()
    return ToolGovernanceService(catalog, DEFAULT_AGENT_REGISTRY)


def test_seeded_platform_and_feature_context_are_deterministic():
    platform = DeterministicMockPlatform.seeded()
    assert platform.get_feature_document("FEATURE-001") == platform.get_feature_document("FEATURE-001")
    context = FeatureContextBuilder(platform).build("FEATURE-001")
    assert context.feature_document
    assert context.code_diff == "新增撤回路径"
    evidence = {item.source_type.value: item for item in context.retrieval_evidence}
    assert evidence["FEATURE_DOCUMENT"].source_ref == "FEATURE-001"
    assert evidence["CODE_DIFF"].source_ref == "abc123"
    assert evidence["MEETING_SUMMARY"].source_ref == "MEETING-001"


@pytest.mark.asyncio
async def test_query_tool_uses_typed_invocation_and_execution_service():
    platform = DeterministicMockPlatform.seeded()
    registry = ToolRegistry()
    for registration in build_builtin_tool_registrations(platform):
        registry.register(registration)
    registry.freeze()
    adapter = registry.require("stage8_get_environment").adapter
    invocation = adapter.build_invocation(json.dumps({"environment_id": "ENV-001"}))
    governance = _governance(registry)
    governance_context = ToolGovernanceContext("core_router", "run", "query")
    registration = registry.require("stage8_get_environment")
    assert governance.evaluate_invocation(governance_context, registration, invocation, adapter.spec).outcome is ToolGovernanceOutcome.ALLOW
    result = await ToolExecutionService().execute(invocation=invocation, adapter=adapter, run_context=_context(), step_id="query")
    assert result.status is ToolExecutionStatus.SUCCEEDED
    assert '"environment_id": "ENV-001"' in result.output.content


def test_query_allow_and_side_effect_requires_approval():
    registry = ToolRegistry()
    for registration in build_builtin_tool_registrations():
        registry.register(registration)
    registry.freeze()
    governance = _governance(registry)
    query = registry.require("stage8_get_environment")
    query_invocation = query.adapter.build_invocation('{"environment_id":"ENV-001"}')
    query_context = ToolGovernanceContext("core_router", "run", "query")
    assert governance.authorize_tool(query_context, query).outcome is ToolGovernanceOutcome.ALLOW
    assert governance.evaluate_invocation(query_context, query, query_invocation, query.adapter.spec).outcome is ToolGovernanceOutcome.ALLOW
    ticket = registry.require("stage8_create_ticket")
    ticket_invocation = ticket.adapter.build_invocation('{"title":"权限问题","severity":"HIGH","description":"撤回权限失败"}')
    assert governance.evaluate_invocation(query_context, ticket, ticket_invocation, ticket.adapter.spec).outcome is ToolGovernanceOutcome.APPROVAL_REQUIRED


@pytest.mark.asyncio
async def test_start_execution_mutates_queryable_mock_external_state_through_runtime():
    platform = DeterministicMockPlatform.seeded()
    adapter = dict((name, adapter) for name, _, adapter in build_stage8_tool_adapters(platform))["stage8_start_execution"]
    invocation = adapter.build_invocation(json.dumps({"case_id": "CASE-001", "environment_id": "ENV-001", "executor_id": "EXECUTOR-001"}))
    result = await ToolExecutionService().execute(invocation=invocation, adapter=adapter, run_context=_context(), step_id="start")
    assert result.status is ToolExecutionStatus.SUCCEEDED
    assert result.side_effect_state is ToolSideEffectState.COMMITTED
    assert platform.get_execution("EXEC-001").status == "RUNNING"


@pytest.mark.asyncio
async def test_start_execution_provider_replays_same_key_without_new_execution():
    platform = DeterministicMockPlatform.seeded()
    adapter = dict((name, adapter) for name, _, adapter in build_stage8_tool_adapters(platform))["stage8_start_execution"]
    arguments = json.dumps({"case_id": "CASE-001", "environment_id": "ENV-001", "executor_id": "EXECUTOR-001"})
    first_invocation = adapter.build_invocation(arguments)
    replay_invocation = adapter.build_invocation(arguments)
    assert first_invocation.invocation_id != replay_invocation.invocation_id
    assert first_invocation.idempotency_key == replay_invocation.idempotency_key
    assert adapter.spec.idempotency is OperationIdempotency.IDEMPOTENT_WITH_KEY
    first = await ToolExecutionService().execute(invocation=first_invocation, adapter=adapter, run_context=_context(), step_id="start-1")
    second = await ToolExecutionService().execute(invocation=replay_invocation, adapter=adapter, run_context=_context(), step_id="start-2")
    assert first.status is ToolExecutionStatus.SUCCEEDED
    assert second.status is ToolExecutionStatus.SUCCEEDED
    assert second.idempotency_replayed is True
    assert len(platform.executions) == 1


def test_create_ticket_is_non_idempotent_with_stable_resource_key():
    adapter = dict((name, adapter) for name, _, adapter in build_stage8_tool_adapters(DeterministicMockPlatform.seeded()))["stage8_create_ticket"]
    invocation = adapter.build_invocation(json.dumps({"title": "权限问题", "severity": "HIGH", "description": "撤回权限失败"}))
    assert adapter.spec.idempotency is OperationIdempotency.NON_IDEMPOTENT
    assert adapter.spec.supports_idempotency_replay is False
    assert invocation.idempotency_key is None
    assert invocation.resource_key == "stage8:tickets"


@pytest.mark.asyncio
async def test_create_ticket_uses_durable_approval_claim_and_tool_runtime(clean_database):
    platform = DeterministicMockPlatform.seeded()
    registry = ToolRegistry()
    for registration in build_builtin_tool_registrations(platform):
        registry.register(registration)
    registry.freeze()
    registration = registry.require("stage8_create_ticket")
    invocation = registration.adapter.build_invocation(
        json.dumps(
            {
                "title": "权限问题",
                "severity": "HIGH",
                "description": "撤回权限失败",
            },
            ensure_ascii=False,
        )
    )
    governance_context = ToolGovernanceContext("core_router", "run", "ticket")
    decision = _governance(registry).evaluate_invocation(
        governance_context,
        registration,
        invocation,
        registration.adapter.spec_for(invocation),
    )
    assert decision.outcome is ToolGovernanceOutcome.APPROVAL_REQUIRED
    assert decision.risk_level is not None
    risk_facts = tuple(fact.value for fact in decision.risk_facts)

    lease = await DurableRunControlService(clean_database).claim(
        uuid.uuid4().hex,
        "stage8-wp2-owner",
    )
    binding = compute_invocation_binding_digest(
        invocation_identity_digest=safe_key_digest(invocation.invocation_id),
        tool_name=invocation.tool_name,
        arguments_digest=invocation.arguments_digest,
        idempotency_key_digest=safe_key_digest(invocation.idempotency_key),
        resource_key_digest=safe_key_digest(invocation.resource_key),
        risk_level=decision.risk_level.value,
        risk_facts=risk_facts,
    )
    approval_request = ApprovalRequest(
        approval_id=uuid.uuid4().hex,
        run_id=lease.run_id,
        step_id="ticket",
        invocation_id=invocation.invocation_id,
        tool_name=invocation.tool_name,
        invocation_identity_digest=safe_key_digest(invocation.invocation_id),
        arguments_digest=invocation.arguments_digest,
        idempotency_key_digest=safe_key_digest(invocation.idempotency_key),
        resource_key_digest=safe_key_digest(invocation.resource_key),
        risk_level=decision.risk_level.value,
        risk_facts=risk_facts,
        invocation_binding_digest=binding,
        requested_at=datetime.now(UTC),
    )
    approval_service = DurableApprovalService(clean_database)
    await approval_service.create(approval_request)
    assert platform.search_tickets("权限问题") == []
    approved = await approval_service.decide(
        run_id=lease.run_id,
        approval_id=approval_request.approval_id,
        invocation_binding_digest=binding,
        decision=ApprovalDecisionValue.APPROVE,
    )
    assert approved.effective_status is ApprovalStatus.APPROVED
    claim = await approval_service.claim_execution(
        lease=lease,
        approval_id=approval_request.approval_id,
        invocation_binding_digest=binding,
    )

    run_context = RunContext.create(
        entry_agent_id="core_router",
        run_id=lease.run_id,
    )
    run_context.attach_durable_lease(lease)
    run_context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=1)))
    durable_invocations = DurableToolInvocationService(clean_database)
    result = await ToolExecutionService(
        durable_invocation_service=durable_invocations
    ).execute(
        invocation=invocation,
        adapter=registration.adapter,
        run_context=run_context,
        step_id="ticket",
        durable_approval_id=approval_request.approval_id,
        durable_execution_claim_id=claim.claim_id,
        durable_binding_digest=binding,
    )

    assert result.status is ToolExecutionStatus.SUCCEEDED
    assert result.side_effect_state is ToolSideEffectState.COMMITTED
    tickets = platform.search_tickets("权限问题")
    assert [ticket.ticket_id for ticket in tickets] == ["BUG-001"]
    durable_record = await durable_invocations.get(invocation.invocation_id)
    assert durable_record is not None
    assert durable_record.state is ToolInvocationState.COMMITTED
    assert durable_record.approval_id == approval_request.approval_id
    assert durable_record.execution_claim_id == claim.claim_id
    assert durable_record.invocation_binding_digest == binding
