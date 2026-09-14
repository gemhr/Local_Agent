from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
from sqlalchemy import select

from core.auth import AuthError, AuthService, AuthorizationService, Principal
from core.persistence.models import ObjectOwnershipRow, RoleRow, UserRoleRow, UserRow
from core.redis_service import RedisTokenBucketRateLimiter
import server
from tests._runtime_assembly_fixtures import make_services

pytest_plugins = ("tests._pg_fixtures",)


_ROLE_IDS = {
    "USER": uuid.UUID("00000000-0000-0000-0000-000000000001"),
    "OPERATOR": uuid.UUID("00000000-0000-0000-0000-000000000002"),
    "ADMIN": uuid.UUID("00000000-0000-0000-0000-000000000003"),
    "SERVICE": uuid.UUID("00000000-0000-0000-0000-000000000004"),
}


async def _ensure_roles(database) -> None:
    async with database.transaction() as session:
        existing = set((await session.scalars(select(RoleRow.code))).all())
        session.add_all(
            RoleRow(id=_ROLE_IDS[code], code=code)
            for code in set(_ROLE_IDS) - existing
        )


async def _create_user(
    database,
    roles: tuple[str, ...],
    *,
    principal_kind: str = "HUMAN",
    service_scopes: list[str] | None = None,
) -> uuid.UUID:
    await _ensure_roles(database)
    user_id = uuid.uuid4()
    session = database.session_factory()
    try:
      async with session.begin():
        session.add(UserRow(
            id=user_id,
            subject=str(user_id),
            display_name="test",
            principal_kind=principal_kind,
            service_scopes=service_scopes or [],
        ))
        await session.flush()
        role_rows = (await session.scalars(
            select(RoleRow).where(RoleRow.code.in_(roles))
        )).all()
        assert len(role_rows) == len(roles)
        session.add_all(UserRoleRow(user_id=user_id, role_id=row.id) for row in role_rows)
    finally:
      await session.close()
    return user_id


def _principal(user_id: uuid.UUID, *roles: str) -> Principal:
    now = datetime.now(UTC)
    return Principal(user_id, str(user_id), frozenset(roles), "test-jti", now, now + timedelta(minutes=5))


def _token(
    private,
    user_id: uuid.UUID,
    roles: list[str],
    *,
    scopes: list[str] | None = None,
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "test-issuer", "aud": "test-api", "sub": str(user_id),
            "roles": roles, "jti": uuid.uuid4().hex, "iat": now,
            "nbf": now, "exp": now + timedelta(minutes=2),
            "scopes": scopes or [],
        }, private, algorithm="EdDSA",
    )


def _disabled_rate_limiter() -> RedisTokenBucketRateLimiter:
    settings = SimpleNamespace(
        rate_limit_enabled=False,
        rate_limit_capacity=1,
        rate_limit_refill_rate=1.0,
    )
    return RedisTokenBucketRateLimiter(SimpleNamespace(), settings)


@pytest.mark.asyncio
async def test_real_postgresql_identity_constraints_and_ownership(clean_database):
    """真实 PG：identity seed、FK/unique 及 owner binding 的 fail-closed 行为。"""
    user_a = await _create_user(clean_database, ("USER",))
    user_b = await _create_user(clean_database, ("USER",))
    authz = AuthorizationService(clean_database)
    principal_a = _principal(user_a, "USER")
    await authz.bind_new(principal_a, "CONVERSATION", "conversation-a")
    await authz.bind_new(principal_a, "RUN", "run-a")

    async with clean_database.session() as session:
        ownership = await session.get(ObjectOwnershipRow, {"object_type": "RUN", "object_id": "run-a"})
        assert ownership is not None and ownership.owner_user_id == user_a
        assert set((await session.scalars(select(RoleRow.code))).all()) == {
            "USER", "OPERATOR", "ADMIN", "SERVICE"
        }

    await authz.require_owner(principal_a, "RUN", "run-a")
    with pytest.raises(AuthError) as denied:
        await authz.require_owner(_principal(user_b, "USER"), "RUN", "run-a")
    assert denied.value.status_code == 404

    with pytest.raises(AuthError) as historical:
        await authz.require_owner(principal_a, "RUN", "historical-no-owner")
    assert historical.value.status_code == 404
    await authz.require_owner(_principal(user_b, "ADMIN"), "RUN", "historical-no-owner")


@pytest.mark.asyncio
async def test_real_eddsa_bearer_identity_chain_uses_postgresql_roles(clean_database):
    user_id = await _create_user(clean_database, ("USER",))
    private = Ed25519PrivateKey.generate()
    public_pem = private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    settings = SimpleNamespace(
        jwt_public_key=public_pem,
        jwt_issuer="test-issuer",
        jwt_audience="test-api",
        jwt_allowed_algorithm="EdDSA",
        jwt_clock_skew_seconds=0,
    )
    token = _token(private, user_id, ["USER"])
    principal = await AuthService(clean_database, settings).authenticate(f"Bearer {token}")
    assert principal.user_id == user_id
    assert principal.roles == frozenset({"USER"})

    with pytest.raises(AuthError) as escalated:
        await AuthService(clean_database, settings).authenticate(
            f"Bearer {_token(private, user_id, ['ADMIN'])}"
        )
    assert escalated.value.code == "AUTH_INVALID_TOKEN"

    unknown_id = uuid.uuid4()
    with pytest.raises(AuthError) as unknown:
        await AuthService(clean_database, settings).authenticate(
            f"Bearer {_token(private, unknown_id, ['USER'])}"
        )
    assert unknown.value.code == "AUTH_UNKNOWN_PRINCIPAL"

    async with clean_database.transaction() as session:
        user = await session.get(UserRow, user_id)
        assert user is not None
        user.disabled_at = datetime.now(UTC)
    with pytest.raises(AuthError) as disabled:
        await AuthService(clean_database, settings).authenticate(f"Bearer {token}")
    assert disabled.value.code == "AUTH_PRINCIPAL_DISABLED"


@pytest.mark.asyncio
async def test_real_http_bearer_chain_hides_other_users_conversation(clean_database, monkeypatch):
    user_a = await _create_user(clean_database, ("USER",))
    user_b = await _create_user(clean_database, ("USER",))
    await AuthorizationService(clean_database).bind_new(
        _principal(user_a, "USER"), "CONVERSATION", "conversation-a"
    )
    private = Ed25519PrivateKey.generate()
    settings = SimpleNamespace(
        jwt_public_key=private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo),
        jwt_issuer="test-issuer", jwt_audience="test-api",
        jwt_allowed_algorithm="EdDSA", jwt_clock_skew_seconds=0,
    )
    monkeypatch.setattr(server.app.state, "chat_service", SimpleNamespace(
        get_history=lambda **_: [{"role": "user", "content": "safe"}]
    ), raising=False)
    monkeypatch.setattr(
        server.app.state, "auth_service", AuthService(clean_database, settings), raising=False
    )
    monkeypatch.setattr(
        server.app.state, "authorization_service", AuthorizationService(clean_database), raising=False
    )
    monkeypatch.setattr(
        server.app.state, "rate_limiter", _disabled_rate_limiter(), raising=False
    )
    client = TestClient(server.app)
    own = client.get("/api/history/conversation-a", headers={"Authorization": f"Bearer {_token(private, user_a, ['USER'])}"})
    other = client.get("/api/history/conversation-a", headers={"Authorization": f"Bearer {_token(private, user_b, ['USER'])}"})
    missing = client.get("/api/history/conversation-a")
    assert own.status_code == 200
    assert other.status_code == 404 and other.json()["error"]["request_id"]
    assert missing.status_code == 401 and missing.json()["error"]["request_id"]
    assert own.headers["X-Request-ID"] and other.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_real_http_approval_uses_run_owner_and_server_actor(clean_database, monkeypatch):
    user_a = await _create_user(clean_database, ("USER",))
    user_b = await _create_user(clean_database, ("USER",))
    await AuthorizationService(clean_database).bind_new(
        _principal(user_a, "USER"), "RUN", "10000000-0000-0000-0000-000000000001"
    )
    private = Ed25519PrivateKey.generate()
    settings = SimpleNamespace(
        jwt_public_key=private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo),
        jwt_issuer="test-issuer", jwt_audience="test-api",
        jwt_allowed_algorithm="EdDSA", jwt_clock_skew_seconds=0,
    )

    class _DurableApproval:
        actor_ids: list[str] = []

        async def decide(self, *args, actor_id: str, **kwargs):
            self.actor_ids.append(actor_id)
            return SimpleNamespace(
                safe_error_code=None,
                effective_status=SimpleNamespace(value="APPROVED"),
                idempotent=False,
                decided_at=None,
            )

    durable_approval = _DurableApproval()
    monkeypatch.setattr(server.app.state, "chat_service", SimpleNamespace(run_registry=SimpleNamespace()), raising=False)
    monkeypatch.setattr(
        server.app.state,
        "runtime_services",
        replace(make_services(), durable_approval=durable_approval),
        raising=False,
    )
    monkeypatch.setattr(
        server.app.state, "auth_service", AuthService(clean_database, settings), raising=False
    )
    monkeypatch.setattr(
        server.app.state, "authorization_service", AuthorizationService(clean_database), raising=False
    )
    monkeypatch.setattr(
        server.app.state, "rate_limiter", _disabled_rate_limiter(), raising=False
    )
    client = TestClient(server.app)
    path = (
        "/api/runtime/runs/10000000-0000-0000-0000-000000000001/"
        "tool-approvals/20000000-0000-0000-0000-000000000002/approve"
    )
    payload = {"invocation_binding_digest": "0" * 64, "actor_id": "admin"}
    other = client.post(
        path, json=payload,
        headers={"Authorization": f"Bearer {_token(private, user_b, ['USER'])}"},
    )
    own = client.post(
        path, json=payload,
        headers={"Authorization": f"Bearer {_token(private, user_a, ['USER'])}"},
    )

    assert other.status_code == 404 and other.json()["error"]["request_id"]
    assert own.status_code == 200
    assert durable_approval.actor_ids == [str(user_a)]


@pytest.mark.asyncio
async def test_real_http_evaluation_controls_are_admin_only(clean_database, monkeypatch):
    user_id = await _create_user(clean_database, ("USER",))
    service_id = await _create_user(
        clean_database,
        ("SERVICE",),
        principal_kind="SERVICE",
        service_scopes=["localagent:evaluation:execute"],
    )
    private = Ed25519PrivateKey.generate()
    settings = SimpleNamespace(
        jwt_public_key=private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo),
        jwt_issuer="test-issuer", jwt_audience="test-api",
        jwt_allowed_algorithm="EdDSA", jwt_clock_skew_seconds=0,
    )
    monkeypatch.setattr(
        server.app.state, "auth_service", AuthService(clean_database, settings), raising=False
    )
    client = TestClient(server.app)

    for path in (
        "/api/runtime/evaluation-execute/v3",
        "/api/runtime/evaluation-execute/v4",
    ):
        response = client.post(
            path, json={},
            headers={"Authorization": f"Bearer {_token(private, user_id, ['USER'])}"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["request_id"]
        assert response.headers["X-Request-ID"]
        service_response = client.post(
            path,
            json={},
            headers={
                "Authorization": (
                    f"Bearer {_token(private, service_id, ['SERVICE'], scopes=['localagent:evaluation:execute'])}"
                )
            },
        )
        assert service_response.status_code == 403


@pytest.mark.asyncio
async def test_real_http_service_principal_cannot_call_regular_chat_api(clean_database, monkeypatch):
    service_id = await _create_user(
        clean_database,
        ("SERVICE",),
        principal_kind="SERVICE",
        service_scopes=["localagent:evaluation:execute"],
    )
    private = Ed25519PrivateKey.generate()
    settings = SimpleNamespace(
        jwt_public_key=private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo),
        jwt_issuer="test-issuer", jwt_audience="test-api",
        jwt_allowed_algorithm="EdDSA", jwt_clock_skew_seconds=0,
    )
    monkeypatch.setattr(
        server.app.state, "auth_service", AuthService(clean_database, settings), raising=False
    )
    response = TestClient(server.app).get(
        "/api/history/any-conversation",
        headers={
            "Authorization": f"Bearer {_token(private, service_id, ['SERVICE'], scopes=['localagent:evaluation:execute'])}"
        },
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_real_http_service_cancel_is_limited_to_owned_run(clean_database, monkeypatch):
    owner_id = await _create_user(
        clean_database,
        ("SERVICE",),
        principal_kind="SERVICE",
        service_scopes=["localagent:evaluation:execute"],
    )
    other_id = await _create_user(
        clean_database,
        ("SERVICE",),
        principal_kind="SERVICE",
        service_scopes=["localagent:evaluation:execute"],
    )
    run_id = "10000000-0000-0000-0000-000000000001"
    await AuthorizationService(clean_database).bind_new(
        Principal(
            owner_id,
            str(owner_id),
            frozenset({"SERVICE"}),
            "owner-jti",
            datetime.now(UTC),
            datetime.now(UTC) + timedelta(minutes=5),
            "SERVICE",
            frozenset({"localagent:evaluation:execute"}),
        ),
        "RUN",
        run_id,
    )
    private = Ed25519PrivateKey.generate()
    settings = SimpleNamespace(
        jwt_public_key=private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo),
        jwt_issuer="test-issuer", jwt_audience="test-api",
        jwt_allowed_algorithm="EdDSA", jwt_clock_skew_seconds=0,
    )
    monkeypatch.setattr(
        server.app.state, "auth_service", AuthService(clean_database, settings), raising=False
    )
    monkeypatch.setattr(
        server.app.state, "authorization_service", AuthorizationService(clean_database), raising=False
    )
    monkeypatch.setattr(
        server.app.state, "rate_limiter", _disabled_rate_limiter(), raising=False
    )
    local_registry = SimpleNamespace(cancel=lambda *_: True)

    class _DurableControl:
        async def request_cancel(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(
        server.app.state,
        "chat_service",
        SimpleNamespace(run_registry=local_registry),
        raising=False,
    )
    monkeypatch.setattr(
        server.app.state,
        "runtime_services",
        replace(
            make_services(),
            durable_run_control=_DurableControl(),
            run_registry=local_registry,
        ),
        raising=False,
    )
    path = f"/api/runtime/runs/{run_id}/cancel"
    owner = TestClient(server.app).post(
        path,
        headers={
            "Authorization": f"Bearer {_token(private, owner_id, ['SERVICE'], scopes=['localagent:evaluation:execute'])}"
        },
    )
    foreign = TestClient(server.app).post(
        path,
        headers={
            "Authorization": f"Bearer {_token(private, other_id, ['SERVICE'], scopes=['localagent:evaluation:execute'])}"
        },
    )

    assert owner.status_code == 200
    assert owner.json()["status"] == "cancelled"
    assert foreign.status_code == 404
