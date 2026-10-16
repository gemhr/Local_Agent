"""Loop 专用 HTTP 边界；本测试不声称真实 JWT/模型/PG E2E。"""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx
import pytest

from core.agent_platform.agent_loop_evaluation import register_agent_loop_evaluation
from core.auth import AuthError, AuthorizationService, EVALUATION_EXECUTE_SCOPE, Principal
from core.persistence.models import ObjectOwnershipRow
from core.runtime.agent_loop.contracts import LOOP_POLICY, LoopSuspended
from core.settings import SettingsValidationError
from tests.test_settings_validation import _load


@pytest.mark.asyncio
async def test_loop_evaluation_disabled_mapping_owner_and_suspension(monkeypatch):
    settings = _load(monkeypatch, LOCAL_AGENT_LOOP_EVALUATION_PROJECT_ID=None,
                     LOCAL_AGENT_LOOP_EVALUATION_SERVICE_PRINCIPAL_ID=None)
    assert settings.loop_evaluation_project_id == settings.loop_evaluation_service_principal_id == ""
    project, service_id, run_id = str(uuid.uuid4()), uuid.uuid4(), str(uuid.uuid4())
    with pytest.raises(SettingsValidationError):
        _load(monkeypatch, LOCAL_AGENT_LOOP_EVALUATION_PROJECT_ID=project)
    with pytest.raises(SettingsValidationError):
        _load(monkeypatch, LOCAL_AGENT_LOOP_EVALUATION_PROJECT_ID="invalid",
              LOCAL_AGENT_LOOP_EVALUATION_SERVICE_PRINCIPAL_ID=str(service_id))
    now = datetime.now(UTC)
    principal = Principal(service_id, str(service_id), frozenset({"SERVICE"}), "test-jti", now,
                          now + timedelta(minutes=1), "SERVICE", frozenset({EVALUATION_EXECUTE_SCOPE}), "test-tenant")
    owner = ObjectOwnershipRow(object_type="RUN", object_id=run_id, owner_user_id=service_id, tenant_id="test-tenant")
    calls = {"execute": 0, "read": 0, "bind": 0}

    class Database:
        @asynccontextmanager
        async def session(self):
            yield self

        async def get(self, model, key):
            assert model is ObjectOwnershipRow
            return owner

    authz = AuthorizationService(Database())

    class Repository:
        async def read_loop_evidence_source(self, requested_run_id):
            calls["read"] += 1
            assert requested_run_id == run_id
            return SimpleNamespace(root=SimpleNamespace(status="ACTIVE"), loop_frames=())

    class Service:
        def _resolve_request(self, request):
            return None, SimpleNamespace(business_options={"execution_policy": LOOP_POLICY}), None, None, None

        async def execute_agent_loop(self, request, *, loop_correlation):
            calls["execute"] += 1
            assert request.run_id == loop_correlation["attempt_id"] == run_id
            raise LoopSuspended("WAIT_APPROVAL")

    async def bind(request, **kwargs):
        calls["bind"] += 1
        await authz._require_existing_binding(request.state.principal, "RUN", kwargs["run_id"])

    async def authorize(request, requested_run_id, action, *, required_scope):
        await authz.authorize(request.state.principal, "RUN", requested_run_id, action, required_scope=required_scope)

    async def check(config):
        app = FastAPI()
        app.state.loop_execution_repository = Repository()

        @app.middleware("http")
        async def authenticated(request, call_next):
            request.state.principal = principal
            return await call_next(request)

        @app.exception_handler(AuthError)
        async def denied(request, error):
            return JSONResponse(status_code=error.status_code, content={"code": error.code})

        register_agent_loop_evaluation(app, settings=config, require_service=Service, bind_run=bind,
            authorize_run=authorize, authorization_service=lambda request: authz,
            final_answer_capture=lambda **kwargs: pytest.fail("suspension cannot form final answer"))
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    payload = {"agent_id": "public-loop", "query": "public", "run_id": run_id,
               "evaluation_project_id": project, "evaluation_run_id": str(uuid.uuid4()),
               "attempt_id": run_id, "request_id": str(uuid.uuid4())}
    path = "/api/runtime/evaluation-execute/agent-loop/v1"
    get = f"/api/runtime/runs/{run_id}/loop-evidence/v1"
    async with await check(settings) as client:
        assert (await client.post(path, json=payload)).status_code == 503
        assert (await client.get(get)).status_code == 503
    assert calls == {"execute": 0, "read": 0, "bind": 0}
    configured = replace(settings, loop_evaluation_project_id=project,
                         loop_evaluation_service_principal_id=str(service_id))
    async with await check(replace(configured, loop_evaluation_service_principal_id=str(uuid.uuid4()))) as client:
        assert (await client.post(path, json=payload)).status_code == 403
        assert (await client.get(get)).status_code == 403
    async with await check(configured) as client:
        assert (await client.post(path, json={**payload, "evaluation_project_id": str(uuid.uuid4())})).status_code == 403
        owner.owner_user_id = uuid.uuid4()
        assert (await client.get(get)).status_code == 404
        assert (await client.post(path, json=payload)).status_code == 404
        assert calls["execute"] == calls["read"] == 0
        owner.owner_user_id = service_id
        response = await client.post(path, json=payload)
        assert response.status_code == 202 and response.json()["run_id"] == run_id
        assert response.json()["capture_status"] == "NOT_READY"
        response = await client.get(get)
        assert response.status_code == 202 and response.json()["capture_status"] == "NOT_READY"
        assert calls["execute"] == 1 and calls["read"] == 1
