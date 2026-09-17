"""Stage8-WP9 HTTP authority 的窄合同测试。"""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import Request
from pydantic import ValidationError

import server
from core.auth import AUTHORIZATION_FORBIDDEN, AuthError


def test_observe_body_is_strictly_empty():
    assert server.Stage8ObserveRequest.model_validate({}) is not None
    with pytest.raises(ValidationError):
        server.Stage8ObserveRequest.model_validate({
            "status": "FAILED",
            "error_message": "caller supplied",
            "log_path": "C:\\secret",
        })


def test_legacy_callback_dto_rejects_unknown_authority_fields():
    with pytest.raises(ValidationError):
        server.Stage8ExecutionResultRequest.model_validate({
            "execution_id": "EXEC-1",
            "status": "FAILED",
            "actual_result": "failed",
            "result_location": "C:\\secret",
        })

    with pytest.raises(ValidationError):
        server.Stage8ExecutionResultRequest.model_validate({
            "execution_id": "EXEC-1",
            "status": "FAILED",
            "actual_result": "failed",
            "logs": ["x" * 2048] * 9,
        })


@pytest.mark.asyncio
async def test_legacy_callback_rejects_normal_authenticated_caller():
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace())})
    request.state.principal = SimpleNamespace(
        principal_kind="HUMAN", scopes=frozenset(), roles=frozenset({"USER"})
    )
    body = server.Stage8ExecutionResultRequest(
        execution_id="EXEC-1", status="FAILED", actual_result="failed"
    )

    with pytest.raises(AuthError) as raised:
        await server.stage8_ingest_execution_result("EXEC-1", body, request)

    assert raised.value.code == AUTHORIZATION_FORBIDDEN
    assert raised.value.status_code == 403


def test_legacy_callback_path_guard_is_exact():
    assert server._is_stage8_result_callback_path(
        "/api/stage8/executions/EXEC-1/result"
    )
    assert not server._is_stage8_result_callback_path(
        "/api/stage8/executions/EXEC-1/result/extra"
    )
    assert not server._is_stage8_result_callback_path(
        "/api/stage8/execution-jobs/JOB-1/observe"
    )


@pytest.mark.asyncio
async def test_observe_endpoint_rejects_caller_supplied_result_fields(monkeypatch):
    async def authenticate(self, authorization):
        return SimpleNamespace(
            principal_kind="HUMAN",
            scopes=frozenset(),
            roles=frozenset({"USER"}),
            authz_domain_id="wp9-user",
        )

    async def check(self, authz_domain_id):
        return SimpleNamespace(allowed=True, retry_after_seconds=0)

    monkeypatch.setattr(server.AuthService, "authenticate", authenticate)
    monkeypatch.setattr(server.RedisTokenBucketRateLimiter, "check", check)
    monkeypatch.setattr(
        server.app.state, "auth_service", object.__new__(server.AuthService),
        raising=False,
    )
    monkeypatch.setattr(
        server.app.state, "rate_limiter",
        object.__new__(server.RedisTokenBucketRateLimiter), raising=False,
    )
    transport = httpx.ASGITransport(app=server.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/stage8/execution-jobs/JOB-1/observe",
            headers={"Authorization": "Bearer test"},
            json={"status": "FAILED", "log_path": "C:\\secret"},
        )

    assert response.status_code == 422
