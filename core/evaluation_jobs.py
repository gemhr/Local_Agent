#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PostgreSQL-authoritative Durable Evaluation Job Application Service。"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from enum import Enum

from core.persistence.database import Database
from core.persistence.repositories import evaluation_jobs as repository


EVALUATION_JOB_REQUEST_SCHEMA = "runtime-evaluation-job.v1"
EVALUATION_JOB_QUEUED_EVENT = "EVALUATION_JOB_QUEUED"
EVALUATION_JOB_QUEUED_PAYLOAD_SCHEMA = "evaluation-job-queued.v1"
EVALUATION_JOB_TIMEOUT_MAX_SECONDS = 3_600.0


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobErrorCode(str, Enum):
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_NOT_CANCELLABLE = "JOB_NOT_CANCELLABLE"
    JOB_STATE_CONFLICT = "JOB_STATE_CONFLICT"


class JobError(Exception):
    def __init__(self, code: JobErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class EvaluationJobRequest:
    agent_id: str
    query: str
    timeout_seconds: float
    evaluator_kind: str = "RUNTIME_EVALUATION_V1"

    def __post_init__(self) -> None:
        if not self.agent_id or not self.query:
            raise ValueError("agent_id 和 query 必须非空")
        if not isinstance(self.timeout_seconds, (int, float)) or isinstance(
            self.timeout_seconds, bool
        ) or not 0 < self.timeout_seconds <= EVALUATION_JOB_TIMEOUT_MAX_SECONDS:
            raise ValueError("timeout_seconds 必须位于 0..3600")
        if self.evaluator_kind != "RUNTIME_EVALUATION_V1":
            raise ValueError("unsupported evaluator_kind")

    def to_payload(self, *, run_id: uuid.UUID) -> dict[str, object]:
        return {
            "schema_version": EVALUATION_JOB_REQUEST_SCHEMA,
            "agent_id": self.agent_id,
            "query": self.query,
            "run_id": str(run_id),
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class EvaluationJob:
    job_id: uuid.UUID
    owner_user_id: uuid.UUID
    status: JobStatus
    evaluator_kind: str
    request_payload: dict[str, object]
    request_digest: str
    attempt: int
    version: int
    failure_code: str | None
    failure_message: str | None


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    job: EvaluationJob
    applied: bool
    idempotent: bool


@dataclass(frozen=True, slots=True)
class WorkerClaim:
    job: EvaluationJob
    claim_owner: str
    claim_token: uuid.UUID


def canonical_json_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def new_uuid7() -> uuid.UUID:
    """生成 RFC 9562 UUIDv7；兼容尚未提供 ``uuid.uuid7`` 的 Python 3.12。"""
    timestamp_ms = int(time.time_ns() // 1_000_000) & ((1 << 48) - 1)
    random_bits = secrets.randbits(74)
    value = timestamp_ms << 80
    value |= 0x7 << 76
    value |= ((random_bits >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= random_bits & ((1 << 62) - 1)
    return uuid.UUID(int=value)


def _project_job(row: object) -> EvaluationJob:
    return EvaluationJob(
        job_id=row.id,
        owner_user_id=row.owner_user_id,
        status=JobStatus(row.status),
        evaluator_kind=row.evaluator_kind,
        request_payload=dict(row.request_payload),
        request_digest=row.request_digest,
        attempt=int(row.attempt),
        version=int(row.version),
        failure_code=row.failure_code,
        failure_message=row.failure_message,
    )


class EvaluationJobService:
    """Job/Result/Outbox 的事务 Owner。"""

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        """Canonical application Database owned by the enclosing composition root."""
        return self._database

    async def submit(self, owner_user_id: uuid.UUID, request: EvaluationJobRequest) -> EvaluationJob:
        job_id = new_uuid7()
        event_id = new_uuid7()
        # payload 中的 run_id 只是提交关联标识；worker 每次 claim 生成新的
        # Runtime execution identity，避免 lease reclaim 复用已终态 Journal。
        request_payload = request.to_payload(run_id=job_id)
        event_payload = {
            "schema_version": EVALUATION_JOB_QUEUED_PAYLOAD_SCHEMA,
            "event_id": str(event_id),
            "job_id": str(job_id),
        }
        async with self._database.transaction() as session:
            row = await repository.insert_job(
                session,
                {
                    "id": job_id,
                    "owner_user_id": owner_user_id,
                    "evaluator_kind": request.evaluator_kind,
                    "request_payload": request_payload,
                    "request_digest": canonical_json_digest(request_payload),
                    "status": JobStatus.QUEUED.value,
                },
            )
            await repository.insert_outbox_event(
                session,
                {
                    "event_id": event_id,
                    "event_type": EVALUATION_JOB_QUEUED_EVENT,
                    "aggregate_type": "EVALUATION_JOB",
                    "aggregate_id": job_id,
                    "schema_version": 1,
                    "payload": event_payload,
                    "payload_digest": canonical_json_digest(event_payload),
                    "status": "PENDING",
                },
            )
        return _project_job(row)

    async def get(self, job_id: uuid.UUID) -> EvaluationJob:
        async with self._database.session() as session:
            row = await repository.select_job(session, job_id)
        if row is None:
            raise JobError(JobErrorCode.JOB_NOT_FOUND)
        return _project_job(row)

    async def cancel(self, job_id: uuid.UUID) -> EvaluationJob:
        async with self._database.transaction() as session:
            row = await repository.cancel_queued_job(session, job_id)
            if row is None:
                current = await repository.select_job(session, job_id)
                if current is None:
                    raise JobError(JobErrorCode.JOB_NOT_FOUND)
                raise JobError(JobErrorCode.JOB_NOT_CANCELLABLE)
        return _project_job(row)

    async def start(self, job_id: uuid.UUID) -> EvaluationJob:
        async with self._database.transaction() as session:
            row = await repository.start_queued_job(session, job_id)
            if row is None:
                current = await repository.select_job(session, job_id)
                if current is None:
                    raise JobError(JobErrorCode.JOB_NOT_FOUND)
                raise JobError(JobErrorCode.JOB_STATE_CONFLICT)
        return _project_job(row)

    async def claim_for_worker(
        self, job_id: uuid.UUID, *, claim_owner: str, lease_seconds: float
    ) -> WorkerClaim | None:
        if not claim_owner or not 0 < lease_seconds <= 86_400:
            raise ValueError("worker claim 参数无效")
        async with self._database.transaction() as session:
            row, token = await repository.claim_job(
                session,
                job_id,
                claim_owner=claim_owner,
                lease_seconds=lease_seconds,
            )
            if row is None:
                return None
        return WorkerClaim(_project_job(row), claim_owner, token)

    async def validate_queued_event(
        self, *, event_id: uuid.UUID, job_id: uuid.UUID, payload: dict[str, object]
    ) -> bool:
        """Validate the untrusted Kafka trigger against the PG Outbox authority."""
        async with self._database.session() as session:
            event = await repository.select_outbox_event(session, event_id)
        # Broker ACK 先于 Publisher 在 PG 标记 PUBLISHED；因此 PENDING 是必须
        # 接受的 crash window。Outbox 意图本身（而非发布状态）是权威。
        if event is None or event.aggregate_id != job_id:
            return False
        return (
            event.event_type == EVALUATION_JOB_QUEUED_EVENT
            and event.aggregate_type == "EVALUATION_JOB"
            and event.schema_version == 1
            and event.payload == payload
            and event.payload_digest == canonical_json_digest(payload)
            and payload.get("event_id") == str(event_id)
            and payload.get("job_id") == str(job_id)
        )

    async def finalize_worker_success(
        self,
        *,
        job_id: uuid.UUID,
        claim_owner: str,
        claim_token: uuid.UUID,
        consumer_name: str,
        event_id: uuid.UUID,
        topic: str,
        partition: int,
        offset: int,
        result_payload: dict[str, object],
    ) -> FinalizationResult:
        """Complete result and consumer evidence in one PostgreSQL transaction."""
        if not result_payload.get("schema_version"):
            raise ValueError("result_payload.schema_version 必须为非空字符串")
        result_digest = canonical_json_digest(result_payload)
        async with self._database.transaction() as session:
            processed = await repository.select_processed_event(
                session, consumer_name=consumer_name, event_id=event_id
            )
            current = await repository.select_job(session, job_id)
            if current is None:
                raise JobError(JobErrorCode.JOB_NOT_FOUND)
            if processed is not None:
                return FinalizationResult(_project_job(current), False, True)
            if current.status == JobStatus.CANCELLED.value:
                await repository.insert_processed_event(
                    session,
                    {
                        "consumer_name": consumer_name,
                        "event_id": event_id,
                        "topic": topic,
                        "partition": partition,
                        "offset": offset,
                        "outcome": "CANCELLED_NOOP",
                    },
                )
                return FinalizationResult(_project_job(current), False, False)
            if current.status in {
                JobStatus.SUCCEEDED.value,
                JobStatus.FAILED.value,
            }:
                await repository.insert_processed_event(
                    session,
                    {
                        "consumer_name": consumer_name,
                        "event_id": event_id,
                        "topic": topic,
                        "partition": partition,
                        "offset": offset,
                        "outcome": "TERMINAL_NOOP",
                    },
                )
                return FinalizationResult(_project_job(current), False, False)
            row = await repository.succeed_running_job(
                session,
                job_id,
                claim_owner=claim_owner,
                claim_token=claim_token,
            )
            if row is None:
                raise JobError(JobErrorCode.JOB_STATE_CONFLICT)
            await repository.insert_result(
                session,
                {
                    "id": new_uuid7(),
                    "job_id": job_id,
                    "result_payload": result_payload,
                    "result_digest": result_digest,
                },
            )
            await repository.insert_processed_event(
                session,
                {
                    "consumer_name": consumer_name,
                    "event_id": event_id,
                    "topic": topic,
                    "partition": partition,
                    "offset": offset,
                    "outcome": "SUCCEEDED",
                },
            )
            return FinalizationResult(_project_job(row), True, False)

    async def record_worker_failure(
        self,
        *,
        job_id: uuid.UUID,
        claim_owner: str,
        claim_token: uuid.UUID,
        consumer_name: str,
        event_id: uuid.UUID,
        topic: str,
        partition: int,
        offset: int,
        failure_code: str,
        failure_message: str | None = None,
    ) -> EvaluationJob:
        async with self._database.transaction() as session:
            if await repository.select_processed_event(
                session, consumer_name=consumer_name, event_id=event_id
            ):
                current = await repository.select_job(session, job_id)
                if current is None:
                    raise JobError(JobErrorCode.JOB_NOT_FOUND)
                return _project_job(current)
            row = await repository.fail_running_job(
                session,
                job_id,
                failure_code=failure_code,
                failure_message=failure_message,
                claim_owner=claim_owner,
                claim_token=claim_token,
            )
            if row is None:
                raise JobError(JobErrorCode.JOB_STATE_CONFLICT)
            await repository.insert_processed_event(
                session,
                {
                    "consumer_name": consumer_name,
                    "event_id": event_id,
                    "topic": topic,
                    "partition": partition,
                    "offset": offset,
                    "outcome": "FAILED",
                },
            )
            return _project_job(row)

    async def record_processed_event_noop(
        self,
        *,
        job_id: uuid.UUID,
        consumer_name: str,
        event_id: uuid.UUID,
        topic: str,
        partition: int,
        offset: int,
    ) -> bool:
        """Record deterministic terminal/no-op evidence, or report a dedup hit."""
        async with self._database.transaction() as session:
            if await repository.select_processed_event(
                session, consumer_name=consumer_name, event_id=event_id
            ):
                return False
            current = await repository.select_job(session, job_id)
            if current is None:
                raise JobError(JobErrorCode.JOB_NOT_FOUND)
            if current.status not in {
                JobStatus.CANCELLED.value,
                JobStatus.SUCCEEDED.value,
                JobStatus.FAILED.value,
            }:
                raise JobError(JobErrorCode.JOB_STATE_CONFLICT)
            outcome = (
                "CANCELLED_NOOP"
                if current.status == JobStatus.CANCELLED.value
                else "TERMINAL_NOOP"
            )
            await repository.insert_processed_event(
                session,
                {
                    "consumer_name": consumer_name,
                    "event_id": event_id,
                    "topic": topic,
                    "partition": partition,
                    "offset": offset,
                    "outcome": outcome,
                },
            )
            return True

    async def complete_success(
        self, job_id: uuid.UUID, result_payload: dict[str, object]
    ) -> FinalizationResult:
        if not isinstance(result_payload.get("schema_version"), str) or not result_payload[
            "schema_version"
        ]:
            raise ValueError("result_payload.schema_version 必须为非空字符串")
        result_digest = canonical_json_digest(result_payload)
        async with self._database.transaction() as session:
            row = await repository.succeed_running_job(session, job_id)
            if row is not None:
                await repository.insert_result(
                    session,
                    {
                        "id": new_uuid7(),
                        "job_id": job_id,
                        "result_payload": result_payload,
                        "result_digest": result_digest,
                    },
                )
                return FinalizationResult(_project_job(row), True, False)

            current = await repository.select_job(session, job_id)
            if current is None:
                raise JobError(JobErrorCode.JOB_NOT_FOUND)
            if current.status == JobStatus.SUCCEEDED.value:
                existing = await repository.select_result(session, job_id)
                if existing is not None and existing.result_digest == result_digest:
                    return FinalizationResult(_project_job(current), False, True)
            raise JobError(JobErrorCode.JOB_STATE_CONFLICT)

    async def complete_failure(
        self,
        job_id: uuid.UUID,
        *,
        failure_code: str,
        failure_message: str | None = None,
    ) -> EvaluationJob:
        if not failure_code or len(failure_code) > 64:
            raise ValueError("failure_code 必须为 1..64 字符")
        if failure_message is not None and len(failure_message) > 512:
            raise ValueError("failure_message 最多 512 字符")
        async with self._database.transaction() as session:
            row = await repository.fail_running_job(
                session,
                job_id,
                failure_code=failure_code,
                failure_message=failure_message,
            )
            if row is None:
                current = await repository.select_job(session, job_id)
                if current is None:
                    raise JobError(JobErrorCode.JOB_NOT_FOUND)
                raise JobError(JobErrorCode.JOB_STATE_CONFLICT)
        return _project_job(row)


__all__ = [
    "EVALUATION_JOB_TIMEOUT_MAX_SECONDS",
    "EVALUATION_JOB_QUEUED_EVENT",
    "EvaluationJob",
    "EvaluationJobRequest",
    "EvaluationJobService",
    "FinalizationResult",
    "WorkerClaim",
    "JobError",
    "JobErrorCode",
    "JobStatus",
    "canonical_json_digest",
    "new_uuid7",
]
