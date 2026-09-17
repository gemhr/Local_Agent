"""Stage8-WP10：PRODUCT Ticket approval 后的 durable continuation。"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from core.persistence.models import DurableApprovalRow
from core.runtime import ToolExecutionService
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.approval import ApprovalDecisionValue, ApprovalStatus
from core.runtime.durable_approval import DurableApprovalService
from core.runtime.run_control import DurableRunControlService
from core.runtime.tool_governance import (
    ToolGovernanceService,
    ToolPolicyCatalog,
    register_default_tool_policies,
)
from core.runtime.tool_idempotency import DurableToolInvocationService
from core.runtime.tool_registry import ToolRegistry
from core.stage8 import TicketContinuationService
from core.stage8 import repositories as stage8_repo
from core.stage8 import FailureTriageResult, FailureTriageService, Stage8ExecutionService
from core.stage8.platforms import DeterministicMockPlatform
from core.stage8.execution import GovernedToolInvoker
from tools.registry import build_builtin_tool_registrations


class _NoResourceAuthorization:
    @staticmethod
    def extract(invocation):
        return None

    @staticmethod
    def require_authorized(request):
        raise AssertionError("Stage8 tools do not declare filesystem resources")


def _runtime(database, platform):
    registry = ToolRegistry()
    for registration in build_builtin_tool_registrations(platform):
        registry.register(registration)
    registry.freeze()
    catalog = ToolPolicyCatalog(
        tool_registry=registry, agent_registry=DEFAULT_AGENT_REGISTRY
    )
    register_default_tool_policies(catalog)
    catalog.freeze()
    approvals = DurableApprovalService(database)
    invoker = GovernedToolInvoker(
        registry,
        ToolGovernanceService(catalog, DEFAULT_AGENT_REGISTRY),
        ToolExecutionService(
            durable_invocation_service=DurableToolInvocationService(database)
        ),
        resource_authorization=_NoResourceAuthorization(),
        durable_run_control=DurableRunControlService(database),
        durable_approval=approvals,
        owner_id="stage8-wp10-test",
    )
    return invoker, approvals


async def _foreign_keys(database, suffix: str):
    mission_id = f"m-wp10-{suffix}"
    job_id = f"job-wp10-{suffix}"
    from core.stage8.service import MissionService

    await MissionService(database).create_mission("FEATURE-WP10", mission_id=mission_id)
    async with database.transaction() as session:
        await stage8_repo.add_execution_job(
            session,
            {
                "job_id": job_id,
                "mission_id": mission_id,
                "execution_id": f"EXEC-WP10-{suffix}",
                "plan_id": "plan-wp10",
                "case_id": "case-wp10",
                "environment_id": "env-wp10",
                "executor_id": "executor-wp10",
                "status": "FAILED",
                "plan_payload": {},
            },
        )
    return mission_id, job_id


async def _pending_continuation(database, suffix: str, *, draft=None):
    platform = DeterministicMockPlatform.seeded()
    invoker, approvals = _runtime(database, platform)
    mission_id, job_id = await _foreign_keys(database, suffix)
    draft = draft or {
        "title": f"WP10 failure {suffix}",
        "severity": "HIGH",
        "description": "assertion failed",
    }
    pending = await invoker(
        "stage8_create_ticket", draft, principal_agent_id="failure_triage"
    )
    continuation_service = TicketContinuationService(
        database,
        tool_invoker=invoker,
        durable_approval=approvals,
    )
    continuation = await continuation_service.create_from_approval(
        mission_id=mission_id,
        execution_job_id=job_id,
        triage_id=f"triage:{suffix}",
        ticket_draft_id=f"ticket-draft:{suffix}",
        draft=pending["request_snapshot"],
        approval=pending,
    )
    return platform, invoker, approvals, continuation_service, pending, continuation


async def _approve(approvals, continuation_service, pending, decision):
    result = await approvals.decide(
        run_id=pending["run_id"],
        approval_id=pending["approval_id"],
        invocation_binding_digest=pending["invocation_binding_digest"],
        decision=decision,
    )
    assert result.safe_error_code is None
    await continuation_service.on_approval_decision(pending["approval_id"], decision)
    return result


async def _approval_count(database) -> int:
    async with database.session() as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(DurableApprovalRow)
                )
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_approve_is_durable_then_worker_creates_one_ticket_and_writes_back(
    clean_database,
):
    platform, _, approvals, service, pending, continuation = await _pending_continuation(
        clean_database, "approve"
    )
    assert continuation.state == "PENDING_APPROVAL"
    assert platform.tickets == {}
    assert await _approval_count(clean_database) == 1

    await _approve(approvals, service, pending, ApprovalDecisionValue.APPROVE)
    approved = await service.get(continuation.continuation_id)
    assert approved.state == "READY"
    assert approved.external_ticket_id is None
    assert platform.tickets == {}

    completed = await service.process_ready_once(continuation.continuation_id)
    assert completed.state == "SUCCEEDED"
    assert completed.external_ticket_id in platform.tickets
    assert completed.external_ticket_url == (
        f"mock://tickets/{completed.external_ticket_id}"
    )
    assert len(platform.tickets) == 1
    assert await approvals.status(pending["approval_id"]) is ApprovalStatus.APPROVED
    assert await _approval_count(clean_database) == 1

    # Duplicate process-ready is a durable no-op; it cannot create Approval B
    # or a second non-idempotent external ticket.
    assert await service.process_ready_once(continuation.continuation_id) is None
    assert len(platform.tickets) == 1


@pytest.mark.asyncio
async def test_product_triage_creates_durable_pending_continuation(clean_database):
    platform = DeterministicMockPlatform.seeded()
    invoker, approvals = _runtime(clean_database, platform)
    mission_id, job_id = await _foreign_keys(clean_database, "triage")
    async with clean_database.transaction() as session:
        await stage8_repo.update_execution_job_by_id(
            session,
            job_id,
            {
                "plan_payload": {
                    "test_plan_subject_id": "plan-wp10",
                    "test_plan_version": 1,
                    "test_plan_digest": "d" * 64,
                },
                "result_payload": {
                    "expected_result": "ticket should not be needed",
                    "actual_result": "assertion failed",
                    "logs": ["assertion failed"],
                },
            },
        )

    class ProductTriage:
        async def failure_triage(self, request):
            return FailureTriageResult(
                classification="PRODUCT",
                confidence=0.95,
                evidence_ids=["EXEC_RESULT"],
                root_cause_hypothesis="product assertion failed",
                recommended_action="CREATE_TICKET",
                severity="HIGH",
                ticket_draft={
                    "title": "triaged PRODUCT failure",
                    "severity": "HIGH",
                    "description": "assertion failed",
                },
            )

    continuation_service = TicketContinuationService(
        clean_database, tool_invoker=invoker, durable_approval=approvals
    )
    triage_service = Stage8ExecutionService(
        clean_database,
        tool_invoker=invoker,
        triage_service=FailureTriageService(ProductTriage()),
        ticket_continuation_service=continuation_service,
    )
    result = await triage_service.triage_execution(f"EXEC-WP10-triage")
    assert result.classification == "PRODUCT"

    async with clean_database.session() as session:
        job = await stage8_repo.get_execution_job(session, f"EXEC-WP10-triage")
    continuation_id = job.triage_payload["ticket_continuation_id"]
    stored = await continuation_service.get(continuation_id)
    assert stored.state == "PENDING_APPROVAL"
    assert await approvals.status(stored.approval_id) is ApprovalStatus.PENDING
    assert await _approval_count(clean_database) == 1
    assert platform.tickets == {}


@pytest.mark.asyncio
async def test_approval_a_uses_frozen_snapshot_not_mutated_draft_b(clean_database):
    draft = {
        "title": "Payload A",
        "severity": "HIGH",
        "description": "A description",
    }
    platform, _, approvals, service, pending, continuation = await _pending_continuation(
        clean_database, "snapshot", draft=draft
    )
    draft.update(title="Payload B", description="B description")
    await _approve(approvals, service, pending, ApprovalDecisionValue.APPROVE)

    completed = await service.process_ready_once(continuation.continuation_id)
    assert completed.state == "SUCCEEDED"
    assert [ticket.title for ticket in platform.tickets.values()] == ["Payload A"]
    assert all(ticket.title != "Payload B" for ticket in platform.tickets.values())


@pytest.mark.asyncio
async def test_reject_is_durable_and_executes_zero_tools(clean_database):
    platform, _, approvals, service, pending, continuation = await _pending_continuation(
        clean_database, "reject"
    )
    await _approve(approvals, service, pending, ApprovalDecisionValue.REJECT)

    rejected = await service.get(continuation.continuation_id)
    assert rejected.state == "REJECTED"
    assert await service.process_ready_once(continuation.continuation_id) is None
    assert await service.process_ready_once() is None
    assert platform.tickets == {}


@pytest.mark.asyncio
async def test_two_workers_claim_once_and_create_one_external_ticket(clean_database):
    platform, invoker, approvals, service_a, pending, continuation = await _pending_continuation(
        clean_database, "concurrent"
    )
    await _approve(approvals, service_a, pending, ApprovalDecisionValue.APPROVE)
    service_b = TicketContinuationService(
        clean_database,
        tool_invoker=invoker,
        durable_approval=approvals,
        owner_id="stage8-wp10-worker-b",
    )

    results = await asyncio.gather(
        service_a.process_ready_once(continuation.continuation_id),
        service_b.process_ready_once(continuation.continuation_id),
        return_exceptions=True,
    )
    successful = [result for result in results if not isinstance(result, Exception)]
    assert len(platform.tickets) == 1
    assert len(successful) == 2
    assert sum(result is not None and result.state == "SUCCEEDED" for result in successful) == 1
    assert sum(result is None for result in successful) == 1


class _UnknownInvoker:
    def __init__(self):
        self.calls = 0

    async def resume_approved(self, continuation):
        self.calls += 1
        error = RuntimeError("provider response uncertain")
        error.outcome_unknown = True
        error.safe_error_code = "TICKET_PROVIDER_OUTCOME_UNKNOWN"
        raise error


@pytest.mark.asyncio
async def test_unknown_is_terminal_for_worker_and_never_blind_retries(clean_database):
    _, _, approvals, _, pending, continuation = await _pending_continuation(
        clean_database, "unknown"
    )
    unknown_invoker = _UnknownInvoker()
    service = TicketContinuationService(
        clean_database, tool_invoker=unknown_invoker, durable_approval=approvals
    )
    await _approve(approvals, service, pending, ApprovalDecisionValue.APPROVE)

    with pytest.raises(RuntimeError, match="uncertain"):
        await service.process_ready_once(continuation.continuation_id)
    assert (await service.get(continuation.continuation_id)).state == "UNKNOWN"
    assert await service.process_ready_once(continuation.continuation_id) is None
    assert unknown_invoker.calls == 1
