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
        ) or not 0 < self.timeout_seconds <= 3_600:
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

    async def submit(self, owner_user_id: uuid.UUID, request: EvaluationJobRequest) -> EvaluationJob:
        job_id = new_uuid7()
        event_id = new_uuid7()
        # WP5 Worker 以 job_id 作为稳定 Runtime run_id；调用方不能注入 run identity。
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
    "EVALUATION_JOB_QUEUED_EVENT",
    "EvaluationJob",
    "EvaluationJobRequest",
    "EvaluationJobService",
    "FinalizationResult",
    "JobError",
    "JobErrorCode",
    "JobStatus",
    "canonical_json_digest",
    "new_uuid7",
]
