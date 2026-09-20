from __future__ import annotations

from datetime import UTC, datetime
import asyncio
from dataclasses import replace
import uuid

import pytest

from core.runtime.run_control import DurableRunControlService
from core.runtime.cancellation import RunCancelledError
from core.runtime.context import RunDeadlineExceededError
from core.runtime.retry import OperationIdempotency
from core.runtime.tool_adapters import ToolAdapter, ToolAdapterResponse
from core.runtime.tool_contract import (
    RetryDisposition,
    ToolExecutionResult,
    ToolExecutionSpec,
    ToolExecutionStatus,
    ToolInvocation,
    ToolSideEffectState,
    ToolSideEffectKind,
    build_tool_output,
)
from core.runtime.tool_execution import ToolExecutionService
from core.runtime.budget import BudgetLedger, RunBudget
from core.runtime.context import create_run_context
from core.persistence.repositories.execution import DurableExecutionRepository
from core.persistence.models import RunControlRow, RuntimeRunExecutionRow, RuntimeStepExecutionRow
from core.runtime.execution_aggregate import ExecutionRootInput
from core.runtime.client_event_feed import PostgresClientEventFeed
from core.runtime.event_journal_store import PostgresRunEventJournal
from core.runtime.planning import TaskCapabilityRequirements, create_single_step_plan
from sqlalchemy import update
from core.runtime.runtime_factory import CoordinatedRuntimeFactory
from tests._runtime_assembly_fixtures import FakeRouter, make_services
from core.runtime.tool_idempotency import (
    DurableToolInvocationService,
    ProviderReconciliationEvidence,
    ProviderReconciliationResult,
    ToolInvocationState,
)


def _invocation() -> ToolInvocation:
    return ToolInvocation.create(
        tool_name="reconcile_test_tool",
        invocation_id=uuid.uuid4().hex,
        idempotency_key=uuid.uuid4().hex,
        arguments={"operation_id": "reconcile-op"},
    )


def _committed_result(invocation: ToolInvocation) -> ToolExecutionResult:
    now = datetime.now(UTC)
    return ToolExecutionResult(
        invocation_id=invocation.invocation_id,
        attempt_id="attempt-1",
        tool_name=invocation.tool_name,
        status=ToolExecutionStatus.SUCCEEDED,
        output=build_tool_output('{"committed":true}', "application/json", 4096),
        safe_summary="committed",
        side_effect_state=ToolSideEffectState.COMMITTED,
        idempotency_replayed=False,
        retry_disposition=RetryDisposition.UNSAFE,
        resource_key_digest=None,
        started_at=now,
        completed_at=now,
        duration_ms=0,
    )


class _CountingSideEffectAdapter(ToolAdapter):
    spec = ToolExecutionSpec(
        tool_name="reconcile_test_tool",
        side_effect_kind=ToolSideEffectKind.LOCAL_STATE_MUTATION,
        idempotency=OperationIdempotency.IDEMPOTENT_WITH_KEY,
        supports_idempotency_replay=True,
        default_timeout_seconds=1,
        max_output_bytes=4096,
        max_concurrency=1,
    )

    def __init__(self):
        self.calls = 0

    def build_invocation(self, _argument_text):
        return _invocation()

    def invoke_once(self, _invocation, _context):
        self.calls += 1
        return ToolAdapterResponse(
            content="must-not-execute",
            content_type="text/plain",
            safe_summary="unexpected",
        )


def _execution_root(run_id: str) -> ExecutionRootInput:
    return ExecutionRootInput(
        run_id=run_id,
        resume_input={"entry_agent_id": "core_router", "user_query": "reconcile"},
        plan=create_single_step_plan("core_router", TaskCapabilityRequirements()),
        absolute_deadline=None,
        budget_totals={"max_model_calls": 1},
        budget_reserved={"model_calls": 0},
        budget_consumed={"model_calls": 0},
    )


async def _started_unknown(clean_database):
    control = DurableRunControlService(clean_database)
    lease = await control.claim(uuid.uuid4().hex, "wp2-batch-b")
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    await service.prepare(
        lease=lease,
        step_id="step",
        invocation=invocation,
        tool_name=invocation.tool_name,
    )
    await service.start(lease=lease, invocation_id=invocation.invocation_id)
    await service.unknown(
        lease=lease,
        invocation_id=invocation.invocation_id,
        reason="response-lost",
        provider_operation_id="provider-op-1",
    )
    return control, lease, service, invocation


@pytest.mark.asyncio
async def test_committed_lookup_persists_result_for_canonical_replay(clean_database):
    _control, lease, service, invocation = await _started_unknown(clean_database)
    result = _committed_result(invocation)

    class Provider:
        def reconcile_durable(self, record):
            assert record.state is ToolInvocationState.UNKNOWN
            return ProviderReconciliationEvidence(
                outcome=ProviderReconciliationResult.COMMITTED,
                result=result,
            )

    record = await service.reconcile_durable_record(
        lease=lease,
        provider=Provider(),
        invocation_id=invocation.invocation_id,
    )

    assert record.state is ToolInvocationState.COMMITTED
    assert record.committed_result is not None
    restored = service.restore_committed_result(record)
    assert restored.invocation_id == invocation.invocation_id
    assert restored.output.content == result.output.content


@pytest.mark.asyncio
async def test_committed_lookup_without_result_fails_closed_without_rewriting_run(clean_database):
    _control, lease, service, invocation = await _started_unknown(clean_database)

    class Provider:
        def reconcile_durable(self, _record):
            return ProviderReconciliationResult.COMMITTED

    record = await service.reconcile_durable_record(
        lease=lease,
        provider=Provider(),
        invocation_id=invocation.invocation_id,
    )

    assert record.state is ToolInvocationState.UNKNOWN
    assert record.manual_required is False
    assert record.last_safe_error_code == "RECONCILIATION_RESULT_MISSING"
    assert (await service.get(invocation.invocation_id)).state is ToolInvocationState.UNKNOWN


@pytest.mark.asyncio
async def test_committed_reconciliation_reuses_result_without_provider_execution(clean_database):
    control, lease, service, invocation = await _started_unknown(clean_database)
    result = _committed_result(invocation)

    class Provider:
        def reconcile_durable(self, _record):
            return ProviderReconciliationEvidence(ProviderReconciliationResult.COMMITTED, result)

    committed = await service.reconcile_durable_record(
        lease=lease, provider=Provider(), invocation_id=invocation.invocation_id
    )
    context, _ = create_run_context(entry_agent_id="core_router", run_id=lease.run_id)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=1)))
    context.attach_durable_lease(lease)
    context.attach_durable_tool_invocation(committed)
    adapter = _CountingSideEffectAdapter()

    replayed = await ToolExecutionService(durable_invocation_service=service).execute(
        invocation=invocation,
        adapter=adapter,
        run_context=context,
        step_id=committed.step_id,
    )

    assert isinstance(replayed, ToolExecutionResult)
    assert replayed.output.content == result.output.content
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_factory_rehydration_binds_committed_tool_to_canonical_replay(clean_database):
    run_id = uuid.uuid4().hex
    control = DurableRunControlService(clean_database)
    repository = DurableExecutionRepository(clean_database, control)
    lease = await control.claim(run_id, "factory-owner")
    root = _execution_root(run_id)
    step_id = root.plan.steps[0].step_id
    await repository.initialize(root, lease=lease)
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    await service.prepare(lease=lease, step_id=step_id, invocation=invocation, tool_name=invocation.tool_name)
    await service.start(lease=lease, invocation_id=invocation.invocation_id)
    await service.unknown(
        lease=lease,
        invocation_id=invocation.invocation_id,
        reason="response-lost",
        provider_operation_id="provider-op-factory",
    )
    expected = _committed_result(invocation)

    class Provider:
        def reconcile_durable(self, _record):
            return ProviderReconciliationEvidence(ProviderReconciliationResult.COMMITTED, expected)

    await service.reconcile_durable_record(
        lease=lease, provider=Provider(), invocation_id=invocation.invocation_id
    )
    async with clean_database.transaction() as session:
        await session.execute(
            update(RuntimeStepExecutionRow).where(
                RuntimeStepExecutionRow.run_id == run_id,
                RuntimeStepExecutionRow.step_id == step_id,
            ).values(status="RUNNING")
        )
    image = await repository.load(run_id)
    assert image is not None
    adapter = _CountingSideEffectAdapter()

    class ReplayRouter(FakeRouter):
        def complete_single_agent(self, agent_id, query, **kwargs):
            del agent_id, query
            replayed = ToolExecutionService(durable_invocation_service=service).execute_sync(
                invocation=invocation,
                adapter=adapter,
                run_context=kwargs["run_context"],
                step_id=step_id,
                event_emitter=kwargs.get("event_emitter"),
            )
            assert isinstance(replayed, ToolExecutionResult)
            return replayed.output.content

    router = ReplayRouter()
    # The canonical scope.execute() path publishes its terminal transition
    # through the durable journal/feed, just like production composition.
    # Keep the test on those concrete services instead of the lightweight
    # in-memory assembly fixture.
    journal = PostgresRunEventJournal(clean_database)
    feed = PostgresClientEventFeed(clean_database)
    services = replace(
        make_services(snapshot_enabled=False),
        durable_run_control=control,
        durable_tool_invocation=service,
        run_control_owner_id="factory-owner",
        event_journal=journal,
        client_event_feed=feed,
    )
    scope = await CoordinatedRuntimeFactory(
        router, services, execution_repository=repository
    ).create_rehydrated_run_scope(image, lease=lease, run_id=run_id)
    try:
        bound = scope.run_context.durable_tool_invocation
        assert bound is not None and bound.invocation_id == invocation.invocation_id
        terminal = await scope.execute()
        assert terminal.status.value == "SUCCEEDED"
        assert adapter.calls == 0
        persisted = await repository.load(run_id)
        assert persisted is not None
        assert persisted.root.status == "SUCCEEDED"
        assert all(str(row.status).rsplit(".", 1)[-1] == "SUCCEEDED" for row in persisted.steps)
        journal_records = await journal.read_after(run_id, 0, 100)
        assert sum(record.event_type.value == "RUN_COMPLETED" for record in journal_records) == 1
    finally:
        await scope.close()


def test_not_committed_handoff_uses_existing_tool_policy_only():
    invocation = _invocation()
    service = ToolExecutionService()
    safe_spec = _CountingSideEffectAdapter.spec
    assert service.reconciliation_retry_disposition(
        state=ToolInvocationState.NOT_COMMITTED,
        spec=safe_spec,
        invocation=invocation,
    ) is RetryDisposition.SAFE_WITH_IDEMPOTENCY_KEY
    blocked_spec = ToolExecutionSpec(
        tool_name=invocation.tool_name,
        side_effect_kind=ToolSideEffectKind.IRREVERSIBLE,
        idempotency=OperationIdempotency.NON_IDEMPOTENT,
        default_timeout_seconds=1,
        max_output_bytes=4096,
        max_concurrency=1,
    )
    assert service.reconciliation_retry_disposition(
        state=ToolInvocationState.NOT_COMMITTED,
        spec=blocked_spec,
        invocation=invocation,
    ) is RetryDisposition.UNSAFE
    assert service.reconciliation_retry_disposition(
        state=ToolInvocationState.UNKNOWN,
        spec=safe_spec,
        invocation=invocation,
    ) is RetryDisposition.OUTCOME_UNKNOWN


@pytest.mark.asyncio
async def test_cancel_and_deadline_stop_new_side_effect_before_adapter():
    adapter = _CountingSideEffectAdapter()
    context, source = create_run_context(entry_agent_id="core_router", timeout_seconds=1)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=1)))
    source.cancel()
    with pytest.raises(RunCancelledError):
        await ToolExecutionService().execute(
            invocation=_invocation(), adapter=adapter, run_context=context, step_id="step"
        )
    assert adapter.calls == 0

    adapter = _CountingSideEffectAdapter()
    context, _ = create_run_context(entry_agent_id="core_router", timeout_seconds=0.01)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=1)))
    await asyncio.sleep(0.02)
    with pytest.raises(RunDeadlineExceededError):
        await ToolExecutionService().execute(
            invocation=_invocation(), adapter=adapter, run_context=context, step_id="step"
        )
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_terminal_run_is_not_a_reconciliation_candidate(clean_database):
    run_id = uuid.uuid4().hex
    control = DurableRunControlService(clean_database)
    repository = DurableExecutionRepository(clean_database, control)
    lease = await control.claim(run_id, "terminal-owner")
    await repository.initialize(_execution_root(run_id), lease=lease)
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    await service.prepare(lease=lease, step_id="step", invocation=invocation, tool_name=invocation.tool_name)
    await service.start(lease=lease, invocation_id=invocation.invocation_id)
    async with clean_database.transaction() as session:
        await session.execute(
            update(RuntimeRunExecutionRow).where(RuntimeRunExecutionRow.run_id == run_id).values(status="SUCCEEDED")
        )
        await session.execute(
            update(RunControlRow).where(RunControlRow.run_id == run_id).values(state="CLOSED", owner_id=None, lease_until=None)
        )
    assert await repository.reconciliation_candidates() == ()
    row = await service.get(invocation.invocation_id)
    assert row is not None and row.state is ToolInvocationState.STARTED
