"""Stage8-WP10 durable continuation for approved PRODUCT tickets."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from sqlalchemy import func

from core.runtime.approval import ApprovalDecisionValue, ApprovalStatus
from core.stage8 import repositories as repo
from core.stage8.service import Stage8ConflictError, Stage8NotFoundError
from core.runtime.continuation import GenericContinuationService, payload_digest


def _digest(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class TicketContinuation:
    continuation_id: str
    mission_id: str
    execution_job_id: str
    triage_id: str
    ticket_draft_id: str
    approval_id: str
    tool_invocation_id: str
    invocation_binding_digest: str
    request_digest: str
    request_snapshot: dict
    state: str
    external_ticket_id: str | None
    external_ticket_url: str | None
    error_code: str | None


def _record(row) -> TicketContinuation:
    return TicketContinuation(
        row.continuation_id, row.mission_id, row.execution_job_id, row.triage_id,
        row.ticket_draft_id, row.approval_id, row.tool_invocation_id,
        row.invocation_binding_digest, row.request_digest, row.request_snapshot,
        row.state, row.external_ticket_id, row.external_ticket_url, row.error_code,
    )


class TicketContinuationService:
    """Business continuation owner; approval and Tool Runtime remain their owners."""

    def __init__(self, database, *, tool_invoker=None, durable_approval=None, durable_run_control=None, owner_id="stage8-ticket-worker"):
        self.database = database
        self.tool_invoker = tool_invoker
        self.durable_approval = durable_approval
        self.durable_run_control = durable_run_control
        self.owner_id = owner_id

    async def create_from_approval(self, *, mission_id, execution_job_id, triage_id, ticket_draft_id, draft, approval):
        snapshot = dict(draft)
        request_digest = _digest(snapshot)
        async with self.database.transaction() as session:
            existing = await repo.get_ticket_continuation_by_approval(session, approval["approval_id"])
            if existing is not None:
                return _record(existing)
            row = await repo.add_ticket_continuation(session, {
                "continuation_id": uuid.uuid4().hex, "mission_id": mission_id,
                "execution_job_id": execution_job_id, "triage_id": triage_id,
                "ticket_draft_id": ticket_draft_id, "approval_id": approval["approval_id"],
                "tool_invocation_id": approval["invocation_id"],
                "invocation_binding_digest": approval["invocation_binding_digest"],
                "request_digest": request_digest, "request_snapshot": snapshot,
            })
            await repo.add_generic_continuation(session, {
                "continuation_id": row.continuation_id, "run_id": approval["run_id"],
                "continuation_kind": "STAGE8_TICKET", "subject_type": "ticket_draft",
                "subject_id": ticket_draft_id, "state": "WAITING", "payload": {
                    "approval_id": approval["approval_id"], "tool_invocation_id": approval["invocation_id"],
                    "invocation_binding_digest": approval["invocation_binding_digest"],
                    "request_digest": request_digest,
                }, "payload_digest": payload_digest({
                    "approval_id": approval["approval_id"], "tool_invocation_id": approval["invocation_id"],
                    "invocation_binding_digest": approval["invocation_binding_digest"], "request_digest": request_digest,
                })
            })
            return _record(row)

    async def on_approval_decision(self, approval_id: str, decision: ApprovalDecisionValue):
        approval_status = await self.durable_approval.status(approval_id)
        async with self.database.transaction() as session:
            row = await repo.get_ticket_continuation_by_approval(session, approval_id, for_update=True)
            if row is None:
                return None
            if row.state != "PENDING_APPROVAL":
                return _record(row)
            if approval_status is ApprovalStatus.APPROVED:
                row.state = "READY"
            elif approval_status is ApprovalStatus.REJECTED or (
                approval_status is not None
                and approval_status.name.startswith("INVALIDATED")
            ):
                row.state = "REJECTED"
            else:
                return _record(row)
            row.version += 1
            if row.state == "READY":
                generic = await repo.get_generic_continuation(session, row.continuation_id, for_update=True)
                if generic is not None and generic.state == "WAITING":
                    generic.state = "READY"; generic.updated_at = func.now()
            return _record(row)

    async def decide(self, continuation_id: str, decision: ApprovalDecisionValue, *, actor_id: str | None = None):
        """对 continuation 绑定的同一 Tool Approval 做 CAS 决策，不执行 Tool。"""
        continuation = await self.get(continuation_id)
        if continuation is None:
            raise Stage8NotFoundError("ticket continuation not found")
        approval = await self.durable_approval.get(continuation.approval_id)
        if approval is None:
            raise Stage8ConflictError("ticket approval not found")
        if (
            approval.invocation_id != continuation.tool_invocation_id
            or approval.invocation_binding_digest != continuation.invocation_binding_digest
        ):
            raise Stage8ConflictError("ticket approval binding mismatch")
        result = await self.durable_approval.decide(
            run_id=approval.run_id,
            approval_id=approval.approval_id,
            invocation_binding_digest=continuation.invocation_binding_digest,
            decision=decision,
            actor_id=actor_id,
        )
        if result.safe_error_code is not None:
            raise Stage8ConflictError(result.safe_error_code)
        updated = await self.on_approval_decision(continuation.approval_id, decision)
        if updated is None:
            raise Stage8ConflictError("ticket continuation disappeared")
        return updated

    async def get(self, continuation_id: str):
        async with self.database.session() as session:
            row = await repo.get_ticket_continuation(session, continuation_id)
            return None if row is None else _record(row)

    async def process_ready_once(self, continuation_id: str):
        async with self.database.transaction() as session:
            row = await repo.get_ticket_continuation(
                session, continuation_id, for_update=True
            )
            rows = [] if row is None else [row]
            selected = None
            for candidate in rows:
                approval = await self.durable_approval.get(candidate.approval_id)
                if approval is not None:
                    status = await self.durable_approval.status(candidate.approval_id)
                    if status is ApprovalStatus.REJECTED and candidate.state != "REJECTED":
                        candidate.state = "REJECTED"; candidate.version += 1
                    elif status is ApprovalStatus.APPROVED and candidate.state == "PENDING_APPROVAL":
                        candidate.state = "READY"; candidate.version += 1
                if candidate.state in {"READY", "PROCESSING", "SUCCEEDED"}:
                    selected = candidate
                    break
            if selected is None:
                return None
            continuation = _record(selected)
        run_control = self.durable_run_control or getattr(
            self.tool_invoker, "durable_run_control", None
        )
        generic = GenericContinuationService(
            self.database, lease_seconds=30, run_control=run_control
        )
        generic_claim = await generic.claim_ready(self.owner_id, continuation_id=continuation.continuation_id)
        if generic_claim is None:
            return None

        async def resume_ticket(_generic_item, lease):
            async with self.database.transaction() as session:
                selected = await repo.get_ticket_continuation(
                    session, continuation.continuation_id, for_update=True
                )
                if selected is None or selected.state not in {
                    "READY", "PROCESSING", "SUCCEEDED"
                }:
                    raise Stage8ConflictError("TICKET_CONTINUATION_STATE_CONFLICT")
                if selected.state == "SUCCEEDED":
                    return _record(selected)
                if selected.state == "READY":
                    selected.state = "PROCESSING"
                    selected.version += 1
                    selected.updated_at = func.now()
                await session.flush()
                current = _record(selected)

            if _digest(current.request_snapshot) != current.request_digest:
                error = Stage8ConflictError("ticket request snapshot digest mismatch")
                error.safe_error_code = "TICKET_REQUEST_DIGEST_MISMATCH"
                raise error
            result = await self.tool_invoker.resume_approved(current, lease)
            ticket_id = result.get("ticket_id")
            ticket_url = result.get("ticket_url")
            if (
                not isinstance(ticket_id, str) or not ticket_id
                or not isinstance(ticket_url, str) or not ticket_url
            ):
                error = ValueError("ticket platform result is missing ticket identity")
                error.safe_error_code = "TICKET_PLATFORM_RESULT_INVALID"
                raise error
            async with self.database.transaction() as session:
                await generic.run_control.assert_current_in_transaction(session, lease)
                stored = await repo.get_ticket_continuation(
                    session, current.continuation_id, for_update=True
                )
                if stored is None or stored.state != "PROCESSING":
                    raise Stage8ConflictError("TICKET_CONTINUATION_STATE_CONFLICT")
                stored.state = "SUCCEEDED"
                stored.external_ticket_id = ticket_id
                stored.external_ticket_url = ticket_url
                stored.version += 1
                return _record(stored)

        try:
            result, terminal = await generic.resume_claimed(
                generic_claim, resume_ticket
            )
        except Exception as exc:
            code = (
                getattr(exc, "safe_error_code", None)
                or getattr(exc, "error_code", None)
                or "TICKET_CONTINUATION_FAILED"
            )
            if hasattr(code, "value"):
                code = code.value
            async with self.database.transaction() as session:
                current = await repo.get_ticket_continuation(session, continuation.continuation_id, for_update=True)
                if current is not None and current.state in {"READY", "PROCESSING"}:
                    current.state = "UNKNOWN" if getattr(exc, "outcome_unknown", False) else "FAILED"
                    current.error_code = code
                    current.version += 1
            raise
        if terminal.state == "CANCELLED":
            async with self.database.transaction() as session:
                current = await repo.get_ticket_continuation(session, continuation.continuation_id, for_update=True)
                if current is not None and current.state in {"READY", "PROCESSING"}:
                    current.state = "FAILED"
                    current.error_code = "RUN_CANCELLED"
                    current.version += 1
            return await self.get(continuation.continuation_id)
        return result


__all__ = ["TicketContinuation", "TicketContinuationService"]
