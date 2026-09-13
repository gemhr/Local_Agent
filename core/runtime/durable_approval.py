"""PostgreSQL Authority for HITL approvals and the pre-execution claim."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select

from core.persistence.database import Database
from core.persistence.models import (
    DurableApprovalRow,
    DurableToolExecutionClaimRow,
)
from core.persistence.repositories import runtime as runtime_repository
from core.runtime.approval import (
    ApprovalCommandErrorCode,
    ApprovalCommandResult,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalRequest,
    ApprovalStatus,
    compute_actor_id_digest,
)
from core.runtime.run_control import OwnershipLost, RunLease


@dataclass(frozen=True, slots=True)
class DurableExecutionClaim:
    claim_id: str
    approval_id: str
    run_id: str
    invocation_binding_digest: str
    owner_id: str
    fencing_token: int
    created_at: datetime


def _status(row: DurableApprovalRow) -> ApprovalStatus:
    if row.state == "INVALIDATED":
        if row.invalidated_reason == "CANCELLED":
            return ApprovalStatus.INVALIDATED_CANCELLED
        if row.invalidated_reason == "RUN_TERMINAL":
            return ApprovalStatus.INVALIDATED_RUN_TERMINAL
        return ApprovalStatus.INVALIDATED_TIMEOUT
    return ApprovalStatus(row.state)


class DurableApprovalService:
    """Application-scoped approval aggregate owner; all mutations are PostgreSQL CAS."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("database 必须是 Database")
        self.database = database

    async def create(self, request: ApprovalRequest) -> ApprovalRequest:
        if not isinstance(request, ApprovalRequest):
            raise TypeError("request 必须是 ApprovalRequest")
        async with self.database.transaction() as session:
            await runtime_repository.lock_run_scope(session, request.run_id)
            existing = (await session.execute(select(DurableApprovalRow).where(
                DurableApprovalRow.run_id == request.run_id,
                DurableApprovalRow.invocation_id == request.invocation_id,
            ).with_for_update())).scalar_one_or_none()
            if existing is not None:
                if existing.invocation_binding_digest != request.invocation_binding_digest:
                    raise ValueError("同一 invocation 的 approval binding 冲突")
                return self._request(existing)
            session.add(DurableApprovalRow(
                approval_id=request.approval_id, run_id=request.run_id,
                step_id=request.step_id, invocation_id=request.invocation_id,
                tool_name=request.tool_name,
                invocation_identity_digest=request.invocation_identity_digest,
                arguments_digest=request.arguments_digest,
                idempotency_key_digest=request.idempotency_key_digest,
                resource_key_digest=request.resource_key_digest,
                invocation_binding_digest=request.invocation_binding_digest,
                risk_level=request.risk_level,
                risk_facts="|".join(sorted(request.risk_facts)),
            ))
            await session.flush()
        return request

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        async with self.database.session() as session:
            row = (await session.execute(select(DurableApprovalRow).where(DurableApprovalRow.approval_id == approval_id))).scalar_one_or_none()
            if row is None:
                return None
            return self._request(row)

    async def status(self, approval_id: str) -> ApprovalStatus | None:
        async with self.database.session() as session:
            row = (await session.execute(select(DurableApprovalRow).where(DurableApprovalRow.approval_id == approval_id))).scalar_one_or_none()
            if row is None:
                return None
            return _status(row)

    async def decide(self, *, run_id: str, approval_id: str, invocation_binding_digest: str, decision: ApprovalDecisionValue, actor_id: str | None = None) -> ApprovalCommandResult:
        async with self.database.transaction() as session:
            await runtime_repository.lock_run_scope(session, run_id)
            row = (await session.execute(select(DurableApprovalRow).where(DurableApprovalRow.approval_id == approval_id, DurableApprovalRow.run_id == run_id).with_for_update())).scalar_one_or_none()
            if row is None:
                return ApprovalCommandResult(run_id, approval_id, ApprovalStatus.PENDING, safe_error_code=ApprovalCommandErrorCode.UNKNOWN_APPROVAL.value)
            if row.invocation_binding_digest != invocation_binding_digest:
                return ApprovalCommandResult(run_id, approval_id, _status(row), safe_error_code=ApprovalCommandErrorCode.BINDING_MISMATCH.value)
            current = _status(row)
            if current is not ApprovalStatus.PENDING:
                if row.state == "INVALIDATED":
                    return ApprovalCommandResult(run_id, approval_id, current, safe_error_code=ApprovalCommandErrorCode.INVALIDATED.value)
                same = row.decision == ("APPROVE" if decision is ApprovalDecisionValue.APPROVE else "REJECT")
                return ApprovalCommandResult(run_id, approval_id, current, idempotent=same, safe_error_code=None if same else ApprovalCommandErrorCode.DECISION_CONFLICT.value, decided_at=row.decided_at)
            control = await self._run_control(session, run_id)
            if control is None or control.state != "ACTIVE" or control.cancel_command_id is not None:
                await self._invalidate_locked(row, "CANCELLED")
                return ApprovalCommandResult(run_id, approval_id, ApprovalStatus.INVALIDATED_CANCELLED, safe_error_code=ApprovalCommandErrorCode.INVALIDATED.value)
            now = datetime.now(UTC)
            row.state = "APPROVED" if decision is ApprovalDecisionValue.APPROVE else "REJECTED"
            row.decision = decision.value
            row.actor_id_digest = compute_actor_id_digest(actor_id)
            row.decided_at = now
            row.version += 1
            return ApprovalCommandResult(run_id, approval_id, ApprovalStatus(row.state), decided_at=now)

    async def invalidate_run(self, run_id: str, reason: str) -> tuple[ApprovalCommandResult, ...]:
        if reason not in {"CANCELLED", "DEADLINE_EXCEEDED", "RUN_TERMINAL"}:
            raise ValueError("invalid approval invalidation reason")
        async with self.database.transaction() as session:
            await runtime_repository.lock_run_scope(session, run_id)
            rows = (await session.execute(select(DurableApprovalRow).where(DurableApprovalRow.run_id == run_id, DurableApprovalRow.state == "PENDING").with_for_update())).scalars().all()
            result = []
            for row in rows:
                await self._invalidate_locked(row, reason)
                result.append(ApprovalCommandResult(run_id, row.approval_id, _status(row)))
            return tuple(result)

    async def claim_execution(self, *, lease: RunLease, approval_id: str, invocation_binding_digest: str) -> DurableExecutionClaim:
        async with self.database.transaction() as session:
            await runtime_repository.lock_run_scope(session, lease.run_id)
            from core.persistence.models import RunControlRow
            control = (await session.execute(select(RunControlRow).where(
                RunControlRow.run_id == lease.run_id,
                RunControlRow.owner_id == lease.owner_id,
                RunControlRow.fencing_token == lease.fencing_token,
                RunControlRow.state == "ACTIVE",
                RunControlRow.lease_until > func.now(),
            ).with_for_update())).scalar_one_or_none()
            if control is None:
                raise OwnershipLost("approval execution claim 的 Run fencing 已失效")
            approval = (await session.execute(select(DurableApprovalRow).where(DurableApprovalRow.approval_id == approval_id).with_for_update())).scalar_one_or_none()
            if approval is None or approval.run_id != lease.run_id or approval.invocation_binding_digest != invocation_binding_digest:
                raise ValueError("approval binding mismatch")
            if approval.state != "APPROVED":
                raise ValueError("approval is not approved")
            existing = (await session.execute(select(DurableToolExecutionClaimRow).where(DurableToolExecutionClaimRow.approval_id == approval_id))).scalar_one_or_none()
            if existing is not None:
                raise ValueError("execution claim already exists")
            now = datetime.now(UTC)
            claim = DurableToolExecutionClaimRow(claim_id=uuid4().hex, approval_id=approval_id, run_id=lease.run_id, invocation_binding_digest=invocation_binding_digest, owner_id=lease.owner_id, fencing_token=lease.fencing_token, created_at=now)
            session.add(claim)
            await session.flush()
            return DurableExecutionClaim(claim.claim_id, claim.approval_id, claim.run_id, claim.invocation_binding_digest, claim.owner_id, claim.fencing_token, now)

    @staticmethod
    async def _run_control(session, run_id):
        from core.persistence.models import RunControlRow
        return (await session.execute(select(RunControlRow).where(RunControlRow.run_id == run_id).with_for_update())).scalar_one_or_none()

    @staticmethod
    async def _invalidate_locked(row, reason: str) -> None:
        row.state = "INVALIDATED"
        row.invalidated_reason = reason
        row.invalidated_at = datetime.now(UTC)
        row.version += 1

    @staticmethod
    def _request(row) -> ApprovalRequest:
        return ApprovalRequest(row.approval_id, row.run_id, row.step_id, row.invocation_id, row.tool_name, row.invocation_identity_digest, row.arguments_digest, row.idempotency_key_digest, row.resource_key_digest, row.risk_level, tuple(filter(None, row.risk_facts.split("|"))), row.invocation_binding_digest, row.created_at)


__all__ = ["DurableApprovalService", "DurableExecutionClaim"]
