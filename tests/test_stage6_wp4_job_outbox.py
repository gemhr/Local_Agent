from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import server
from core.auth import AuthService, AuthorizationService, Principal
from core.evaluation_jobs import (
    EvaluationJobRequest,
    EvaluationJobService,
    JobError,
    JobErrorCode,
)
from core.outbox_publisher import (
    OUTBOX_STALE_CLAIM,
    OutboxError,
    OutboxPublisherConfig,
    OutboxPublisherService,
    RecordingEventSink,
)
from core.persistence.models import (
    EvaluationJobRow,
    EvaluationResultRow,
    OutboxEventRow,
    RoleRow,
    UserRoleRow,
    UserRow,
)
from core.persistence.readiness import check_schema_readiness
from core.persistence.repositories import evaluation_jobs as repository
from core.redis_service import RedisTokenBucketRateLimiter


_ROLE_IDS = {
    "USER": uuid.UUID("00000000-0000-0000-0000-000000000001"),
    "ADMIN": uuid.UUID("00000000-0000-0000-0000-000000000003"),
}


async def _create_user(database, role: str = "USER") -> uuid.UUID:
    user_id = uuid.uuid4()
    async with database.transaction() as session:
        if await session.get(RoleRow, _ROLE_IDS[role]) is None:
            session.add(RoleRow(id=_ROLE_IDS[role], code=role))
            await session.flush()
        session.add(UserRow(id=user_id, subject=str(user_id), display_name="test"))
        await session.flush()
        session.add(UserRoleRow(user_id=user_id, role_id=_ROLE_IDS[role]))
    return user_id


def _principal(user_id: uuid.UUID, role: str = "USER") -> Principal:
    now = datetime.now(UTC)
    return Principal(
        user_id,
        str(user_id),
        frozenset({role}),
        uuid.uuid4().hex,
        now,
        now + timedelta(minutes=5),
    )


def _request(suffix: str = "a") -> EvaluationJobRequest:
    return EvaluationJobRequest(
        agent_id=f"agent-{suffix}", query=f"question-{suffix}", timeout_seconds=30.0
    )


@pytest.mark.asyncio
async def test_submission_is_atomic_and_payload_is_minimal(clean_database, monkeypatch):
    readiness = await check_schema_readiness(clean_database)
    assert readiness.ready and readiness.alembic_revision == readiness.alembic_head
    owner = await _create_user(clean_database)
    service = EvaluationJobService(clean_database)
    job = await service.submit(owner, _request())
    assert job.job_id.version == 7
    assert job.request_payload["run_id"] == str(job.job_id)

    async with clean_database.session() as session:
        event = await session.scalar(select(OutboxEventRow))
        assert event is not None
        assert set(event.payload) == {"schema_version", "event_id", "job_id"}
        assert event.payload["event_id"] == str(event.event_id)
        assert event.payload["job_id"] == str(job.job_id)
        assert "question-a" not in str(event.payload)

    original_outbox_insert = repository.insert_outbox_event

    async def fail_outbox(*_args, **_kwargs):
        raise RuntimeError("injected outbox failure")

    monkeypatch.setattr(repository, "insert_outbox_event", fail_outbox)
    with pytest.raises(RuntimeError):
        await service.submit(owner, _request("rollback"))
    monkeypatch.setattr(repository, "insert_outbox_event", original_outbox_insert)

    original_job_insert = repository.insert_job

    async def fail_job(*_args, **_kwargs):
        raise RuntimeError("injected job failure")

    monkeypatch.setattr(repository, "insert_job", fail_job)
    with pytest.raises(RuntimeError):
        await service.submit(owner, _request("no-outbox"))
    monkeypatch.setattr(repository, "insert_job", original_job_insert)

    async with clean_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(EvaluationJobRow)) == 1
        assert await session.scalar(select(func.count()).select_from(OutboxEventRow)) == 1


@pytest.mark.asyncio
async def test_job_state_races_and_result_atomicity(clean_database, monkeypatch):
    owner = await _create_user(clean_database)
    service = EvaluationJobService(clean_database)

    start_job = await service.submit(owner, _request("start-race"))
    starts = await asyncio.gather(
        service.start(start_job.job_id),
        service.start(start_job.job_id),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in starts) == 1
    assert sum(
        isinstance(item, JobError) and item.code is JobErrorCode.JOB_STATE_CONFLICT
        for item in starts
    ) == 1

    cancel_job = await service.submit(owner, _request("cancel-race"))
    race = await asyncio.gather(
        service.start(cancel_job.job_id),
        service.cancel(cancel_job.job_id),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in race) == 1
    terminal = await service.get(cancel_job.job_id)
    assert terminal.status.value in {"RUNNING", "CANCELLED"}
    with pytest.raises(JobError) as non_queued:
        await service.cancel(cancel_job.job_id)
    assert non_queued.value.code is JobErrorCode.JOB_NOT_CANCELLABLE

    rollback_job = await service.submit(owner, _request("result-rollback"))
    await service.start(rollback_job.job_id)
    original_result_insert = repository.insert_result

    async def fail_result(*_args, **_kwargs):
        raise RuntimeError("injected result failure")

    monkeypatch.setattr(repository, "insert_result", fail_result)
    with pytest.raises(RuntimeError):
        await service.complete_success(
            rollback_job.job_id, {"schema_version": "result.v1"}
        )
    monkeypatch.setattr(repository, "insert_result", original_result_insert)
    assert (await service.get(rollback_job.job_id)).status.value == "RUNNING"

    first = await service.complete_success(start_job.job_id, {"schema_version": "result.v1", "score": 1})
    duplicate = await service.complete_success(start_job.job_id, {"schema_version": "result.v1", "score": 1})
    assert first.applied and duplicate.idempotent
    with pytest.raises(JobError) as conflict:
        await service.complete_success(start_job.job_id, {"schema_version": "result.v1", "score": 2})
    assert conflict.value.code is JobErrorCode.JOB_STATE_CONFLICT
    async with clean_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(EvaluationResultRow)) == 1


@pytest.mark.asyncio
async def test_outbox_skip_locked_claims_do_not_overlap(clean_database):
    owner = await _create_user(clean_database)
    service = EvaluationJobService(clean_database)
    await service.submit(owner, _request("one"))
    await service.submit(owner, _request("two"))

    publisher_a = OutboxPublisherService(
        clean_database,
        RecordingEventSink(),
        OutboxPublisherConfig(claim_owner="publisher-a", batch_size=1),
    )
    publisher_b = OutboxPublisherService(
        clean_database,
        RecordingEventSink(),
        OutboxPublisherConfig(claim_owner="publisher-b", batch_size=1),
    )
    claims_a, claims_b = await asyncio.gather(publisher_a.claim(), publisher_b.claim())
    assert len(claims_a) == len(claims_b) == 1
    assert claims_a[0].event_id != claims_b[0].event_id
    assert claims_a[0].claim_owner != claims_b[0].claim_owner

    await service.submit(owner, _request("locked"))
    await service.submit(owner, _request("skipped"))
    async with clean_database.session() as lock_session:
        async with lock_session.begin():
            locked = await lock_session.scalar(
                select(OutboxEventRow)
                .where(OutboxEventRow.claim_owner.is_(None))
                .order_by(OutboxEventRow.created_at, OutboxEventRow.event_id)
                .with_for_update()
                .limit(1)
            )
            assert locked is not None
            skipped_claim = (await publisher_b.claim())[0]
            assert skipped_claim.event_id != locked.event_id

    class LockObservingSink:
        def __init__(self):
            self.lock_acquired = False

        async def publish(self, event):
            async with clean_database.transaction(lock_timeout_ms=100) as session:
                row = await session.scalar(
                    select(OutboxEventRow)
                    .where(OutboxEventRow.event_id == event.event_id)
                    .with_for_update(nowait=True)
                )
                self.lock_acquired = row is not None

    await service.submit(owner, _request("outside-transaction"))
    observing_sink = LockObservingSink()
    observing_publisher = OutboxPublisherService(
        clean_database,
        observing_sink,
        OutboxPublisherConfig(claim_owner="publisher-observer", batch_size=1),
    )
    assert await observing_publisher.run_once() == 1
    assert observing_sink.lock_acquired


@pytest.mark.asyncio
async def test_lease_reclaim_stale_fencing_and_at_least_once(clean_database, monkeypatch):
    owner = await _create_user(clean_database)
    job = await EvaluationJobService(clean_database).submit(owner, _request("crash"))
    sink = RecordingEventSink()
    publisher_a = OutboxPublisherService(
        clean_database,
        sink,
        OutboxPublisherConfig(
            claim_owner="stable-publisher-id", batch_size=1, lease_seconds=0.03
        ),
    )
    publisher_b = OutboxPublisherService(
        clean_database,
        sink,
        OutboxPublisherConfig(
            claim_owner="stable-publisher-id", batch_size=1, lease_seconds=2.0
        ),
    )

    first = (await publisher_a.claim())[0]
    await sink.publish(first)  # 外部 publish 成功后模拟 mark 前 crash。
    await asyncio.sleep(0.05)
    second = (await publisher_b.claim())[0]
    assert second.event_id == first.event_id
    assert second.claim_token != first.claim_token
    with pytest.raises(OutboxError) as stale:
        await publisher_a.mark_published(first)
    assert stale.value.code == OUTBOX_STALE_CLAIM
    await sink.publish(second)
    await publisher_b.mark_published(second)

    assert [event.event_id for event in sink.events] == [first.event_id, first.event_id]
    assert first.aggregate_id == second.aggregate_id == job.job_id
    async with clean_database.session() as session:
        event = await session.get(OutboxEventRow, first.event_id)
        assert event is not None and event.status == "PUBLISHED"

    bounded_config = OutboxPublisherConfig(
        claim_owner="publisher-bounded-backoff",
        retry_base_seconds=1.0,
        retry_max_seconds=1.0,
        retry_jitter_ratio=1.0,
    )
    bounded_publisher = OutboxPublisherService(clean_database, sink, bounded_config)
    monkeypatch.setattr("core.outbox_publisher.random.uniform", lambda *_args: 2.0)
    assert bounded_publisher._backoff_seconds(30) == 1.0
    with pytest.raises(ValueError):
        OutboxPublisherConfig(claim_owner="invalid-lease", lease_seconds=float("nan"))

    await EvaluationJobService(clean_database).submit(owner, _request("retry"))

    class FailingSink:
        async def publish(self, _event):
            raise RuntimeError("sink unavailable")

    failing_publisher = OutboxPublisherService(
        clean_database,
        FailingSink(),
        OutboxPublisherConfig(
            claim_owner="publisher-failing",
            batch_size=1,
            retry_base_seconds=1.0,
            retry_max_seconds=1.0,
        ),
    )
    assert await failing_publisher.run_once() == 0
    async with clean_database.session() as session:
        failed = await session.scalar(
            select(OutboxEventRow).where(OutboxEventRow.status == "PENDING")
        )
        assert failed is not None
        assert failed.attempt_count == 1
        assert failed.available_at > failed.created_at
        assert failed.claim_owner is None and failed.claim_token is None
        assert failed.last_error_code == "OUTBOX_PUBLISH_FAILED"


def _token(private_key, user_id: uuid.UUID, role: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "test-issuer",
            "aud": "test-api",
            "sub": str(user_id),
            "roles": [role],
            "jti": uuid.uuid4().hex,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=2),
        },
        private_key,
        algorithm="EdDSA",
    )


@pytest.mark.asyncio
async def test_http_job_owner_is_principal_derived_and_admin_can_manage(clean_database, monkeypatch):
    user_a = await _create_user(clean_database)
    user_b = await _create_user(clean_database)
    admin = await _create_user(clean_database, "ADMIN")
    private_key = Ed25519PrivateKey.generate()
    auth_settings = SimpleNamespace(
        jwt_public_key=private_key.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        ),
        jwt_issuer="test-issuer",
        jwt_audience="test-api",
        jwt_allowed_algorithm="EdDSA",
        jwt_clock_skew_seconds=0,
    )
    limiter = RedisTokenBucketRateLimiter(
        None,
        SimpleNamespace(
            rate_limit_enabled=False,
            rate_limit_capacity=1,
            rate_limit_refill_rate=1.0,
        ),
    )
    monkeypatch.setattr(server.app.state, "auth_service", AuthService(clean_database, auth_settings), raising=False)
    monkeypatch.setattr(server.app.state, "authorization_service", AuthorizationService(clean_database), raising=False)
    monkeypatch.setattr(server.app.state, "evaluation_job_service", EvaluationJobService(clean_database), raising=False)
    monkeypatch.setattr(server.app.state, "rate_limiter", limiter, raising=False)

    client = TestClient(server.app)
    headers_a = {"Authorization": f"Bearer {_token(private_key, user_a, 'USER')}"}
    headers_b = {"Authorization": f"Bearer {_token(private_key, user_b, 'USER')}"}
    headers_admin = {"Authorization": f"Bearer {_token(private_key, admin, 'ADMIN')}"}
    created = client.post(
        "/api/evaluation/jobs",
        headers=headers_a,
        json={"agent_id": "agent", "query": "safe", "timeout_seconds": 30},
    )
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    spoof = client.post(
        "/api/evaluation/jobs",
        headers=headers_a,
        json={
            "agent_id": "agent",
            "query": "safe",
            "timeout_seconds": 30,
            "owner_user_id": str(user_b),
        },
    )
    assert spoof.status_code == 422
    hidden = client.get(f"/api/evaluation/jobs/{job_id}", headers=headers_b)
    missing = client.get(
        f"/api/evaluation/jobs/{uuid.uuid4()}", headers=headers_b
    )
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json()["error"]["code"] == missing.json()["error"]["code"]
    assert hidden.json()["error"]["message"] == missing.json()["error"]["message"]
    assert client.get(f"/api/evaluation/jobs/{job_id}", headers=headers_admin).status_code == 200
    cancelled = client.post(f"/api/evaluation/jobs/{job_id}/cancel", headers=headers_a)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
