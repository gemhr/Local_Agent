"""Generic durable continuation scheduling; resume and side-effect authorities stay elsewhere."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from inspect import isawaitable
from uuid import uuid4

from sqlalchemy import func, select, update

from core.persistence.database import Database
from core.persistence.models import DurableContinuationRow
from core.runtime.run_control import DurableRunControlService, OwnershipLost, RunLease


class ContinuationState(StrEnum):
    WAITING = "WAITING"
    READY = "READY"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ContinuationConflict(RuntimeError):
    """当前 claim、payload 或状态不能执行该 mutation。"""


def payload_digest(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class DurableContinuation:
    continuation_id: str
    continuation_kind: str
    run_id: str
    subject_type: str
    subject_id: str
    state: str
    payload: dict
    payload_digest: str
    claim_token: str | None
    claimed_by: str | None
    claim_deadline_at: datetime | None
    attempt_count: int
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


def _record(row) -> DurableContinuation:
    return DurableContinuation(*(getattr(row, field) for field in DurableContinuation.__dataclass_fields__))


class GenericContinuationService:
    """PostgreSQL-only scheduling owner. It never grants tool execution permission."""

    def __init__(self, database: Database, *, lease_seconds: int = 30, metrics=None,
                 run_control: DurableRunControlService | None = None):
        if not isinstance(database, Database):
            raise TypeError("database 必须是 Database")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须为正数")
        self.database, self.lease_seconds, self.metrics = database, lease_seconds, metrics
        self.run_control = run_control or DurableRunControlService(database)

    def _metric(self, name: str) -> None:
        if self.metrics is not None:
            try: self.metrics.increment_counter(name)
            except Exception: pass

    async def create(self, *, run_id: str, continuation_kind: str, subject_type: str,
                     subject_id: str, payload: dict, state: ContinuationState = ContinuationState.WAITING,
                     continuation_id: str | None = None) -> DurableContinuation:
        if not isinstance(payload, dict): raise TypeError("payload 必须是 dict")
        row = DurableContinuationRow(continuation_id=continuation_id or uuid4().hex, run_id=run_id,
            continuation_kind=continuation_kind, subject_type=subject_type, subject_id=subject_id,
            state=state.value, payload=json.loads(json.dumps(payload)), payload_digest=payload_digest(payload))
        async with self.database.transaction() as session:
            session.add(row); await session.flush(); await session.refresh(row)
            result = _record(row)
        self._metric("runtime_continuation_created_total")
        return result

    async def get(self, continuation_id: str) -> DurableContinuation | None:
        async with self.database.session() as session:
            row = await session.get(DurableContinuationRow, continuation_id)
            return None if row is None else _record(row)

    async def list_by_run(self, run_id: str) -> tuple[DurableContinuation, ...]:
        async with self.database.session() as session:
            rows = (await session.execute(
                select(DurableContinuationRow)
                .where(DurableContinuationRow.run_id == run_id)
                .order_by(DurableContinuationRow.created_at, DurableContinuationRow.continuation_id)
            )).scalars().all()
            return tuple(_record(row) for row in rows)

    async def mark_ready(self, continuation_id: str) -> DurableContinuation | None:
        async with self.database.transaction() as session:
            row = (await session.execute(select(DurableContinuationRow).where(DurableContinuationRow.continuation_id == continuation_id).with_for_update())).scalar_one_or_none()
            if row is None: return None
            if row.state == ContinuationState.WAITING: row.state = ContinuationState.READY; row.updated_at = func.now()
            elif row.state != ContinuationState.READY: raise ContinuationConflict("continuation 不是 WAITING/READY")
            await session.flush(); await session.refresh(row); return _record(row)

    async def claim_ready(self, worker_id: str, *, continuation_id: str | None = None) -> DurableContinuation | None:
        token = uuid4().hex
        async with self.database.transaction() as session:
            query = select(DurableContinuationRow).where(DurableContinuationRow.state == ContinuationState.READY)
            if continuation_id is not None: query = query.where(DurableContinuationRow.continuation_id == continuation_id)
            row = (await session.execute(query.order_by(DurableContinuationRow.created_at, DurableContinuationRow.continuation_id).limit(1).with_for_update(skip_locked=True))).scalar_one_or_none()
            if row is None: return None
            row.state, row.claim_token, row.claimed_by = ContinuationState.PROCESSING, token, worker_id
            row.claim_deadline_at, row.attempt_count, row.updated_at = func.now() + timedelta(seconds=self.lease_seconds), row.attempt_count + 1, func.now()
            await session.flush(); await session.refresh(row)
            self._metric("runtime_continuation_claimed_total")
            return _record(row)

    async def _claim_mutation(self, item: DurableContinuation, *, state=None, error=None,
                              renew=False, lease: RunLease | None = None) -> DurableContinuation:
        if not item.claim_token: raise ContinuationConflict("缺少 claim token")
        async with self.database.transaction() as session:
            if lease is not None:
                await self.run_control.assert_current_in_transaction(session, lease)
            values = {"updated_at": func.now()}
            if renew: values["claim_deadline_at"] = func.now() + timedelta(seconds=self.lease_seconds)
            if state is not None: values.update(state=state, claim_token=None, claimed_by=None, claim_deadline_at=None)
            if error is not None: values["last_error_code"] = error
            result = await session.execute(update(DurableContinuationRow).where(
                DurableContinuationRow.continuation_id == item.continuation_id,
                DurableContinuationRow.state == ContinuationState.PROCESSING,
                DurableContinuationRow.claim_token == item.claim_token,
            ).values(**values).returning(DurableContinuationRow))
            row = result.scalar_one_or_none()
            if row is None:
                self._metric("runtime_continuation_lease_lost_total")
                raise ContinuationConflict("claim token 已失效")
            return _record(row)

    async def heartbeat(self, item): return await self._claim_mutation(item, renew=True)
    async def complete(self, item, *, lease: RunLease | None = None):
        return await self._claim_mutation(item, state=ContinuationState.SUCCEEDED, lease=lease)
    async def fail(self, item, error_code: str, *, lease: RunLease | None = None):
        return await self._claim_mutation(
            item, state=ContinuationState.FAILED, error=error_code, lease=lease
        )

    async def reap_expired_once(self) -> int:
        async with self.database.transaction() as session:
            expired = (await session.execute(
                select(DurableContinuationRow.continuation_id, DurableContinuationRow.claim_token)
                .where(
                    DurableContinuationRow.state == ContinuationState.PROCESSING,
                    DurableContinuationRow.claim_deadline_at < func.now(),
                )
                .order_by(DurableContinuationRow.claim_deadline_at, DurableContinuationRow.continuation_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )).one_or_none()
            if expired is None:
                count = 0
            else:
                result = await session.execute(update(DurableContinuationRow).where(
                    DurableContinuationRow.continuation_id == expired.continuation_id,
                    DurableContinuationRow.state == ContinuationState.PROCESSING,
                    DurableContinuationRow.claim_token == expired.claim_token,
                    DurableContinuationRow.claim_deadline_at < func.now(),
                ).values(state=ContinuationState.READY, claim_token=None, claimed_by=None,
                         claim_deadline_at=None, updated_at=func.now()))
                count = int(result.rowcount or 0)
        if count: self._metric("runtime_continuation_reaped_total")
        return count

    async def resume_claimed(self, item: DurableContinuation, handler):
        if payload_digest(item.payload) != item.payload_digest:
            await self.fail(item, "CONTINUATION_PAYLOAD_DIGEST_MISMATCH")
            raise ContinuationConflict("continuation payload digest mismatch")
        lease: RunLease | None = None
        try:
            lease = await self.run_control.claim(
                item.run_id, item.claimed_by or "continuation-worker"
            )
            if await self.run_control.cancel_intent(item.run_id) is not None:
                await self._claim_mutation(
                    item, state=ContinuationState.CANCELLED,
                    error="RUN_CANCELLED", lease=lease,
                )
                return None, await self.get(item.continuation_id)
            await self.run_control.assert_current(lease)
            result = handler(item, lease)
            if isawaitable(result): result = await result
            await self.run_control.assert_current(lease)
            completed = await self.complete(item, lease=lease)
            self._metric("runtime_continuation_resume_success_total")
            return result, completed
        except ContinuationConflict:
            raise
        except Exception as exc:
            error = getattr(exc, "safe_error_code", None) or getattr(exc, "error_code", None)
            if hasattr(error, "value"):
                error = error.value
            if lease is not None:
                try:
                    await self.fail(
                        item, error or "CONTINUATION_RESUME_FAILED", lease=lease
                    )
                except (ContinuationConflict, OwnershipLost):
                    pass
            self._metric("runtime_continuation_resume_failure_total")
            raise
        finally:
            if lease is not None:
                await self.run_control.release(lease)


GenericDurableContinuation = DurableContinuation
GenericContinuationStore = GenericContinuationService

__all__ = ["ContinuationState", "ContinuationConflict", "DurableContinuation", "GenericDurableContinuation", "GenericContinuationService", "GenericContinuationStore", "payload_digest"]
