"""Stage7-WP5 durable Tool side-effect intent and reconciliation evidence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import uuid

import pytest

from core.persistence.errors import DatabaseErrorCode, PersistenceError
from core.runtime.budget import BudgetLedger, RunBudget
from core.runtime.approval import (
    ApprovalDecisionValue,
    ApprovalRequest,
    ApprovalStatus,
    compute_invocation_binding_digest,
)
from core.runtime.context import RunContext
from core.runtime.durable_approval import DurableApprovalService
from core.runtime.run_control import DurableRunControlService, OwnershipLost
from core.runtime.tool_adapters import ComplexWorkflowToolAdapter, ToolAdapterInvocationError
from core.runtime.tool_contract import (
    RetryDisposition,
    ToolErrorCategory,
    ToolExecutionError,
    ToolInvocation,
    ToolSideEffectState,
    safe_key_digest,
)
from core.runtime.tool_execution import ToolExecutionService
from core.runtime.tool_idempotency import (
    DurableToolInvocationService,
    ProviderReconciliationResult,
    ToolInvocationState,
)
from core.runtime.tool_registry import ToolRegistry
from tools.registry import register_all_tools


def _invocation(
    *,
    mode: str = "IDEMPOTENT_COMMIT",
    invocation_id: str | None = None,
    requested_timeout_seconds: float | None = None,
    failure_injection: str | None = None,
) -> ToolInvocation:
    arguments = {
        "operation_id": "wp5-operation",
        "resource_key": "wp5-resource",
        "idempotency_key": "stable-wp5-key",
        "execution_mode": mode,
        "items": [{"item_id": "item-1", "action": "ADD", "quantity": 1}],
    }
    if failure_injection is not None:
        arguments["failure_injection"] = failure_injection
        arguments["processing_options"] = {"enable_compensation": False}
    return ToolInvocation.create(
        tool_name="complex_workflow_simulator",
        invocation_id=invocation_id or uuid.uuid4().hex,
        idempotency_key="stable-wp5-key",
        resource_key="wp5-resource",
        requested_timeout_seconds=requested_timeout_seconds,
        arguments=arguments,
    )


@dataclass
class _Provider:
    result: ProviderReconciliationResult
    calls: int = 0

    def reconcile_provider(self, invocation, *, provider_operation_id):
        del invocation, provider_operation_id
        self.calls += 1
        return self.result


class _CountingAdapter(ComplexWorkflowToolAdapter):
    is_async = True

    def __init__(self, *, delay: float = 0, provider_error: bool = False) -> None:
        super().__init__()
        self.calls = 0
        self.delay = delay
        self.provider_error = provider_error

    async def invoke_once(self, invocation, context):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.provider_error:
            raise ToolAdapterInvocationError(
                category=ToolErrorCategory.TRANSIENT,
                safe_error_code="PROVIDER_CONNECTION_LOST",
                safe_message="Provider connection lost.",
            )
        return super().invoke_once(invocation, context)


@pytest.mark.asyncio
async def test_durable_invocation_state_machine_and_stable_identity(clean_database):
    run = DurableRunControlService(clean_database)
    lease = await run.claim(uuid.uuid4().hex, "wp5-owner-a")
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()

    prepared = await service.prepare(
        lease=lease, step_id="answer", invocation=invocation, tool_name=invocation.tool_name
    )
    assert prepared.state is ToolInvocationState.PREPARED
    assert prepared.idempotency_key_digest != "stable-wp5-key"
    assert await service.get(invocation.invocation_id) == prepared

    started = await service.start(lease=lease, invocation_id=invocation.invocation_id)
    assert started.state is ToolInvocationState.STARTED
    committed = await service.committed(
        lease=lease,
        invocation_id=invocation.invocation_id,
        provider_operation_id="provider-operation-1",
    )
    assert committed.state is ToolInvocationState.COMMITTED
    assert committed.provider_operation_id == "provider-operation-1"


@pytest.mark.asyncio
async def test_real_approval_claim_flows_into_durable_tool_invocation(clean_database):
    run = DurableRunControlService(clean_database)
    lease = await run.claim(uuid.uuid4().hex, "wp5-cross-wp-owner")
    invocation = _invocation()
    risk_facts = ("RESOURCE_WRITE",)
    binding = compute_invocation_binding_digest(
        invocation_identity_digest=safe_key_digest(invocation.invocation_id),
        tool_name=invocation.tool_name,
        arguments_digest=invocation.arguments_digest,
        idempotency_key_digest=safe_key_digest(invocation.idempotency_key),
        resource_key_digest=safe_key_digest(invocation.resource_key),
        risk_level="HIGH",
        risk_facts=risk_facts,
    )
    request = ApprovalRequest(
        approval_id=uuid.uuid4().hex,
        run_id=lease.run_id,
        step_id="answer",
        invocation_id=invocation.invocation_id,
        tool_name=invocation.tool_name,
        invocation_identity_digest=safe_key_digest(invocation.invocation_id),
        arguments_digest=invocation.arguments_digest,
        idempotency_key_digest=safe_key_digest(invocation.idempotency_key),
        resource_key_digest=safe_key_digest(invocation.resource_key),
        risk_level="HIGH",
        risk_facts=risk_facts,
        invocation_binding_digest=binding,
        requested_at=datetime.now(UTC),
    )
    approval = DurableApprovalService(clean_database)
    await approval.create(request)
    decision = await approval.decide(
        run_id=lease.run_id,
        approval_id=request.approval_id,
        invocation_binding_digest=binding,
        decision=ApprovalDecisionValue.APPROVE,
    )
    assert decision.effective_status is ApprovalStatus.APPROVED

    service = DurableToolInvocationService(clean_database)
    assert await service.get(invocation.invocation_id) is None
    claim = await approval.claim_execution(
        lease=lease,
        approval_id=request.approval_id,
        invocation_binding_digest=binding,
    )
    assert await service.get(invocation.invocation_id) is None

    observed_states = []
    original_start = service.start
    original_committed = service.committed

    async def observe_start(**kwargs):
        prepared = await service.get(invocation.invocation_id)
        assert prepared is not None
        assert prepared.state is ToolInvocationState.PREPARED
        assert prepared.approval_id == request.approval_id
        assert prepared.execution_claim_id == claim.claim_id
        assert prepared.invocation_binding_digest == binding
        observed_states.append(prepared.state)
        started = await original_start(**kwargs)
        observed_states.append(started.state)
        return started

    async def observe_committed(**kwargs):
        committed = await original_committed(**kwargs)
        observed_states.append(committed.state)
        return committed

    service.start = observe_start  # type: ignore[method-assign]
    service.committed = observe_committed  # type: ignore[method-assign]
    context = RunContext.create(entry_agent_id="core_router", run_id=lease.run_id)
    context.attach_durable_lease(lease)
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    adapter = _CountingAdapter()

    outcome = await ToolExecutionService(durable_invocation_service=service).execute(
        invocation=invocation,
        adapter=adapter,
        run_context=context,
        step_id="answer",
        durable_approval_id=request.approval_id,
        durable_execution_claim_id=claim.claim_id,
        durable_binding_digest=binding,
    )

    assert not isinstance(outcome, ToolExecutionError)
    assert adapter.calls == 1
    assert observed_states == [
        ToolInvocationState.PREPARED,
        ToolInvocationState.STARTED,
        ToolInvocationState.COMMITTED,
    ]
    record = await service.get(invocation.invocation_id)
    assert record is not None
    assert record.state is ToolInvocationState.COMMITTED
    assert record.approval_id == request.approval_id
    assert record.execution_claim_id == claim.claim_id
    assert record.invocation_binding_digest == binding


@pytest.mark.asyncio
async def test_stale_fenced_executor_cannot_mutate_invocation(clean_database):
    run_id = uuid.uuid4().hex
    first = DurableRunControlService(clean_database, lease_seconds=1)
    second = DurableRunControlService(clean_database, lease_seconds=1)
    old = await first.claim(run_id, "wp5-owner-a")
    await asyncio.sleep(1.1)
    current = await second.claim(run_id, "wp5-owner-b")
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    with pytest.raises(OwnershipLost):
        await service.prepare(
            lease=old, step_id="answer", invocation=invocation, tool_name=invocation.tool_name
        )
    prepared = await service.prepare(
        lease=current, step_id="answer", invocation=invocation, tool_name=invocation.tool_name
    )
    assert prepared.fencing_token == current.fencing_token


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (ProviderReconciliationResult.COMMITTED, ToolInvocationState.COMMITTED),
        (ProviderReconciliationResult.NOT_COMMITTED, ToolInvocationState.NOT_COMMITTED),
        (ProviderReconciliationResult.STILL_PENDING, ToolInvocationState.UNKNOWN),
        (ProviderReconciliationResult.UNKNOWN, ToolInvocationState.UNKNOWN),
    ],
)
async def test_reconciliation_is_provider_specific_and_cas_safe(clean_database, result, expected):
    run = DurableRunControlService(clean_database)
    lease = await run.claim(uuid.uuid4().hex, "wp5-owner")
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    await service.prepare(lease=lease, step_id="answer", invocation=invocation, tool_name=invocation.tool_name)
    await service.start(lease=lease, invocation_id=invocation.invocation_id)
    await service.unknown(
        lease=lease, invocation_id=invocation.invocation_id, reason="RESPONSE_LOST", provider_operation_id="op-1"
    )
    provider = _Provider(result)
    reconciled = await service.reconcile(lease=lease, invocation=invocation, provider=provider)
    assert reconciled.state is expected
    assert provider.calls == 1
    again = await service.reconcile(lease=lease, invocation=invocation, provider=provider)
    assert again.state is expected
    assert provider.calls == 1 if expected in {ToolInvocationState.COMMITTED, ToolInvocationState.NOT_COMMITTED} else provider.calls == 2


@pytest.mark.asyncio
async def test_concurrent_reconciliation_has_one_effective_transition(clean_database):
    run = DurableRunControlService(clean_database)
    lease = await run.claim(uuid.uuid4().hex, "wp5-owner")
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    await service.prepare(lease=lease, step_id="answer", invocation=invocation, tool_name=invocation.tool_name)
    await service.start(lease=lease, invocation_id=invocation.invocation_id)
    await service.unknown(lease=lease, invocation_id=invocation.invocation_id, reason="CONNECTION_DROP")
    provider = _Provider(ProviderReconciliationResult.COMMITTED)
    results = await asyncio.gather(
        service.reconcile(lease=lease, invocation=invocation, provider=provider),
        service.reconcile(lease=lease, invocation=invocation, provider=provider),
    )
    assert {item.state for item in results} == {ToolInvocationState.COMMITTED}
    assert (await service.get(invocation.invocation_id)).version == 4


@pytest.mark.asyncio
async def test_concurrent_same_invocation_crosses_provider_boundary_once(clean_database):
    run = DurableRunControlService(clean_database)
    lease = await run.claim(uuid.uuid4().hex, "wp5-owner")
    context = RunContext.create(entry_agent_id="core_router", run_id=lease.run_id)
    context.attach_durable_lease(lease)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=2)))
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    adapter = _CountingAdapter(delay=0.02)
    original_prepare = service.prepare
    both_prepared = asyncio.Event()
    prepared_count = 0

    async def synchronized_prepare(**kwargs):
        nonlocal prepared_count
        record = await original_prepare(**kwargs)
        prepared_count += 1
        if prepared_count == 2:
            both_prepared.set()
        await both_prepared.wait()
        return record

    service.prepare = synchronized_prepare  # type: ignore[method-assign]
    execution = ToolExecutionService(durable_invocation_service=service)
    results = await asyncio.gather(
        execution.execute(invocation=invocation, adapter=adapter, run_context=context, step_id="answer"),
        execution.execute(invocation=invocation, adapter=adapter, run_context=context, step_id="answer"),
    )

    assert adapter.calls == 1
    assert sum(not isinstance(item, ToolExecutionError) for item in results) == 1
    assert (await service.get(invocation.invocation_id)).state is ToolInvocationState.COMMITTED


@pytest.mark.asyncio
async def test_takeover_reconciles_unknown_and_stale_owner_cannot_query(clean_database):
    run_id = uuid.uuid4().hex
    first = DurableRunControlService(clean_database, lease_seconds=1)
    second = DurableRunControlService(clean_database, lease_seconds=1)
    old = await first.claim(run_id, "wp5-owner-a")
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    await service.prepare(lease=old, step_id="answer", invocation=invocation, tool_name=invocation.tool_name)
    await service.start(lease=old, invocation_id=invocation.invocation_id)
    await asyncio.sleep(1.1)
    current = await second.claim(run_id, "wp5-owner-b")

    class _TakeoverProvider:
        calls = 0
        executions = 0

        async def reconcile_provider(self, _invocation, *, provider_operation_id):
            del _invocation, provider_operation_id
            row_at_query = await service.get(invocation.invocation_id)
            assert row_at_query.state is ToolInvocationState.UNKNOWN
            assert row_at_query.owner_id == current.owner_id
            assert row_at_query.fencing_token == current.fencing_token
            self.calls += 1
            return ProviderReconciliationResult.COMMITTED

    provider = _TakeoverProvider()

    with pytest.raises(OwnershipLost):
        await service.reconcile(lease=old, invocation=invocation, provider=provider)
    assert provider.calls == 0
    reconciled = await service.reconcile(lease=current, invocation=invocation, provider=provider)
    assert reconciled.state is ToolInvocationState.COMMITTED
    assert reconciled.owner_id == current.owner_id
    assert reconciled.fencing_token == current.fencing_token
    assert provider.calls == 1
    assert provider.executions == 0


@pytest.mark.asyncio
async def test_commit_then_response_loss_reconciles_without_second_execution(clean_database):
    run = DurableRunControlService(clean_database)
    lease = await run.claim(uuid.uuid4().hex, "wp5-owner")
    context = RunContext.create(entry_agent_id="core_router", run_id=lease.run_id)
    context.attach_durable_lease(lease)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=2)))
    service = DurableToolInvocationService(clean_database)
    adapter = _CountingAdapter()
    invocation = _invocation(failure_injection="FAIL_AFTER_SIDE_EFFECT")

    outcome = await ToolExecutionService(durable_invocation_service=service).execute(
        invocation=invocation, adapter=adapter, run_context=context, step_id="answer"
    )
    assert isinstance(outcome, ToolExecutionError)
    assert outcome.side_effect_state is ToolSideEffectState.UNKNOWN
    assert adapter.calls == 1
    assert (await service.get(invocation.invocation_id)).state is ToolInvocationState.UNKNOWN

    reconciled = await service.reconcile(lease=lease, invocation=invocation, provider=adapter)
    assert reconciled.state is ToolInvocationState.COMMITTED
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_local_commit_persistence_failure_reconciles_without_reexecution(clean_database):
    run = DurableRunControlService(clean_database)
    lease = await run.claim(uuid.uuid4().hex, "wp5-owner")
    context = RunContext.create(entry_agent_id="core_router", run_id=lease.run_id)
    context.attach_durable_lease(lease)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=2)))
    service = DurableToolInvocationService(clean_database)
    adapter = _CountingAdapter()
    invocation = _invocation()
    original_committed = service.committed
    fail_once = True

    async def committed_with_one_failure(**kwargs):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("injected local commit persistence failure")
        return await original_committed(**kwargs)

    service.committed = committed_with_one_failure  # type: ignore[method-assign]
    outcome = await ToolExecutionService(durable_invocation_service=service).execute(
        invocation=invocation, adapter=adapter, run_context=context, step_id="answer"
    )
    assert isinstance(outcome, ToolExecutionError)
    assert outcome.side_effect_state is ToolSideEffectState.UNKNOWN
    assert adapter.calls == 1
    assert (await service.get(invocation.invocation_id)).state is ToolInvocationState.UNKNOWN

    reconciled = await service.reconcile(lease=lease, invocation=invocation, provider=adapter)
    assert reconciled.state is ToolInvocationState.COMMITTED
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_missing_provider_reconciler_fails_closed_and_keeps_unknown(clean_database):
    run = DurableRunControlService(clean_database)
    lease = await run.claim(uuid.uuid4().hex, "wp5-owner")
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    await service.prepare(lease=lease, step_id="answer", invocation=invocation, tool_name=invocation.tool_name)
    await service.start(lease=lease, invocation_id=invocation.invocation_id)
    await service.unknown(lease=lease, invocation_id=invocation.invocation_id, reason="RESPONSE_LOST")

    with pytest.raises(AttributeError):
        await service.reconcile(lease=lease, invocation=invocation, provider=object())  # type: ignore[arg-type]
    assert (await service.get(invocation.invocation_id)).state is ToolInvocationState.UNKNOWN


@pytest.mark.asyncio
async def test_reconciliation_binding_mismatch_does_not_query_provider(clean_database):
    run = DurableRunControlService(clean_database)
    lease = await run.claim(uuid.uuid4().hex, "wp5-owner")
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    await service.prepare(lease=lease, step_id="answer", invocation=invocation, tool_name=invocation.tool_name)
    await service.start(lease=lease, invocation_id=invocation.invocation_id)
    await service.unknown(lease=lease, invocation_id=invocation.invocation_id, reason="RESPONSE_LOST")
    conflicting = ToolInvocation.create(
        tool_name=invocation.tool_name,
        invocation_id=invocation.invocation_id,
        idempotency_key=invocation.idempotency_key,
        resource_key=invocation.resource_key,
        arguments={"different": True},
    )
    provider = _Provider(ProviderReconciliationResult.COMMITTED)

    with pytest.raises(ValueError, match="immutable binding conflict"):
        await service.reconcile(lease=lease, invocation=conflicting, provider=provider)
    assert provider.calls == 0

    other_lease = await run.claim(uuid.uuid4().hex, "wp5-other-run-owner")
    with pytest.raises(ValueError, match="immutable binding conflict"):
        await service.reconcile(lease=other_lease, invocation=invocation, provider=provider)
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_prepare_linkage_is_immutable_and_exact_replay_does_not_mutate(clean_database):
    run = DurableRunControlService(clean_database)
    lease = await run.claim(uuid.uuid4().hex, "wp5-owner")
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    prepared = await service.prepare(
        lease=lease,
        step_id="answer",
        invocation=invocation,
        tool_name=invocation.tool_name,
        approval_id="approval-1",
        execution_claim_id="claim-1",
    )
    replayed = await service.prepare(
        lease=lease,
        step_id="answer",
        invocation=invocation,
        tool_name=invocation.tool_name,
        approval_id="approval-1",
        execution_claim_id="claim-1",
    )
    assert replayed == prepared

    context = RunContext.create(entry_agent_id="core_router", run_id=lease.run_id)
    context.attach_durable_lease(lease)
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    adapter = _CountingAdapter()
    with pytest.raises(ValueError, match="immutable binding conflict"):
        await ToolExecutionService(durable_invocation_service=service).execute(
            invocation=invocation,
            adapter=adapter,
            run_context=context,
            step_id="answer",
            durable_approval_id="approval-2",
            durable_execution_claim_id="claim-1",
        )
    assert adapter.calls == 0
    assert await service.get(invocation.invocation_id) == prepared


@pytest.mark.asyncio
async def test_same_run_tool_idempotency_key_rejects_conflicting_invocation(clean_database):
    run = DurableRunControlService(clean_database)
    lease = await run.claim(uuid.uuid4().hex, "wp5-owner")
    service = DurableToolInvocationService(clean_database)
    original = _invocation()
    await service.prepare(
        lease=lease,
        step_id="answer",
        invocation=original,
        tool_name=original.tool_name,
    )
    conflicting = ToolInvocation.create(
        tool_name=original.tool_name,
        idempotency_key=original.idempotency_key,
        resource_key=original.resource_key,
        arguments={"different": True},
    )
    adapter = _CountingAdapter()

    with pytest.raises(PersistenceError) as exc_info:
        await service.prepare(
            lease=lease,
            step_id="answer",
            invocation=conflicting,
            tool_name=conflicting.tool_name,
        )
    assert exc_info.value.error_code is DatabaseErrorCode.DATABASE_INTEGRITY_VIOLATION
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_prepared_invocation_cannot_be_marked_unknown(clean_database):
    run = DurableRunControlService(clean_database)
    lease = await run.claim(uuid.uuid4().hex, "wp5-owner")
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    await service.prepare(lease=lease, step_id="answer", invocation=invocation, tool_name=invocation.tool_name)

    with pytest.raises(ValueError, match="UNKNOWN 只能由 STARTED"):
        await service.unknown(lease=lease, invocation_id=invocation.invocation_id, reason="NOT_STARTED")
    assert (await service.get(invocation.invocation_id)).state is ToolInvocationState.PREPARED


@pytest.mark.asyncio
async def test_durable_lease_without_invocation_service_never_calls_provider(clean_database):
    run = DurableRunControlService(clean_database)
    lease = await run.claim(uuid.uuid4().hex, "wp5-owner")
    context = RunContext.create(entry_agent_id="core_router", run_id=lease.run_id)
    context.attach_durable_lease(lease)
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    adapter = _CountingAdapter()

    with pytest.raises(RuntimeError, match="缺少 invocation service"):
        await ToolExecutionService().execute(
            invocation=_invocation(), adapter=adapter, run_context=context, step_id="answer"
        )
    assert adapter.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_error", [False, True], ids=["timeout", "connection_error"])
async def test_durable_provider_failure_is_unknown_and_not_retried(clean_database, provider_error):
    run = DurableRunControlService(clean_database)
    lease = await run.claim(uuid.uuid4().hex, "wp5-owner")
    context = RunContext.create(entry_agent_id="core_router", run_id=lease.run_id)
    context.attach_durable_lease(lease)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=3, max_retries=2)))
    service = DurableToolInvocationService(clean_database)
    adapter = _CountingAdapter(delay=0 if provider_error else 0.6, provider_error=provider_error)
    invocation = _invocation(requested_timeout_seconds=None if provider_error else 0.3)

    outcome = await ToolExecutionService(durable_invocation_service=service).execute(
        invocation=invocation, adapter=adapter, run_context=context, step_id="answer"
    )

    assert isinstance(outcome, ToolExecutionError)
    assert outcome.side_effect_state is ToolSideEffectState.UNKNOWN
    assert outcome.retry_disposition is RetryDisposition.OUTCOME_UNKNOWN
    assert adapter.calls == 1
    assert (await service.get(invocation.invocation_id)).state is ToolInvocationState.UNKNOWN


@pytest.mark.asyncio
async def test_production_tool_execution_persists_commit_and_operation_correlation(clean_database):
    run = DurableRunControlService(clean_database)
    lease = await run.claim(uuid.uuid4().hex, "wp5-owner")
    context = RunContext.create(entry_agent_id="core_router", run_id=lease.run_id)
    context.attach_durable_lease(lease)
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    invocation = _invocation()
    service = DurableToolInvocationService(clean_database)
    outcome = await ToolExecutionService(
        durable_invocation_service=service
    ).execute(
        invocation=invocation,
        adapter=ComplexWorkflowToolAdapter(),
        run_context=context,
        step_id="answer",
    )
    assert not hasattr(outcome, "category")
    record = await service.get(invocation.invocation_id)
    assert record is not None
    assert record.state is ToolInvocationState.COMMITTED
    assert record.provider_operation_id == "wp5-operation"


def test_representative_side_effect_tool_is_production_registered():
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    registration = registry.require("complex_workflow_simulator")
    assert registration.adapter.spec.side_effect_kind.value != "NONE"
    assert hasattr(registration.adapter, "reconcile_provider")
