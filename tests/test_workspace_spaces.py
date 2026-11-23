"""通过 v1 路由、真实 EdDSA 与隔离 PostgreSQL 验证 Workspace。"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import func, select

import server
from core.auth import AuthError, AuthService
from core.persistence.models import SpaceMemberRow, SpaceRow, UserRow
from core.workspace.service import WorkspaceService
from tests.test_stage6_wp2_ownership import (
    _create_user, _DEFAULT_TENANT_ID, _disabled_rate_limiter, _principal, _token,
)


@pytest.fixture
def workspace_http(clean_database, monkeypatch):
    private = Ed25519PrivateKey.generate()
    settings = SimpleNamespace(
        jwt_public_key=private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo),
        jwt_issuer="test-issuer", jwt_audience="test-api",
        jwt_allowed_algorithm="EdDSA", jwt_clock_skew_seconds=0,
    )
    monkeypatch.setattr(server.app.state, "auth_service", AuthService(clean_database, settings), raising=False)
    monkeypatch.setattr(server.app.state, "workspace_service", WorkspaceService(clean_database), raising=False)
    monkeypatch.setattr(server.app.state, "rate_limiter", _disabled_rate_limiter(), raising=False)

    def connect(user_id, role):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app),
            base_url="http://testserver/api/v1",
            headers={"Authorization": f"Bearer {_token(private, user_id, [role])}"},
        )

    return connect


@pytest.mark.asyncio
async def test_concurrent_personal_space_and_team_persistent_readback(clean_database, workspace_http, monkeypatch):
    user_id = await _create_user(clean_database, ("USER",))
    admin_id = await _create_user(clean_database, ("ADMIN",))
    async with workspace_http(user_id, "USER") as owner, workspace_http(admin_id, "ADMIN") as admin:
        responses = await asyncio.gather(*(owner.get("/spaces") for _ in range(3)))
        assert all(response.status_code == 200 for response in responses)
        personal, = responses[0].json()
        assert all(response.json() == [personal] for response in responses)
        assert personal["kind"] == "PERSONAL" and personal["role"] == "OWNER"
        assert personal["can_manage_members"] is False
        async with clean_database.session() as session:
            assert await session.scalar(select(func.count()).select_from(SpaceRow)) == 1
            assert await session.scalar(select(func.count()).select_from(SpaceMemberRow)) == 0
        assert (await admin.get(f"/spaces/{personal['id']}")).status_code == 404
        assert (await owner.get(f"/spaces/{personal['id']}/members")).status_code == 404
        assert (await owner.put(f"/spaces/{personal['id']}/members/{admin_id}", json={"role": "MEMBER"})).status_code == 404
        for field in ("tenant_id", "owner_user_id", "role", "kind"):
            rejected = await owner.post("/spaces", json={"name": "研究组", field: str(admin_id)})
            assert rejected.status_code == 422
            assert rejected.json()["error"]["code"] == "VALIDATION_ERROR"
        for invalid_name in ("   ", "team\x00"):
            assert (await owner.post("/spaces", json={"name": invalid_name})).status_code == 422
        created = await owner.post("/spaces", json={"name": "  研究组  "})
        assert created.status_code == 201
        team = created.json()
        assert (team["name"], team["tenant_id"], team["owner_user_id"], team["role"]) == (
            "研究组", _DEFAULT_TENANT_ID, str(user_id), "OWNER",
        )
        monkeypatch.setattr(server.app.state, "workspace_service", WorkspaceService(clean_database))
        assert (await owner.get(f"/spaces/{team['id']}")).json() == team
        assert (await owner.get("/spaces")).json() == [personal, team]


@pytest.mark.asyncio
async def test_workspace_membership_admin_and_revocation(clean_database, workspace_http):
    owner_id = await _create_user(clean_database, ("USER",))
    member_id = await _create_user(clean_database, ("USER",))
    tenant_admin_id = await _create_user(clean_database, ("ADMIN",))
    guest_id = await _create_user(clean_database, ("USER",), tenant_id="other-tenant")
    service_id = await _create_user(clean_database, ("SERVICE",), principal_kind="SERVICE")
    async with (
        workspace_http(owner_id, "USER") as owner, workspace_http(member_id, "USER") as member,
        workspace_http(tenant_admin_id, "ADMIN") as tenant_admin, workspace_http(guest_id, "USER") as guest,
        workspace_http(service_id, "SERVICE") as service,
    ):
        team = (await owner.post("/spaces", json={"name": "权限验证"})).json()
        path = f"/spaces/{team['id']}"
        roster = f"{path}/members"
        for outsider in (member, tenant_admin, guest):
            assert (await outsider.get(path)).status_code == 404
            assert (await outsider.get(roster)).status_code == 404
            assert (await outsider.put(f"{roster}/{member_id}", json={"role": "ADMIN"})).status_code == 404
            assert [space["kind"] for space in (await outsider.get("/spaces")).json()] == ["PERSONAL"]
        assert (await service.get("/spaces")).status_code == 403
        for target in (guest_id, service_id, uuid4()):
            rejected = await owner.put(f"{roster}/{target}", json={"role": "MEMBER"})
            assert (rejected.status_code, rejected.json()["error"]["code"]) == (404, "SPACE_MEMBER_USER_NOT_FOUND")
        assert (await owner.put(f"{roster}/{member_id}", json={"role": "OWNER"})).status_code == 422
        assert (await owner.put(f"{roster}/{member_id}", json={"role": "ADMIN", "tenant_id": "other-tenant"})).status_code == 422
        assert (await owner.put(f"{roster}/{owner_id}", json={"role": "ADMIN"})).status_code == 409
        assert (await owner.delete(f"{roster}/{owner_id}")).status_code == 409
        for _ in range(2):
            assert (await owner.put(f"{roster}/{member_id}", json={"role": "MEMBER"})).status_code == 200
        assert (await member.get(path)).json()["can_manage_members"] is False
        assert [entry["role"] for entry in (await member.get(roster)).json()] == ["OWNER", "MEMBER"]
        assert (await member.put(f"{roster}/{member_id}", json={"role": "ADMIN"})).status_code == 403
        assert (await member.delete(f"{roster}/{member_id}")).status_code == 403
        assert (await owner.put(f"{roster}/{member_id}", json={"role": "ADMIN"})).status_code == 200
        assert (await member.get(path)).json()["can_manage_members"] is True
        assert (await member.put(f"{roster}/{tenant_admin_id}", json={"role": "MEMBER"})).status_code == 200
        assert (await tenant_admin.get(path)).json()["role"] == "MEMBER"
        async with clean_database.transaction() as session:
            disabled = await session.get(UserRow, tenant_admin_id)
            disabled.disabled_at = datetime.now(UTC)
        assert (await tenant_admin.get(path)).status_code == 401
        assert (await member.put(f"{roster}/{tenant_admin_id}", json={"role": "ADMIN"})).status_code == 404
        assert (await member.delete(f"{roster}/{tenant_admin_id}")).status_code == 204
        assert (await owner.delete(f"{roster}/{member_id}")).status_code == 204
        assert (await owner.delete(f"{roster}/{member_id}")).json()["error"]["code"] == "SPACE_MEMBER_NOT_FOUND"
        assert (await member.get(path)).status_code == 404
        assert [space["kind"] for space in (await member.get("/spaces")).json()] == ["PERSONAL"]


@pytest.mark.asyncio
async def test_workspace_bound_resource_tenant_must_match(clean_database):
    user_id = await _create_user(clean_database, ("USER",))
    principal = _principal(user_id, "USER")
    access = await WorkspaceService(clean_database).create_team(principal, "资源归属验证")
    async with clean_database.session() as session:
        allowed = await WorkspaceService.require_access_in_session(
            session, principal, access.space.id, resource_tenant_id=_DEFAULT_TENANT_ID,
        )
        assert allowed.role == "OWNER"
        with pytest.raises(AuthError) as denied:
            await WorkspaceService.require_access_in_session(
                session, principal, access.space.id, resource_tenant_id="other-tenant",
            )
        assert denied.value.status_code == 404
