"""Stage8-WP10 durable continuation for approved PRODUCT tickets."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from core.runtime.approval import ApprovalDecisionValue, ApprovalStatus
from core.stage8 import repositories as repo


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
            return _record(row)

    async def on_approval_decision(self, approval_id: str, decision: ApprovalDecisionValue):
        async with self.database.transaction() as session:
            row = await repo.get_ticket_continuation_by_approval(session, approval_id, for_update=True)
            if row is None:
                return None
            if row.state != "PENDING_APPROVAL":
                return _record(row)
            row.state = "READY" if decision is ApprovalDecisionValue.APPROVE else "REJECTED"
            row.version += 1
            return _record(row)

    async def get(self, continuation_id: str):
        async with self.database.session() as session:
            row = await repo.get_ticket_continuation(session, continuation_id)
            return None if row is None else _record(row)

    async def process_ready_once(self, continuation_id: str | None = None):
        async with self.database.transaction() as session:
            if continuation_id is not None:
                row = await repo.get_ticket_continuation(session, continuation_id, for_update=True)
                rows = [] if row is None else [row]
            else:
                rows = await repo.list_ready_ticket_continuations(session)
            selected = None
            for candidate in rows:
                approval = await self.durable_approval.get(candidate.approval_id)
                if approval is not None:
                    status = await self.durable_approval.status(candidate.approval_id)
                    if status is ApprovalStatus.REJECTED and candidate.state != "REJECTED":
                        candidate.state = "REJECTED"; candidate.version += 1
                    elif status is ApprovalStatus.APPROVED and candidate.state == "PENDING_APPROVAL":
                        candidate.state = "READY"; candidate.version += 1
                if candidate.state == "READY":
                    selected = await repo.claim_ticket_continuation(session, candidate.continuation_id)
                    if selected is not None:
                        break
            if selected is None:
                return None
            continuation = _record(selected)
        try:
            if _digest(continuation.request_snapshot) != continuation.request_digest:
                raise ValueError("ticket request snapshot digest mismatch")
            result = await self.tool_invoker.resume_approved(continuation)
        except Exception as exc:
            # Tool Runtime owns UNKNOWN semantics; the worker never retries a
            # non-idempotent call after an uncertain outcome.
            code = getattr(exc, "safe_error_code", "TICKET_CONTINUATION_FAILED")
            async with self.database.transaction() as session:
                current = await repo.get_ticket_continuation(session, continuation.continuation_id, for_update=True)
                if current is not None:
                    current.state = "UNKNOWN" if getattr(exc, "outcome_unknown", False) else "FAILED"
                    current.error_code = code
                    current.version += 1
            raise
        ticket_id = result.get("ticket_id")
        ticket_url = result.get("ticket_url")
        if not isinstance(ticket_id, str) or not ticket_id or not isinstance(ticket_url, str) or not ticket_url:
            async with self.database.transaction() as session:
                current = await repo.get_ticket_continuation(session, continuation.continuation_id, for_update=True)
                if current is not None:
                    current.state = "FAILED"
                    current.error_code = "TICKET_PLATFORM_RESULT_INVALID"
                    current.version += 1
            raise ValueError("ticket platform result is missing ticket identity")
        async with self.database.transaction() as session:
            current = await repo.get_ticket_continuation(session, continuation.continuation_id, for_update=True)
            if current is None:
                return None
            current.state = "SUCCEEDED"
            current.external_ticket_id = ticket_id
            current.external_ticket_url = ticket_url
            current.version += 1
            return _record(current)


__all__ = ["TicketContinuation", "TicketContinuationService"]
