import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from starlette.requests import Request

from core.agent_platform.application import AgentApplicationError, ExecutionRequest
from core.agent_platform.opencode_runtime_adapter import OpenCodeRuntimeAdapter
from core.auth import AuthService, AuthorizationService
from core.persistence.repositories.external_runs import ExternalRunRepository
from core.runtime.cancellation import CancellationReason
from core.runtime.client_event_feed import PostgresClientEventFeed
from core.runtime.memory_scope import MemoryAccessContext
from core.runtime.run_control import DurableRunControlService, RunControlConflict
from core.runtime.run_registry import RunRegistry
from core.runtime.state import RunStatus, StopReason
from tests.test_opencode_runtime_adapter import _adapter, _application, _assistant, _instance
from tests.test_stage6_wp2_ownership import _create_user, _disabled_rate_limiter, _principal, _token


class _Events(httpx.AsyncByteStream):
    def __init__(self):
        self.events = asyncio.Queue()
        self.closed = asyncio.Event()

    def send(self, event_id, event_type, **properties):
        self.events.put_nowait(
            ("data: " + json.dumps({"id": event_id, "type": event_type, "properties": properties}) + "\n\n").encode()
        )

    async def __aiter__(self):
        while (data := await self.events.get()) is not None:
            yield data

    async def aclose(self):
        self.closed.set()


async def _until(probe):
    async with asyncio.timeout(3):
        while not await probe():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["native", "opencode"])
async def test_v1_chat_admits_only_one_runtime_for_the_same_run(clean_database, tmp_path, monkeypatch, winner):
    import server
    from core.agent_platform.application import AgentApplicationService
    from core.persistence.models import RunControlRow
    from core.runtime.model_invocation import ModelAdapterResponse
    from core.runtime.run_coordinator import RunCoordinator
    from tests.test_runtime_adapter import _native_application

    _, chat, factory, model, _ = await _native_application(
        clean_database, tmp_path, [ModelAdapterResponse("native answer", finish_reason="stop", tool_call_count=0)],
    )
    owner = await _create_user(clean_database, ("USER",), tenant_id="tenant-a")
    instance = replace(_instance(), allowed_principals=frozenset({("tenant-a", owner)}))
    private = Ed25519PrivateKey.generate()
    settings = replace(
        server.settings, opencode_instances=(instance,), jwt_issuer="test-issuer", jwt_audience="test-api",
        jwt_public_key=private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
    )
    headers = {"Authorization": f"Bearer {_token(private, owner, ['USER'])}"}
    services = factory.services
    feed = services.client_event_feed
    runs = ExternalRunRepository(clean_database, feed)
    supervisor = server.RunExecutionSupervisor(max_active_runs=2)
    run_id = str(uuid4())
    permit_execution = asyncio.Event()
    arrived = {runtime: asyncio.Event() for runtime in ("native", "opencode")}
    first_claimed = asyncio.Event()
    native_claim = services.durable_run_control.claim
    external_claim = runs.claim
    calls = []

    async def enter_claim(runtime, other):
        arrived[runtime].set()
        await arrived[other].wait()
        if runtime != winner:
            await first_claimed.wait()

    async def claim_native(claimed_run_id, owner_id, **kwargs):
        await enter_claim("native", "opencode")
        lease = await native_claim(claimed_run_id, owner_id, **kwargs)
        first_claimed.set()
        return lease

    async def claim_external(*args):
        await enter_claim("opencode", "native")
        result = await external_claim(*args)
        first_claimed.set()
        return result

    plan = RunCoordinator._prepare_dynamic_execution

    async def wait_for_plan(coordinator):
        await permit_execution.wait()
        return await plan(coordinator)

    async def upstream(request):
        path = request.url.path
        calls.append((request.method, path))
        if path == "/session":
            return httpx.Response(200, json={"id": "ses_admitted", "directory": instance.workspace})
        if path == "/event":
            return httpx.Response(200, headers={"content-type": "text/event-stream"})
        assert path == "/session/ses_admitted/message"
        await permit_execution.wait()
        user_message_id = json.loads(request.content)["messageID"]
        return httpx.Response(200, json=_assistant("ses_admitted", user_message_id, "opencode answer"))

    adapter = OpenCodeRuntimeAdapter(
        instance, runs=runs, run_registry=services.run_registry, transport=httpx.MockTransport(upstream),
    )
    application = AgentApplicationService(
        chat, factory._agent_registry, runtime_instances={"local": adapter}, external_runs=runs,
    )
    monkeypatch.setattr(services.durable_run_control, "claim", claim_native)
    monkeypatch.setattr(runs, "claim", claim_external)
    monkeypatch.setattr(RunCoordinator, "_prepare_dynamic_execution", wait_for_plan)
    monkeypatch.setattr(server, "settings", settings)
    for name, value in {
        "agent_application_service": application, "chat_service": chat, "runtime_services": services,
        "auth_service": AuthService(clean_database, settings), "authorization_service": AuthorizationService(clean_database),
        "rate_limiter": _disabled_rate_limiter(), "run_execution_supervisor": supervisor,
        "client_event_feed": feed, "external_runs": runs,
    }.items():
        monkeypatch.setattr(server.app.state, name, value, raising=False)

    native_payload = {"agent_id": "core_router", "query": "test", "run_id": run_id}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
            async with asyncio.timeout(6):
                native, external = await asyncio.gather(
                    client.post("/api/v1/chat", json=native_payload, headers=headers),
                    client.post("/api/v1/chat", json=dict(native_payload, runtime_instance_id="local"), headers=headers),
                )
            accepted, rejected = (native, external) if winner == "native" else (external, native)
            assert accepted.status_code == 200 and accepted.json()["run_id"] == run_id
            assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "RUN_ALREADY_ACCEPTED"
            assert "events_url" not in rejected.json()
            assert [event.event_type for event in await feed.read_after(run_id, 0)] == ["run.started"]
            if winner == "native":
                active_duplicate = await client.post("/api/v1/chat", json=native_payload, headers=headers)
                assert active_duplicate.status_code == 409
                assert active_duplicate.json()["error"]["code"] == "RUN_ALREADY_ACCEPTED"

            permit_execution.set()
            await asyncio.wait_for(supervisor.close(), timeout=5)
            assert not services.run_registry.active_handles()
            assert all(scope.is_closed for scope in factory.scopes)

            completed_feed = await feed.read_after(run_id, 0)
            old_native = await client.post("/api/v1/chat", json=native_payload, headers=headers)
            assert old_native.status_code == 409 and old_native.json()["error"]["code"] == "RUN_ALREADY_ACCEPTED"
            assert "events_url" not in old_native.json()
            assert await feed.read_after(run_id, 0) == completed_feed
            assert sum(event.event_type == "run.completed" for event in completed_feed) == 1
            replay = await client.get(accepted.json()["events_url"], headers=headers)
            assert f"{winner} answer" in replay.text and "event: run.completed" in replay.text
            if winner == "native":
                assert len(model.requests) == len(factory.scopes) == 1
                assert not calls and await runs.get(run_id) is None
                async with clean_database.session() as session:
                    assert (await session.get(RunControlRow, run_id)).fencing_token == 1
            else:
                assert not model.requests and not factory.scopes
                assert calls.count(("POST", "/session")) == calls.count(("POST", "/session/ses_admitted/message")) == 1
                assert (await runs.get(run_id)).terminal_status == "SUCCEEDED"
    finally:
        permit_execution.set()
        first_claimed.set()
        await supervisor.close()
        await adapter.close()


@pytest.mark.asyncio
async def test_external_v1_feed_and_result_survive_application_restart(clean_database, monkeypatch):
    import server

    owner = await _create_user(clean_database, ("USER",), tenant_id="tenant-a")
    other = await _create_user(clean_database, ("USER",), tenant_id="tenant-a")
    instance = replace(_instance(), allowed_principals=frozenset({("tenant-a", owner), ("tenant-a", other)}))
    private = Ed25519PrivateKey.generate()
    settings = replace(
        server.settings, opencode_instances=(instance,), jwt_issuer="test-issuer", jwt_audience="test-api",
        jwt_public_key=private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
    )
    headers = {"Authorization": f"Bearer {_token(private, owner, ['USER'])}"}
    foreign_headers = {"Authorization": f"Bearer {_token(private, other, ['USER'])}"}
    feed = PostgresClientEventFeed(clean_database)
    runs = ExternalRunRepository(clean_database, feed)
    control = DurableRunControlService(clean_database)
    registry = RunRegistry()
    supervisor = server.RunExecutionSupervisor()
    sse = _Events()
    run_id = str(uuid4())
    calls = []
    final = None
    permit_completion = asyncio.Event()
    transport_available = True
    wrong_parent = False

    async def upstream(request):
        nonlocal final
        calls.append((request.method, request.url.path))
        assert request.url.params["directory"] == instance.workspace
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_local", "directory": instance.workspace})
        if request.url.path == "/event":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=sse)
        if request.method == "GET":
            assert request.url.path == "/session/ses_local/message/msg_assistantlocal"
            if not transport_available:
                raise httpx.ConnectError("private error", request=request)
            return httpx.Response(200, json={**final, "info": {**final["info"], "parentID": "msg_wrong"}} if wrong_parent else final)
        assert request.url.path == "/session/ses_local/message"
        user_message = json.loads(request.content)["messageID"]
        final = _assistant("ses_local", user_message, "hello world")
        assistant_id = final["info"]["id"]
        part = {"id": "prt_good", "sessionID": "ses_local", "messageID": assistant_id, "type": "text", "text": ""}
        sse.send("other", "message.updated", info={**final["info"], "sessionID": "ses_other"})
        sse.send("assistant", "message.updated", info=final["info"])
        sse.send("part", "message.part.updated", part=part)
        delta = {"messageID": assistant_id, "partID": "prt_good", "field": "text"}
        sse.send("foreign", "message.part.delta", sessionID="ses_other", delta="secret", **delta)
        sse.send("busy", "session.status", sessionID="ses_local", status={"type": "busy"})
        sse.send("text", "message.part.delta", sessionID="ses_local", delta="hello", **delta)
        sse.send("text", "message.part.delta", sessionID="ses_local", delta="hello", **delta)
        sse.events.put_nowait(None)
        await permit_completion.wait()
        return httpx.Response(200, json=final)

    adapter = OpenCodeRuntimeAdapter(instance, runs=runs, run_registry=registry, transport=httpx.MockTransport(upstream))
    application = _application(("local", adapter), external_runs=runs)
    monkeypatch.setattr(server, "settings", settings)
    for name, value in {
        "agent_application_service": application, "chat_service": SimpleNamespace(run_registry=registry),
        "auth_service": AuthService(clean_database, settings),
        "authorization_service": AuthorizationService(clean_database), "rate_limiter": _disabled_rate_limiter(),
        "run_execution_supervisor": supervisor, "client_event_feed": feed, "external_runs": runs,
        "runtime_services": SimpleNamespace(durable_run_control=control),
    }.items():
        monkeypatch.setattr(server.app.state, name, value, raising=False)
    payload = {"run_id": run_id, "runtime_instance_id": "local", "agent_id": "core_router", "query": "test"}

    async def prefix_available():
        return any(event.payload.get("text") == "hello" for event in await feed.read_after(run_id, 0))

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
            accepted = await client.post("/api/v1/chat", json=payload, headers=headers)
            assert accepted.status_code == 200
            await _until(prefix_available)
            prefix = await feed.read_after(run_id, 0)
            assert [(event.event_type, event.payload) for event in prefix[-2:]] == [
                ("run.progress", {"phase": "session.busy"}), ("output.delta", {"text": "hello"}),
            ]
            await asyncio.wait_for(sse.closed.wait(), timeout=2)
            assert (await runs.get(run_id)).observation_state == "ACTIVE"
            rejected = await client.post("/api/v1/chat", json=payload, headers=headers)
            assert rejected.status_code == 409
            with pytest.raises(RunControlConflict):
                await control.claim(run_id, "native")
            permit_completion.set()
            await asyncio.wait_for(supervisor.close(), timeout=2)
            assert registry.get(run_id) is None
            row = await runs.get(run_id)
            assert (row.instance_id, row.external_session_id, row.user_message_id, row.assistant_message_id) == (
                "local", "ses_local", final["info"]["parentID"], final["info"]["id"],
            )
            assert row.terminal_status == "SUCCEEDED"
            events = await client.get(accepted.json()["events_url"], headers={**headers, "Last-Event-ID": str(prefix[-1].cursor)})
            assert '"text":" world"' in events.text and "event: run.completed" in events.text
            assert "secret" not in events.text and '"text":"hello"' not in events.text
            assert calls.count(("POST", "/session/ses_local/message")) == 1

            assert (await client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)).json()["status"] == "already_completed"
            assert not any(path.endswith("/abort") for _, path in calls)
            await adapter.close()
            restarted_runs = ExternalRunRepository(clean_database, PostgresClientEventFeed(clean_database))
            restarted_adapter = OpenCodeRuntimeAdapter(
                instance, runs=restarted_runs, run_registry=RunRegistry(), transport=httpx.MockTransport(upstream),
            )
            try:
                monkeypatch.setattr(server.app.state, "agent_application_service", _application(
                    ("local", restarted_adapter), external_runs=restarted_runs,
                ))
                url = f"/api/v1/runs/{run_id}/result"
                count = len(calls)
                assert (await client.get(url, headers=foreign_headers)).status_code == 404
                assert len(calls) == count
                wrong_parent = True
                rejected = (await client.get(url, headers=headers)).json()
                assert rejected["output"] is None and rejected["read_error_code"] == "OPENCODE_RESULT_UNKNOWN"
                wrong_parent = False
                transport_available = False
                unavailable = (await client.get(url, headers=headers)).json()
                assert unavailable["output"] is None and unavailable["read_error_code"] == "OPENCODE_RESULT_UNKNOWN"
                assert unavailable["status"] == "SUCCEEDED"
                transport_available = True
                recovered = (await client.get(url, headers=headers)).json()
                assert recovered["output"] == "hello world" and recovered["status"] == "SUCCEEDED"
                assert sum(event.event_type == "run.completed" for event in await feed.read_after(run_id, 0)) == 1
                changed_adapter = OpenCodeRuntimeAdapter(
                    replace(instance, workspace="/other"), runs=restarted_runs,
                    run_registry=RunRegistry(), transport=httpx.MockTransport(upstream),
                )
                try:
                    previous_calls = len(calls)
                    changed = _application(("local", changed_adapter), external_runs=restarted_runs)
                    result = await changed.read_external_result(run_id)
                    assert result["output"] is None and result["read_error_code"] == "OPENCODE_RESULT_UNKNOWN"
                    assert len(calls) == previous_calls
                finally:
                    await changed_adapter.close()
            finally:
                await restarted_adapter.close()
    finally:
        permit_completion.set()
        await supervisor.close()
        await adapter.close()


@pytest.mark.asyncio
async def test_prompt_completion_closes_an_open_event_stream():
    instance = _instance()
    sse = _Events()

    def upstream(request):
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_live", "directory": instance.workspace})
        if request.url.path == "/event":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=sse)
        prompt = json.loads(request.content)
        return httpx.Response(200, json=_assistant("ses_live", prompt["messageID"], "answer"))

    adapter = _adapter(instance, httpx.MockTransport(upstream))
    app = _application(("local", adapter))
    try:
        request = ExecutionRequest("core_router", "test", runtime_instance_id="local")
        events = [event async for event in app.stream(request)]
        assert events[-1].status is RunStatus.SUCCEEDED
        assert sse.closed.is_set()
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_cancel_route_preserves_unknown_until_a_bound_abort_message_exists(clean_database, monkeypatch):
    import server
    from tests._runtime_assembly_fixtures import make_services

    user = await _create_user(clean_database, ("USER",))
    principal = _principal(user, "USER")
    authz = AuthorizationService(clean_database)
    registry = RunRegistry()
    feed = PostgresClientEventFeed(clean_database)
    runs = ExternalRunRepository(clean_database, feed)
    control = DurableRunControlService(clean_database)
    instance = _instance()
    run_id = str(uuid4())
    await authz.bind_new(principal, "RUN", run_id)
    prompt_sent = asyncio.Event()
    user_message_id = None
    confirmed = False
    requests = []

    async def upstream(request):
        nonlocal user_message_id
        requests.append((request.method, request.url.path))
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_abort", "directory": instance.workspace})
        if request.url.path.endswith("/abort"):
            return httpx.Response(503)
        if request.method == "GET":
            if not confirmed:
                return httpx.Response(200, json=[])
            message = _assistant("ses_abort", user_message_id, "")
            message["info"]["error"] = {"name": "MessageAbortedError"}
            return httpx.Response(200, json=[message])
        user_message_id = json.loads(request.content)["messageID"]
        prompt_sent.set()
        await asyncio.Future()

    adapter = OpenCodeRuntimeAdapter(instance, runs=runs, run_registry=registry, transport=httpx.MockTransport(upstream))
    application = _application(("local", adapter), external_runs=runs)
    for name, value in {
        "authorization_service": authz, "chat_service": SimpleNamespace(run_registry=registry),
        "runtime_services": replace(make_services(), durable_run_control=control, run_registry=registry),
        "external_runs": runs, "agent_application_service": application,
    }.items():
        monkeypatch.setattr(server.app.state, name, value, raising=False)
    request = Request({"type": "http", "app": server.app})
    request.state.principal = principal
    access = MemoryAccessContext.from_principal(principal, "core_router")
    task = asyncio.create_task(application.execute(
        ExecutionRequest("core_router", "test", run_id=run_id, runtime_instance_id="local"),
        memory_access_context=access,
    ))
    try:
        await asyncio.wait_for(prompt_sent.wait(), timeout=2)
        response = await server.cancel_run_endpoint(run_id, request)
        assert response == {"status": "cancellation_requested", "run_id": run_id}
        with pytest.raises(AgentApplicationError, match="OPENCODE_RESULT_UNKNOWN"):
            await asyncio.wait_for(task, timeout=2)
        row = await runs.get(run_id)
        assert (row.observation_state, row.terminal_status, row.cancel_reason) == ("UNKNOWN", None, "REQUEST_CANCELLED")
        assert (await control.cancel_intent(run_id)).reason == "REQUEST_CANCELLED"
        assert (await feed.read_after(run_id, 0))[-1].event_type == "run.observation_lost"
        assert requests.count(("POST", "/session/ses_abort/abort")) == 1
        assert requests.count(("POST", "/session/ses_abort/message")) == 1
        assert registry.get(run_id) is None
        confirmed = True
        recovered = await application.read_external_result(run_id)
        assert recovered["status"] == "CANCELLED" and recovered["output"] is None
        assert (await feed.read_after(run_id, 0))[-1].event_type == "run.cancelled"
    finally:
        registry.cancel(run_id, CancellationReason.SERVER_SHUTDOWN)
        await asyncio.gather(task, return_exceptions=True)
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "deadline", "abort_unconfirmed", "during_session"])
async def test_external_cancel_is_scoped_and_requires_completion_evidence(mode):
    local, managed = _instance(), _instance("managed", "MANAGED", 4097)
    registry = RunRegistry()
    session_started = asyncio.Event()
    finish_session = asyncio.Event()
    both_prompts = asyncio.Event()
    managed_done = asyncio.Event()
    active = {}
    aborted = []
    replies = {}

    async def upstream(request):
        port = request.url.port
        instance = local if port == 4096 else managed
        session_id = f"ses_{instance.instance_id}"
        assert request.url.params["directory"] == instance.workspace
        if request.url.path == "/session":
            if mode == "during_session" and port == 4096:
                session_started.set()
                await finish_session.wait()
            return httpx.Response(200, json={"id": session_id, "directory": instance.workspace})
        if request.url.path.endswith("/abort"):
            aborted.append((port, request.url.path))
            if mode == "abort_unconfirmed":
                return httpx.Response(503)
            replies[port] = _assistant(session_id, active[port], "")
            replies[port]["info"]["error"] = {"name": "MessageAbortedError"}
            return httpx.Response(200, json=True)
        if request.method == "GET":
            return httpx.Response(200, json=[replies[port]] if port in replies else [])
        prompt = json.loads(request.content)
        active[port] = prompt["messageID"]
        if len(active) == 2:
            both_prompts.set()
        if port == 4097:
            await managed_done.wait()
            return httpx.Response(200, json=_assistant(session_id, active[port], "other run"))
        await asyncio.Future()

    local_adapter = _adapter(local, httpx.MockTransport(upstream), registry)
    managed_adapter = _adapter(managed, httpx.MockTransport(upstream), registry)
    request = ExecutionRequest(
        "core_router", "test", run_id="run-local", runtime_instance_id="local",
        timeout_seconds=0.25 if mode == "deadline" else 3,
    )
    app = _application(("local", local_adapter), ("managed", managed_adapter))
    task = asyncio.create_task(app.execute(request))
    neighbour = None
    try:
        if mode == "during_session":
            await asyncio.wait_for(session_started.wait(), timeout=2)
            assert registry.cancel("run-local", CancellationReason.REQUEST_CANCELLED) is True
            finish_session.set()
        else:
            neighbour = asyncio.create_task(app.execute(
                replace(request, run_id="run-managed", runtime_instance_id="managed", timeout_seconds=4)
            ))
            await asyncio.wait_for(both_prompts.wait(), timeout=2)
            if mode != "deadline":
                assert registry.cancel("run-local", CancellationReason.REQUEST_CANCELLED) is True
        if mode == "abort_unconfirmed":
            with pytest.raises(AgentApplicationError, match="OPENCODE_RESULT_UNKNOWN"):
                await asyncio.wait_for(task, timeout=2)
            local_adapter._runs.observe_unknown.assert_awaited_once_with("run-local")
        else:
            result = await asyncio.wait_for(task, timeout=2)
            if mode == "deadline":
                assert (result.status, result.stop_reason) == (RunStatus.FAILED, StopReason.DEADLINE_EXCEEDED)
            else:
                assert result.status is RunStatus.CANCELLED
        assert aborted == ([] if mode == "during_session" else [(4096, "/session/ses_local/abort")])
        assert registry.get("run-local") is None
        if neighbour is not None:
            assert registry.get("run-managed") is not None
            managed_done.set()
            assert (await neighbour).output == "other run"
        else:
            assert not active
        assert registry.active_handles() == ()
    finally:
        finish_session.set()
        managed_done.set()
        await asyncio.gather(task, *((neighbour,) if neighbour is not None else ()), return_exceptions=True)
        await local_adapter.close()
        await managed_adapter.close()


@pytest.mark.asyncio
async def test_durable_pre_cancel_and_native_ownership_prevent_external_dispatch(clean_database):
    instance = _instance()
    registry = RunRegistry()
    runs = ExternalRunRepository(clean_database, PostgresClientEventFeed(clean_database))
    control = DurableRunControlService(clean_database)
    calls = []

    def reject_http(request):
        calls.append(request)
        raise AssertionError("unexpected external request")

    adapter = OpenCodeRuntimeAdapter(instance, runs=runs, run_registry=registry, transport=httpx.MockTransport(reject_http))
    app = _application(("local", adapter), external_runs=runs)
    ctx = MemoryAccessContext("tenant-a", str(uuid4()), "core_router", "default")
    run_id = str(uuid4())
    request = ExecutionRequest("core_router", "test", run_id=run_id, runtime_instance_id="local")
    try:
        await control.request_cancel(run_id, CancellationReason.REQUEST_CANCELLED.value)
        result = await app.execute(request, memory_access_context=ctx)
        row = await runs.get(run_id)
        assert result.status is RunStatus.CANCELLED and row.terminal_status == "CANCELLED"
        assert row.external_session_id is None
        await runs.finish(replace(result, status=RunStatus.SUCCEEDED, stop_reason=StopReason.COMPLETED))
        assert (await runs.get(run_id)).terminal_status == "CANCELLED"

        native_run = str(uuid4())
        await control.claim(native_run, "native")
        with pytest.raises(AgentApplicationError, match="RUN_ALREADY_ACCEPTED"):
            await app.execute(replace(request, run_id=native_run), memory_access_context=ctx)
        assert await runs.get(native_run) is None
        assert not calls and registry.active_handles() == ()
    finally:
        await adapter.close()
