import asyncio
from base64 import b64encode
from contextlib import AsyncExitStack, aclosing
from dataclasses import asdict, replace
import json
import re
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from core.agent_platform.application import AgentApplicationError, AgentApplicationService, ExecutionRequest
from core.agent_platform.opencode_runtime_adapter import OpenCodeRuntimeAdapter
from core.agent_platform.registry import AgentRegistrationBundle, compile_agent_catalog
from core.persistence.repositories.external_runs import ExternalRunRepository
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.run_registry import RunRegistry
from core.runtime.state import RunStatus, StopReason
from core.settings import OpenCodeInstanceSettings, Settings, SettingsValidationError


def _instance(instance_id="local", deployment="LOCAL", port=4096):
    return OpenCodeInstanceSettings(
        instance_id, deployment, f"http://127.0.0.1:{port}", f"D:/隔离 工作区/{instance_id}",
        f"test-{instance_id}", provider_id="example", model_id="model/flash", agent="text",
    )


def _application(*adapters, external_runs=None):
    registry = compile_agent_catalog(
        AgentRegistrationBundle(registrations=()),
        builtin_registrations=(
            DEFAULT_AGENT_REGISTRY.resolve(agent_id) for agent_id in DEFAULT_AGENT_REGISTRY.agent_ids
        ),
    ).agent_registry
    return AgentApplicationService(
        Mock(spec_set=[]), registry, runtime_instances=dict(adapters), external_runs=external_runs,
    )


def _adapter(instance, transport, registry=None):
    runs = Mock(spec_set=ExternalRunRepository)
    runs.claim.return_value = None
    runs.dispatch.return_value = None
    return OpenCodeRuntimeAdapter(instance, runs=runs, run_registry=registry or RunRegistry(), transport=transport)


def _assistant(session_id, parent_id, text):
    assistant_id = "msg_assistant" + session_id.removeprefix("ses_")
    return {
        "info": {
            "id": assistant_id, "role": "assistant", "sessionID": session_id,
            "parentID": parent_id, "time": {"completed": 123}, "finish": "stop",
        },
        "parts": [{
            "type": "text", "text": text, "sessionID": session_id, "messageID": assistant_id,
        }],
    }


class _ClosingTransport(httpx.MockTransport):
    closed = False

    async def aclose(self):
        self.closed = True
        await super().aclose()


@pytest.mark.asyncio
async def test_application_selects_instances_and_keeps_concurrent_runs_isolated():
    local, managed = _instance(), _instance("managed", "MANAGED", 4097)
    instances = {4096: local, 4097: managed}
    session_count = 0
    received = {}
    both_local_prompts = asyncio.Event()

    async def handle(request):
        nonlocal session_count
        instance = instances[request.url.port]
        assert request.url.params["directory"] == instance.workspace
        encoded = b64encode(f"{instance.username}:{instance.password}".encode()).decode()
        assert request.headers["authorization"] == f"Basic {encoded}"
        if request.url.path == "/event":
            assert request.method == "GET"
            return httpx.Response(200, headers={"content-type": "text/event-stream"})
        assert request.method == "POST"
        if request.url.path == "/session":
            assert json.loads(request.content) == {"title": "AgentCore Run"}
            session_count += 1
            return httpx.Response(200, json={"id": f"ses_{session_count}", "directory": instance.workspace})
        assert re.fullmatch(r"/session/ses_\d+/message", request.url.path)
        session_id = request.url.path.split("/")[2]
        prompt = json.loads(request.content)
        assert re.fullmatch(r"msg_[0-9a-f]{12}[A-Za-z0-9]{14}", prompt["messageID"])
        assert prompt["model"] == {"providerID": "example", "modelID": "model/flash"}
        assert prompt["agent"] == "text"
        assert prompt["tools"] == {"*": False}
        query = prompt["parts"][0]["text"]
        received[query] = (instance.instance_id, session_id, prompt["messageID"])
        if instance is local:
            if sum(item[0] == "local" for item in received.values()) == 2:
                both_local_prompts.set()
            await asyncio.wait_for(both_local_prompts.wait(), timeout=2)
        return httpx.Response(200, json=_assistant(session_id, prompt["messageID"], f"reply/{query}"))

    local_transport, managed_transport = _ClosingTransport(handle), _ClosingTransport(handle)
    local_adapter = _adapter(local, local_transport)
    managed_adapter = _adapter(managed, managed_transport)
    app = _application(("local", local_adapter), ("managed", managed_adapter))
    first = ExecutionRequest("core_router", "first", run_id="run-1", runtime_instance_id="local")
    second = ExecutionRequest("core_router", "second", run_id="run-2", runtime_instance_id="local")
    managed_request = ExecutionRequest("core_router", "third", run_id="run-3", runtime_instance_id="managed")
    try:
        async with aclosing(app.stream(replace(first, run_id="run-early"))) as early_stream:
            assert (await anext(early_stream)).run_id == "run-early"
        assert session_count == 0
        result1, result2 = await asyncio.gather(app.execute(first), app.execute(second))
        events = [event async for event in app.stream(managed_request)]
        assert [event.kind for event in events] == ["started", "output_delta", "terminal"]
        assert events[1].output_delta == "reply/third"
        assert events[-1].status is RunStatus.SUCCEEDED
        assert {event.run_id for event in events} == {"run-3"}
        assert result1.output == "reply/first" and result1.run_id == "run-1"
        assert result2.output == "reply/second" and result2.run_id == "run-2"
        assert result1.agent_version == "builtin-1" and result1.stop_reason is StopReason.COMPLETED
        assert result1.output_disposition == "NOT_REQUIRED"
        assert result1.toolset_identity is result1.resolved_model_profile_id is None
        assert result1.resolved_retrieval_profile_id is result1.resolved_memory_profile_id is None
        assert [received[query][0] for query in ("first", "second", "third")] == ["local", "local", "managed"]
        assert len({item[1] for item in received.values()}) == 3
        assert len({item[2] for item in received.values()}) == 3
        assert not local_transport.closed and not managed_transport.closed
    finally:
        await local_adapter.close()
        await managed_adapter.close()
    assert local_transport.closed and managed_transport.closed


@pytest.mark.asyncio
async def test_unavailable_instance_version_or_agent_is_rejected_before_http():
    adapter = Mock(spec_set=OpenCodeRuntimeAdapter)
    app = _application(("local", adapter))
    for request, expected in (
        (ExecutionRequest("core_router", "prompt", runtime_instance_id="missing"), "RUNTIME_INSTANCE_UNAVAILABLE"),
        (ExecutionRequest("code_expert", "prompt", runtime_instance_id="local"), "RUNTIME_INSTANCE_UNAVAILABLE"),
        (ExecutionRequest("core_router", "prompt", expected_agent_version="invalid", runtime_instance_id="local"),
         "AGENT_VERSION_MISMATCH"),
    ):
        with pytest.raises(AgentApplicationError, match=expected):
            await app.execute(request)
        with pytest.raises(AgentApplicationError, match=expected):
            await anext(app.stream(request))
    with pytest.raises(AgentApplicationError, match="RUNTIME_INSTANCE_UNAVAILABLE"):
        await app.execute_evaluation(ExecutionRequest("core_router", "prompt", runtime_instance_id="local"))
    assert not adapter.mock_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("unfinished", "OPENCODE_RESULT_UNKNOWN"),
        ("wrong_parent", "OPENCODE_RESULT_UNKNOWN"),
        ("wrong_part", "OPENCODE_RESULT_UNKNOWN"),
        ("truncated", "OPENCODE_TASK_FAILED"),
        ("read_timeout", "OPENCODE_RESULT_UNKNOWN"),
        ("missing_session", "OPENCODE_SESSION_NOT_FOUND"),
        ("rejected", "OPENCODE_PROMPT_REJECTED"),
        ("auth", "OPENCODE_AUTH_FAILED"),
        ("connect", "OPENCODE_CONNECTION_FAILED"),
        ("terminal_error", "OPENCODE_TASK_FAILED"),
    ],
)
async def test_unconfirmed_or_failed_prompt_never_returns_output_or_retries(failure, expected):
    instance = _instance()
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/event":
            return httpx.Response(200, headers={"content-type": "text/event-stream"})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if failure == "connect":
            raise httpx.ConnectError("private upstream data", request=request)
        if failure == "auth":
            return httpx.Response(401, text="private upstream data")
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_test", "directory": instance.workspace})
        if failure == "read_timeout":
            raise httpx.ReadTimeout("private upstream data", request=request)
        if failure in {"missing_session", "rejected"}:
            return httpx.Response(404 if failure == "missing_session" else 400, text="private upstream data")
        parent = json.loads(request.content)["messageID"]
        reply = _assistant("ses_test", parent, "private upstream data")
        if failure == "unfinished":
            reply["info"]["time"] = {}
        if failure == "wrong_parent":
            reply["info"]["parentID"] = "msg_other"
        if failure == "wrong_part":
            reply["parts"][0]["messageID"] = "msg_other"
        if failure == "truncated":
            reply["info"]["finish"] = "length"
        if failure == "terminal_error":
            reply["info"]["error"] = {"name": "APIError", "data": {"message": "private upstream data"}}
        return httpx.Response(200, json=reply)

    adapter = _adapter(instance, httpx.MockTransport(handle))
    app = _application(("local", adapter))
    request = ExecutionRequest("core_router", "prompt", runtime_instance_id="local")
    try:
        if expected == "OPENCODE_TASK_FAILED":
            result = await app.execute(request)
            assert result.status is RunStatus.FAILED and result.error_code == expected
            assert result.output is None and "private upstream data" not in result.safe_message
        else:
            with pytest.raises(AgentApplicationError, match=expected) as exc:
                await app.execute(request)
            assert "private upstream data" not in str(exc.value)
        posts = [path for method, path in calls if method == "POST"]
        assert posts == (["/session"] if failure in {"auth", "connect"} else ["/session", "/session/ses_test/message"])
        calls.clear()
        if failure == "wrong_parent":
            async with aclosing(app.stream(request)) as stream:
                assert (await anext(stream)).kind == "started"
                with pytest.raises(AgentApplicationError, match=expected):
                    await anext(stream)
            assert calls == [
                ("POST", "/session"), ("GET", "/event"),
                ("POST", "/session/ses_test/message"), ("GET", "/session/ses_test/message"),
            ]
    finally:
        await adapter.close()


def test_settings_resolve_instance_secret_and_fail_closed(monkeypatch):
    monkeypatch.delenv("LOCAL_AGENT_OPENCODE_INSTANCES", raising=False)
    assert Settings.load().opencode_instances == ()
    instance = _instance()
    entry = asdict(instance)
    entry.pop("password")
    entry.pop("allowed_principals")
    entry["password_env"] = "OPENCODE_TEST_HTTP_PASSWORD"
    monkeypatch.setenv("LOCAL_AGENT_OPENCODE_INSTANCES", json.dumps([entry]))
    monkeypatch.delenv(entry["password_env"], raising=False)
    with pytest.raises(SettingsValidationError, match="INVALID_HTTP_CREDENTIALS"):
        Settings.load()
    monkeypatch.setenv(entry["password_env"], instance.password)
    assert Settings.load().opencode_instances == (instance,)
    assert instance.password not in repr(Settings.load().opencode_instances)
    user_id = uuid4()
    grant = {"tenant_id": "tenant-a", "user_id": str(user_id).upper()}
    for grants, expected in (([], frozenset()), ([grant], frozenset({("tenant-a", user_id)}))):
        monkeypatch.setenv("LOCAL_AGENT_OPENCODE_INSTANCES", json.dumps([dict(entry, allowed_principals=grants)]))
        assert Settings.load().opencode_instances[0].allowed_principals == expected
    for invalid, reason in (
        ([entry, entry], "INVALID_OR_DUPLICATE_INSTANCE_ID"),
        ([dict(entry, base_url="http://untrusted.invalid")], "INVALID_ENDPOINT"),
        ([dict(entry, allowed_principals={})], "INVALID_ALLOWED_PRINCIPALS"),
        ([dict(entry, allowed_principals=[dict(grant, user_id="bad-uuid")])], "INVALID_ALLOWED_PRINCIPALS"),
    ):
        monkeypatch.setenv("LOCAL_AGENT_OPENCODE_INSTANCES", json.dumps(invalid))
        with pytest.raises(SettingsValidationError, match=reason):
            Settings.load()


@pytest.mark.asyncio
async def test_http_opencode_instance_requires_tenant_and_user_grant(clean_database, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    import server
    from core.auth import AuthService, AuthorizationAction, AuthorizationService
    from core.runtime.client_event_feed import PostgresClientEventFeed
    from tests.test_runtime_execute_endpoint import _TEST_AGENT_REGISTRY, _result, _scripted_service
    from tests.test_stage6_wp2_ownership import _create_user, _disabled_rate_limiter, _token

    allowed = await _create_user(clean_database, ("USER",), tenant_id="tenant-a")
    other = await _create_user(clean_database, ("USER",), tenant_id="tenant-a")
    entry = {
        "deployment": "MANAGED", "base_url": "https://managed.example.test", "workspace": "/demo",
        "password_env": "OPENCODE_TEST_HTTP_PASSWORD",
    }
    monkeypatch.setenv("OPENCODE_TEST_HTTP_PASSWORD", "test-password")
    monkeypatch.setenv("LOCAL_AGENT_OPENCODE_INSTANCES", json.dumps([
        dict(entry, instance_id="managed", allowed_principals=[
            {"tenant_id": "tenant-a", "user_id": str(allowed)},
            {"tenant_id": "tenant-b", "user_id": str(other)},
        ]),
        dict(entry, instance_id="unlisted"),
    ]))
    private_key = Ed25519PrivateKey.generate()
    settings = replace(
        Settings.load(), jwt_issuer="test-issuer", jwt_audience="test-api",
        jwt_public_key=private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
    )
    authenticator = AuthService(clean_database, settings)
    authz = AuthorizationService(clean_database)
    runs = ExternalRunRepository(clean_database, PostgresClientEventFeed(clean_database))
    native_run = str(uuid4())
    chat, native_factory, _ = _scripted_service(_result(RunStatus.SUCCEEDED, StopReason.COMPLETED, run_id=native_run))
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        assert request.url.host == "managed.example.test" and request.url.params["directory"] == "/demo"
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_granted", "directory": "/demo"})
        prompt = json.loads(request.content)
        return httpx.Response(200, json=_assistant("ses_granted", prompt["messageID"], "authorized reply"))

    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server.app.state, "auth_service", authenticator, raising=False)
    monkeypatch.setattr(server.app.state, "authorization_service", authz, raising=False)
    monkeypatch.setattr(server.app.state, "rate_limiter", _disabled_rate_limiter(), raising=False)
    monkeypatch.setattr(server.app.state, "chat_service", chat, raising=False)
    headers_allowed = {"Authorization": f"Bearer {_token(private_key, allowed, ['USER'])}"}
    headers_other = {"Authorization": f"Bearer {_token(private_key, other, ['USER'])}"}
    payload = {"agent_id": "core_router", "query": "test", "timeout_seconds": 30}

    async with AsyncExitStack() as stack:
        adapters = {}
        for instance in settings.opencode_instances:
            adapter = OpenCodeRuntimeAdapter(
                instance, runs=runs, run_registry=chat.run_registry, transport=httpx.MockTransport(handle),
            )
            stack.push_async_callback(adapter.close)
            adapters[instance.instance_id] = adapter
        application = AgentApplicationService(chat, _TEST_AGENT_REGISTRY, runtime_instances=adapters, external_runs=runs)
        monkeypatch.setattr(server.app.state, "agent_application_service", application, raising=False)
        client = await stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test",
        ))

        for instance_id, headers in (("managed", headers_other), ("unlisted", headers_allowed)):
            run_id = str(uuid4())
            denied = await client.post(
                "/api/runtime/execute", json=dict(payload, runtime_instance_id=instance_id, run_id=run_id),
                headers=headers,
            )
            assert denied.status_code == 403 and denied.json()["error"]["code"] == "AUTHORIZATION_FORBIDDEN"
            await authz.authorize(await authenticator.authenticate(headers["Authorization"]),
                                  "RUN", run_id, AuthorizationAction.PROCESS)
            assert calls == []

        response = await client.post("/api/runtime/execute", json=dict(payload, run_id=native_run), headers=headers_other)
        assert response.status_code == 200 and response.json()["status"] == "SUCCEEDED"
        assert response.json()["run_id"] == native_run and native_factory.create_count == 1
        assert calls == []

        run_id = str(uuid4())
        response = await client.post(
            "/api/runtime/execute", json=dict(payload, runtime_instance_id="managed", run_id=run_id),
            headers=headers_allowed,
        )
        assert response.status_code == 200
        assert response.json()["run_id"] == run_id and response.json()["status"] == "SUCCEEDED"
        assert response.json()["stop_reason"] == "COMPLETED"
        assert native_factory.create_count == 1
        assert calls == [("POST", "/session"), ("POST", "/session/ses_granted/message")]
