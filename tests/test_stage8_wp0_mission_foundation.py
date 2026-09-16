from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest
from sqlalchemy import select

from core.stage8 import (
    BusinessReview,
    BusinessReviewService,
    MissionService,
    MissionStatus,
    SpecialistAgentApplicationService,
    Stage8ConflictError,
    Stage8ValidationError,
)
from core.auth import AuthService, AuthorizationService
from core.persistence.models import RoleRow, UserRoleRow, UserRow
from core.redis_service import RedisTokenBucketRateLimiter
import server


_USER_ROLE_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _create_http_user(database) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with database.transaction() as session:
        role = await session.scalar(select(RoleRow).where(RoleRow.code == "USER"))
        if role is None:
            role = RoleRow(id=_USER_ROLE_ID, code="USER")
            session.add(role)
            await session.flush()
        session.add(UserRow(id=user_id, subject=str(user_id), display_name="stage8-test"))
        await session.flush()
        session.add(UserRoleRow(user_id=user_id, role_id=role.id))
    return user_id


def _http_token(private_key, user_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "test-issuer",
            "aud": "test-api",
            "sub": str(user_id),
            "roles": ["USER"],
            "jti": uuid.uuid4().hex,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=5),
            "scopes": [],
        },
        private_key,
        algorithm="EdDSA",
    )


@pytest.mark.asyncio
async def test_mission_lifecycle_and_run_reference(clean_database):
    missions = MissionService(clean_database)
    mission = await missions.create_mission("feature-1", title="Messaging")
    assert mission.status is MissionStatus.CREATED and mission.version == 1
    mission = await missions.transition_mission(mission.mission_id, "CONTEXT_READY", 1)
    assert mission.version == 2
    with pytest.raises(Stage8ValidationError):
        await missions.transition_mission(mission.mission_id, "COMPLETED", 2)
    reference = await missions.attach_run_reference(mission.mission_id, "run-1", "risk-analysis")
    assert reference.run_id == "run-1"
    assert len(await missions.list_run_references(mission.mission_id)) == 1


@pytest.mark.asyncio
async def test_mission_stale_version_is_rejected(clean_database):
    missions = MissionService(clean_database)
    mission = await missions.create_mission("feature-1")
    await missions.transition_mission(mission.mission_id, "CONTEXT_READY", 1)
    with pytest.raises(Stage8ConflictError):
        await missions.transition_mission(mission.mission_id, "AWAITING_REVIEW", 1)


@pytest.mark.asyncio
async def test_business_review_binding_and_idempotent_decision(clean_database):
    missions = MissionService(clean_database)
    mission = await missions.create_mission("feature-1")
    reviews = BusinessReviewService(clean_database)
    review = await reviews.create_review(mission.mission_id, "TEST_PLAN", subject_version=3, subject_digest="a" * 64)
    with pytest.raises(Stage8ConflictError, match="subject binding"):
        await reviews.approve_review(review.review_id, mission.mission_id)
    with pytest.raises(Stage8ConflictError):
        await reviews.approve_review(review.review_id, mission.mission_id, subject_version=4, subject_digest="a" * 64)
    with pytest.raises(Stage8ConflictError):
        await reviews.approve_review(review.review_id, mission.mission_id, subject_version=3, subject_digest="b" * 64)
    approved = await reviews.approve_review(review.review_id, mission.mission_id, subject_version=3, subject_digest="a" * 64)
    assert approved.status.value == "APPROVED"
    assert (await reviews.approve_review(review.review_id, mission.mission_id)).status.value == "APPROVED"
    with pytest.raises(Stage8ConflictError):
        await reviews.reject_review(review.review_id, mission.mission_id)


@pytest.mark.asyncio
async def test_business_review_concurrent_decisions_have_one_winner(clean_database):
    missions = MissionService(clean_database)
    mission = await missions.create_mission("feature-1")
    reviews = BusinessReviewService(clean_database)
    review = await reviews.create_review(
        mission.mission_id,
        "TEST_PLAN",
        subject_version=3,
        subject_digest="b" * 64,
    )

    results = await asyncio.gather(
        reviews.approve_review(
            review.review_id,
            mission.mission_id,
            subject_version=3,
            subject_digest="b" * 64,
        ),
        reviews.reject_review(
            review.review_id,
            mission.mission_id,
            subject_version=3,
            subject_digest="b" * 64,
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, BusinessReview) for result in results) == 1
    assert sum(isinstance(result, Stage8ConflictError) for result in results) == 1
    winner = next(result for result in results if isinstance(result, BusinessReview))
    assert (await reviews.get_review(review.review_id)).status is winner.status


@pytest.mark.asyncio
async def test_stage8_minimal_asgi_mission_lifecycle(clean_database, monkeypatch):
    async def specialist_runner(agent_id, prompt):
        assert agent_id == "feature_understanding"
        return '{"feature_id":"feature-http","summary":"typed API","change_points":[],"affected_components":[],"clarifications":[],"known_constraints":[],"evidence":[]}'

    user_id = await _create_http_user(clean_database)
    private_key = Ed25519PrivateKey.generate()
    settings = SimpleNamespace(
        jwt_public_key=private_key.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        ),
        jwt_issuer="test-issuer",
        jwt_audience="test-api",
        jwt_allowed_algorithm="EdDSA",
        jwt_clock_skew_seconds=0,
    )
    monkeypatch.setattr(
        server.app.state,
        "stage8_mission_service",
        MissionService(clean_database),
        raising=False,
    )
    monkeypatch.setattr(
        server.app.state,
        "stage8_review_service",
        BusinessReviewService(clean_database),
        raising=False,
    )
    monkeypatch.setattr(
        server.app.state,
        "stage8_specialist_service",
        SpecialistAgentApplicationService(runner=specialist_runner),
        raising=False,
    )
    monkeypatch.setattr(
        server.app.state,
        "auth_service",
        AuthService(clean_database, settings),
        raising=False,
    )
    monkeypatch.setattr(
        server.app.state,
        "authorization_service",
        AuthorizationService(clean_database),
        raising=False,
    )
    monkeypatch.setattr(
        server.app.state,
        "rate_limiter",
        RedisTokenBucketRateLimiter(
            SimpleNamespace(),
            SimpleNamespace(
                rate_limit_enabled=False,
                rate_limit_capacity=1,
                rate_limit_refill_rate=1.0,
            ),
        ),
        raising=False,
    )
    headers = {"Authorization": f"Bearer {_http_token(private_key, user_id)}"}
    transport = httpx.ASGITransport(app=server.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/stage8/missions",
            json={"feature_id": "feature-http", "title": "HTTP mission"},
            headers=headers,
        )
        assert created.status_code == 201
        mission_id = created.json()["mission_id"]

        fetched = await client.get(f"/api/stage8/missions/{mission_id}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "CREATED"

        specialist = await client.post(
            "/api/stage8/agents/feature-understanding/run",
            json={"context": {"feature_id": "feature-http"}},
            headers=headers,
        )
        assert specialist.status_code == 200
        assert specialist.json()["feature_id"] == "feature-http"

        transitioned = await client.post(
            f"/api/stage8/missions/{mission_id}/transition",
            json={"status": "CONTEXT_READY", "expected_version": 1},
            headers=headers,
        )
        assert transitioned.status_code == 200
        assert transitioned.json()["status"] == "CONTEXT_READY"
