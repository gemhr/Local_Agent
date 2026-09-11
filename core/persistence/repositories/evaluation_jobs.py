#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Durable Evaluation Job / Result / Outbox 的窄 PostgreSQL repositories。

所有函数只使用调用方传入的 ``AsyncSession``。事务、提交与回滚始终由
Application Service 拥有。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.persistence.models import (
    ConsumerProcessedEventRow,
    EvaluationJobRow,
    EvaluationResultRow,
    OutboxEventRow,
)


@dataclass(frozen=True, slots=True)
class OutboxClaim:
    event_id: uuid.UUID
    event_type: str
    aggregate_type: str
    aggregate_id: uuid.UUID
    schema_version: int
    payload: dict[str, object]
    payload_digest: str
    attempt_count: int
    claim_owner: str
    claim_token: uuid.UUID
    claim_deadline: datetime
    traceparent: str | None = None
    tracestate: str | None = None
    was_reclaimed: bool = False


async def insert_job(session: AsyncSession, values: dict[str, object]) -> EvaluationJobRow:
    row = EvaluationJobRow(**values)
    session.add(row)
    await session.flush()
    return row


async def insert_result(session: AsyncSession, values: dict[str, object]) -> EvaluationResultRow:
    row = EvaluationResultRow(**values)
    session.add(row)
    await session.flush()
    return row


async def insert_outbox_event(session: AsyncSession, values: dict[str, object]) -> OutboxEventRow:
    row = OutboxEventRow(**values)
    session.add(row)
    await session.flush()
    return row


async def insert_processed_event(
    session: AsyncSession, values: dict[str, object]
) -> ConsumerProcessedEventRow:
    row = ConsumerProcessedEventRow(**values)
    session.add(row)
    await session.flush()
    return row


async def select_processed_event(
    session: AsyncSession, *, consumer_name: str, event_id: uuid.UUID
) -> ConsumerProcessedEventRow | None:
    return await session.get(ConsumerProcessedEventRow, (consumer_name, event_id))


async def select_job(session: AsyncSession, job_id: uuid.UUID) -> EvaluationJobRow | None:
    return await session.get(EvaluationJobRow, job_id)


async def select_result(session: AsyncSession, job_id: uuid.UUID) -> EvaluationResultRow | None:
    return await session.scalar(
        select(EvaluationResultRow).where(EvaluationResultRow.job_id == job_id)
    )


async def select_outbox_event(
    session: AsyncSession, event_id: uuid.UUID
) -> OutboxEventRow | None:
    return await session.get(OutboxEventRow, event_id)


async def cancel_queued_job(session: AsyncSession, job_id: uuid.UUID) -> EvaluationJobRow | None:
    return (
        await session.scalars(
            update(EvaluationJobRow)
            .where(EvaluationJobRow.id == job_id, EvaluationJobRow.status == "QUEUED")
            .values(
                status="CANCELLED",
                terminal_at=func.now(),
                updated_at=func.now(),
                version=EvaluationJobRow.version + 1,
            )
            .returning(EvaluationJobRow)
        )
    ).one_or_none()


async def start_queued_job(session: AsyncSession, job_id: uuid.UUID) -> EvaluationJobRow | None:
    return (
        await session.scalars(
            update(EvaluationJobRow)
            .where(EvaluationJobRow.id == job_id, EvaluationJobRow.status == "QUEUED")
            .values(
                status="RUNNING",
                started_at=func.now(),
                updated_at=func.now(),
                attempt=EvaluationJobRow.attempt + 1,
                version=EvaluationJobRow.version + 1,
            )
            .returning(EvaluationJobRow)
        )
    ).one_or_none()


async def claim_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    claim_owner: str,
    lease_seconds: float,
) -> tuple[EvaluationJobRow | None, uuid.UUID]:
    """Claim a queued job or reclaim an expired RUNNING lease using PG now()."""
    token = uuid.uuid4()
    statement = (
        update(EvaluationJobRow)
        .where(
            EvaluationJobRow.id == job_id,
            (
                (EvaluationJobRow.status == "QUEUED")
                | (
                    (EvaluationJobRow.status == "RUNNING")
                    & (
                        EvaluationJobRow.worker_claim_deadline.is_(None)
                        | (EvaluationJobRow.worker_claim_deadline <= func.now())
                    )
                )
            ),
        )
        .values(
            status="RUNNING",
            started_at=func.now(),
            updated_at=func.now(),
            attempt=EvaluationJobRow.attempt + 1,
            version=EvaluationJobRow.version + 1,
            worker_claim_owner=claim_owner,
            worker_claim_token=token,
            worker_claim_deadline=func.now()
            + text("CAST(:lease_seconds AS double precision) * interval '1 second'"),
        )
        .returning(EvaluationJobRow)
    )
    row = (await session.scalars(statement, {"lease_seconds": lease_seconds})).one_or_none()
    return row, token


async def succeed_running_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    claim_owner: str | None = None,
    claim_token: uuid.UUID | None = None,
) -> EvaluationJobRow | None:
    conditions = [EvaluationJobRow.id == job_id, EvaluationJobRow.status == "RUNNING"]
    if claim_owner is not None or claim_token is not None:
        conditions.extend(
            [
                EvaluationJobRow.worker_claim_owner == claim_owner,
                EvaluationJobRow.worker_claim_token == claim_token,
                EvaluationJobRow.worker_claim_deadline > func.now(),
            ]
        )
    return (
        await session.scalars(
            update(EvaluationJobRow)
            .where(*conditions)
            .values(
                status="SUCCEEDED",
                terminal_at=func.now(),
                updated_at=func.now(),
                version=EvaluationJobRow.version + 1,
                worker_claim_owner=None,
                worker_claim_token=None,
                worker_claim_deadline=None,
            )
            .returning(EvaluationJobRow)
        )
    ).one_or_none()


async def fail_running_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    failure_code: str,
    failure_message: str | None,
    claim_owner: str | None = None,
    claim_token: uuid.UUID | None = None,
) -> EvaluationJobRow | None:
    conditions = [EvaluationJobRow.id == job_id, EvaluationJobRow.status == "RUNNING"]
    if claim_owner is not None or claim_token is not None:
        conditions.extend(
            [
                EvaluationJobRow.worker_claim_owner == claim_owner,
                EvaluationJobRow.worker_claim_token == claim_token,
                EvaluationJobRow.worker_claim_deadline > func.now(),
            ]
        )
    return (
        await session.scalars(
            update(EvaluationJobRow)
            .where(*conditions)
            .values(
                status="FAILED",
                terminal_at=func.now(),
                updated_at=func.now(),
                failure_code=failure_code,
                failure_message=failure_message,
                version=EvaluationJobRow.version + 1,
                worker_claim_owner=None,
                worker_claim_token=None,
                worker_claim_deadline=None,
            )
            .returning(EvaluationJobRow)
        )
    ).one_or_none()


_CLAIM_SQL = text(
    """
    WITH candidates AS (
        SELECT event_id, claim_deadline IS NOT NULL AS was_reclaimed
        FROM outbox_events
        WHERE status = 'PENDING'
          AND published_at IS NULL
          AND available_at <= now()
          AND (claim_deadline IS NULL OR claim_deadline <= now())
        ORDER BY available_at ASC, created_at ASC, event_id ASC
        FOR UPDATE SKIP LOCKED
        LIMIT :batch_size
    )
    UPDATE outbox_events AS event
    SET claim_owner = :claim_owner,
        claim_token = :claim_token,
        claim_deadline = now() + CAST(:lease_seconds AS double precision) * interval '1 second'
    FROM candidates
    WHERE event.event_id = candidates.event_id
    RETURNING event.event_id, event.event_type, event.aggregate_type,
              event.aggregate_id, event.schema_version, event.payload,
              event.payload_digest, event.attempt_count, event.claim_owner,
              event.claim_token, event.claim_deadline, event.traceparent,
              event.tracestate, candidates.was_reclaimed
    """
)


async def claim_due_outbox_events(
    session: AsyncSession,
    *,
    claim_owner: str,
    lease_seconds: float,
    batch_size: int,
) -> tuple[OutboxClaim, ...]:
    claim_token = uuid.uuid4()
    result = await session.execute(
        _CLAIM_SQL,
        {
            "claim_owner": claim_owner,
            "claim_token": claim_token,
            "lease_seconds": lease_seconds,
            "batch_size": batch_size,
        },
    )
    return tuple(
        OutboxClaim(
            event_id=row.event_id,
            event_type=row.event_type,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            schema_version=int(row.schema_version),
            payload=dict(row.payload),
            payload_digest=row.payload_digest,
            attempt_count=int(row.attempt_count),
            claim_owner=row.claim_owner,
            claim_token=row.claim_token,
            claim_deadline=row.claim_deadline,
            traceparent=row.traceparent,
            tracestate=row.tracestate,
            was_reclaimed=bool(row.was_reclaimed),
        )
        for row in result.mappings().all()
    )


async def mark_outbox_published(session: AsyncSession, claim: OutboxClaim) -> bool:
    result = await session.execute(
        update(OutboxEventRow)
        .where(
            OutboxEventRow.event_id == claim.event_id,
            OutboxEventRow.status == "PENDING",
            OutboxEventRow.claim_owner == claim.claim_owner,
            OutboxEventRow.claim_token == claim.claim_token,
            OutboxEventRow.claim_deadline > func.now(),
        )
        .values(
            status="PUBLISHED",
            published_at=func.now(),
            claim_owner=None,
            claim_token=None,
            claim_deadline=None,
        )
    )
    return int(result.rowcount or 0) == 1


async def record_outbox_failure(
    session: AsyncSession,
    claim: OutboxClaim,
    *,
    backoff_seconds: float,
    error_code: str,
) -> bool:
    result = await session.execute(
        update(OutboxEventRow)
        .where(
            OutboxEventRow.event_id == claim.event_id,
            OutboxEventRow.status == "PENDING",
            OutboxEventRow.claim_owner == claim.claim_owner,
            OutboxEventRow.claim_token == claim.claim_token,
            OutboxEventRow.claim_deadline > func.now(),
        )
        .values(
            attempt_count=OutboxEventRow.attempt_count + 1,
            available_at=(
                func.now()
                + text("CAST(:backoff_seconds AS double precision) * interval '1 second'")
            ),
            last_error_code=error_code,
            claim_owner=None,
            claim_token=None,
            claim_deadline=None,
        ),
        {"backoff_seconds": backoff_seconds},
    )
    return int(result.rowcount or 0) == 1


__all__ = [
    "OutboxClaim",
    "cancel_queued_job",
    "claim_job",
    "claim_due_outbox_events",
    "fail_running_job",
    "insert_job",
    "insert_outbox_event",
    "insert_processed_event",
    "insert_result",
    "mark_outbox_published",
    "record_outbox_failure",
    "select_job",
    "select_outbox_event",
    "select_processed_event",
    "select_result",
    "start_queued_job",
    "succeed_running_job",
]
