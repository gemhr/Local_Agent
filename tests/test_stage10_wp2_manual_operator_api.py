"""Stage10-WP2 manual Tool resolution HTTP/auth/audit integration tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import select

import server
from core.auth import (
    AUTHORIZATION_FORBIDDEN,
    AUTHORIZATION_OBJECT_NOT_OWNED,
    AuthError,
    AuthService,
    AuthorizationService,
    Principal,
)
from core.persistence.models import ManualToolResolutionAuditRow
from core.runtime.run_control import DurableRunControlService
from core.runtime.metrics import InMemoryMetricsRecorder
from core.runtime.tool_contract import ToolInvocation
from core.runtime.tool_idempotency import (
    DurableToolInvocationService,
    ToolInvocationState,
)


TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
USER_A = uuid.UUID("10000000-0000-0000-0000-000000000001")
USER_B = uuid.UUID("10000000-0000-0000-0000-000000000002")


class _Auth(AuthService):
    async def authenticate(self, authorization: str | None) -> Principal:
        if authorization is None:
            raise AuthError("AUTH_MISSING_CREDENTIAL")
        user_id, tenant = (
            (USER_B, TENANT_B)
            if authorization == "Bearer wrong-tenant"
            else (USER_A, TENANT_A)
        )
        now = datetime.now(UTC)
        return Principal(
            user_id=user_id,
            subject=str(user_id),
            roles=frozenset({"USER"}),
            token_id=authorization[7:] if authorization.startswith("Bearer ") else "manual-operator-test",
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            tenant_id=tenant,
        )


class _Authorization(AuthorizationService):
    async def authorize(self, principal, object_type, object_id, action, *, required_scope=None):
        if principal.tenant_id == TENANT_B:
            raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)
        if principal.token_id == "insufficient":
            raise AuthError(AUTHORIZATION_FORBIDDEN, 403)


def _invocation() -> ToolInvocation:
    return ToolInvocation.create(
        tool_name="manual_operator_test_tool",
        invocation_id=uuid.uuid4().hex,
        idempotency_key=uuid.uuid4().hex,
        arguments={"operation_id": "safe-operation-id"},
    )


async def _unknown(database, run_id: str, owner: str = "worker"):
    control = DurableRunControlService(database, lease_seconds=300)
    lease = await control.claim(run_id, owner)
    service = DurableToolInvocationService(database)
    invocation = _invocation()
    await service.prepare(
        lease=lease,
        step_id="step-1",
        invocation=invocation,
        tool_name=invocation.tool_name,
    )
    await service.start(lease=lease, invocation_id=invocation.invocation_id)
    await service.unknown(
        lease=lease,
        invocation_id=invocation.invocation_id,
        reason="response lost after provider boundary",
    )
    await control.release(lease)
    return control, service, invocation


@pytest.mark.asyncio
async def test_manual_resolution_http_auth_binding_and_audit(
    clean_database, monkeypatch
):
    run_id = uuid.uuid4().hex
    control, service, invocation = await _unknown(clean_database, run_id)
    runtime = SimpleNamespace(
        durable_run_control=control,
        durable_tool_invocation=service,
        run_control_owner_id="api-test-owner",
    )

    monkeypatch.setattr(
        server.app.state,
        "auth_service",
        _Auth.__new__(_Auth),
        raising=False,
    )
    monkeypatch.setattr(
        server.app.state,
        "authorization_service",
        _Authorization.__new__(_Authorization),
        raising=False,
    )
    monkeypatch.setattr(
        server.app.state,
        "rate_limiter",
        server.RedisTokenBucketRateLimiter(
            SimpleNamespace(),
            SimpleNamespace(
                rate_limit_enabled=False,
                rate_limit_capacity=10,
                rate_limit_refill_rate=10.0,
            ),
        ),
        raising=False,
    )
    metrics = InMemoryMetricsRecorder()
    monkeypatch.setattr(server.app.state, "runtime_metrics", metrics, raising=False)
    monkeypatch.setattr(server, "_require_runtime_services", lambda: runtime)

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        path = f"/api/runtime/runs/{run_id}/tool-invocations/{invocation.invocation_id}/resolve"

        unauthenticated = await client.post(
            path, json={"resolution": "COMMITTED", "reason": "provider evidence"}
        )
        assert unauthenticated.status_code == 401

        wrong_tenant = await client.post(
            path,
            headers={"Authorization": "Bearer wrong-tenant"},
            json={"resolution": "COMMITTED", "reason": "provider evidence"},
        )
        assert wrong_tenant.status_code == 404

        insufficient = await client.post(
            path,
            headers={"Authorization": "Bearer insufficient"},
            json={"resolution": "COMMITTED", "reason": "provider evidence"},
        )
        assert insufficient.status_code == 403

        mismatch = await client.post(
            f"/api/runtime/runs/{uuid.uuid4().hex}/tool-invocations/{invocation.invocation_id}/resolve",
            headers={"Authorization": "Bearer operator"},
            json={"resolution": "COMMITTED", "reason": "provider evidence"},
        )
        assert mismatch.status_code in {404, 409, 422}

        response = await client.post(
            path,
            headers={"Authorization": "Bearer operator"},
            json={
                "resolution": "COMMITTED",
                "reason": "provider evidence confirmed in external record",
                "provider_operation_id": "provider-op-1",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "COMMITTED"

        stale = await client.post(
            path,
            headers={"Authorization": "Bearer operator"},
            json={"resolution": "NOT_COMMITTED", "reason": "late conflicting claim"},
        )
        assert stale.status_code == 409

    assert metrics.snapshot().counter("runtime_reconciliation_manual_resolved_total") == 1

    assert (await service.get(invocation.invocation_id)).state is ToolInvocationState.COMMITTED
    async with clean_database.session() as session:
        audit = (
            await session.execute(
                select(ManualToolResolutionAuditRow).where(
                    ManualToolResolutionAuditRow.invocation_id == invocation.invocation_id
                )
            )
        ).scalar_one()
        assert audit.actor_id == str(USER_A)
        assert audit.tenant_id == TENANT_A
        assert audit.run_id == run_id
        assert audit.resolution == "COMMITTED"
        assert audit.reason.startswith("provider evidence")


@pytest.mark.asyncio
async def test_manual_not_committed_http_path_persists_audit(clean_database, monkeypatch):
    run_id = uuid.uuid4().hex
    control, service, invocation = await _unknown(clean_database, run_id)
    runtime = SimpleNamespace(
        durable_run_control=control,
        durable_tool_invocation=service,
        run_control_owner_id="api-test-owner",
    )
    monkeypatch.setattr(server.app.state, "auth_service", _Auth.__new__(_Auth), raising=False)
    monkeypatch.setattr(
        server.app.state,
        "authorization_service",
        _Authorization.__new__(_Authorization),
        raising=False,
    )
    monkeypatch.setattr(
        server.app.state,
        "rate_limiter",
        server.RedisTokenBucketRateLimiter(
            SimpleNamespace(),
            SimpleNamespace(rate_limit_enabled=False, rate_limit_capacity=10, rate_limit_refill_rate=10.0),
        ),
        raising=False,
    )
    monkeypatch.setattr(server, "_require_runtime_services", lambda: runtime)

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{run_id}/tool-invocations/{invocation.invocation_id}/resolve",
            headers={"Authorization": "Bearer operator"},
            json={"resolution": "NOT_COMMITTED", "reason": "provider confirms no commit"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "NOT_COMMITTED"
    async with clean_database.session() as session:
        audit = (
            await session.execute(
                select(ManualToolResolutionAuditRow).where(
                    ManualToolResolutionAuditRow.invocation_id == invocation.invocation_id
                )
            )
        ).scalar_one()
        assert audit.resolution == "NOT_COMMITTED"


@pytest.mark.asyncio
async def test_typed_manual_resolution_rejects_started_state(clean_database):
    control = DurableRunControlService(clean_database, lease_seconds=300)
    lease = await control.claim(uuid.uuid4().hex, "worker")
    service = DurableToolInvocationService(clean_database)
    invocation = _invocation()
    await service.prepare(
        lease=lease,
        step_id="step-1",
        invocation=invocation,
        tool_name=invocation.tool_name,
    )
    await service.start(lease=lease, invocation_id=invocation.invocation_id)
    with pytest.raises(ValueError, match="manual resolution requires UNKNOWN"):
        await service.resolve_unknown_not_committed(
            lease=lease, invocation_id=invocation.invocation_id
        )
