import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from core.agent_platform.application import AgentApplicationError, AgentApplicationService, ExecutionRequest
from core.agent_platform.opencode_runtime_adapter import OpenCodeRuntimeAdapter
from core.agent_platform.registrations import BUSINESS_REGISTRATION_BUNDLE
from core.agent_platform.runtime_adapter import RuntimeRequest
from core.agent_platform.workflow import WorkflowError, WorkflowService
from core.auth import AuthError, AuthService, AuthorizationService
from core.chat_service import ChatRuntimeTransportError
from core.persistence.models import RunControlRow
from core.persistence.repositories.external_runs import ExternalRunRepository
from core.persistence.repositories.workflows import WorkflowRepository
from core.runtime.memory_scope import MemoryAccessContext
from core.runtime.model_invocation import ModelAdapterResponse
from core.runtime.run_control import RunControlConflict
from tests.test_opencode_runtime_adapter import _assistant, _instance
from tests.test_runtime_adapter import _native_application
from tests.test_stage6_wp2_ownership import _create_user, _disabled_rate_limiter, _principal, _token


DEFINITION, = BUSINESS_REGISTRATION_BUNDLE.workflows


async def _setup(database, tmp_path, upstream, *, capacity=2):
    import server

    owner = _principal(await _create_user(database, ("USER",)), "USER")
    _, chat, factory, model, _ = await _native_application(
        database, tmp_path, [ModelAdapterResponse("native result", finish_reason="stop", tool_call_count=0)],
    )
    instance = replace(_instance("workflow-opencode"), allowed_principals=frozenset({(owner.tenant_id, owner.user_id)}))
    services = factory.services
    runs = ExternalRunRepository(database, services.client_event_feed)
    adapter = OpenCodeRuntimeAdapter(instance, runs=runs, run_registry=services.run_registry, transport=httpx.MockTransport(upstream))
    application = AgentApplicationService(chat, factory._agent_registry, runtime_instances={instance.instance_id: adapter}, external_runs=runs)
    authz = AuthorizationService(database)
    supervisor = server.RunExecutionSupervisor(max_active_runs=capacity)

    def service(definition=DEFINITION):
        return WorkflowService(WorkflowRepository(database), {definition.workflow_id: definition}, application,
                               authz, supervisor, services.run_registry, services.admission_gate)

    return owner, application, services, runs, adapter, instance, model, service, chat, supervisor, authz


async def _when(service, owner, run_id, predicate):
    async with asyncio.timeout(8):
        while True:
            view = await service.get(owner, run_id)
            if predicate(view):
                return view
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_serial_workflow_api_reserves_ids_and_waits_for_durable_completion(clean_database, tmp_path, monkeypatch):
    import server

    calls = []
    before_claim, allow_claim = asyncio.Event(), asyncio.Event()
    before_terminal, allow_terminal = asyncio.Event(), asyncio.Event()
    before_native, allow_native = asyncio.Event(), asyncio.Event()

    async def upstream(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_workflow", "directory": instance.workspace})
        assert request.url.path == "/session/ses_workflow/message"
        user_message = json.loads(request.content)["messageID"]
        return httpx.Response(200, json=_assistant("ses_workflow", user_message, "external result"))

    owner, app, services, runs, adapter, instance, model, build, chat, supervisor, authz = await _setup(clean_database, tmp_path, upstream)
    service = build()
    private = Ed25519PrivateKey.generate()
    settings = replace(server.settings, opencode_instances=(instance,), jwt_issuer="test-issuer", jwt_audience="test-api",
                       jwt_public_key=private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode())
    monkeypatch.setattr(server, "settings", settings)
    for key, value in {
        "workflow_service": service, "agent_application_service": app, "chat_service": chat,
        "auth_service": AuthService(clean_database, settings), "authorization_service": authz,
        "rate_limiter": _disabled_rate_limiter(), "run_execution_supervisor": supervisor,
    }.items():
        monkeypatch.setattr(server.app.state, key, value, raising=False)

    execute, finish = app.execute, runs.finish
    native_request = None

    async def held_execute(request, **kwargs):
        nonlocal native_request
        if request.runtime_instance_id is not None:
            before_claim.set()
            await allow_claim.wait()
        else:
            native_request = (request, kwargs)
            before_native.set()
            await allow_native.wait()
        return await execute(request, **kwargs)

    async def held_finish(result):
        before_terminal.set()
        await allow_terminal.wait()
        return await finish(result)

    monkeypatch.setattr(app, "execute", held_execute)
    monkeypatch.setattr(runs, "finish", held_finish)
    payload = {"request_id": str(uuid4()), "expected_workflow_version": "1"}
    create_route = f"/api/v1/workflows/{DEFINITION.workflow_id}/runs"
    headers = {"Authorization": f"Bearer {_token(private, owner.user_id, ['USER'])}"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test", headers=headers) as client:
            first, duplicate = await asyncio.gather(*(client.post(create_route, json=payload) for _ in range(2)))
            assert first.status_code == duplicate.status_code == 202, first.text
            run_id = first.json()["workflow_run_id"]
            assert duplicate.json()["workflow_run_id"] == run_id
            await asyncio.wait_for(before_claim.wait(), 4)
            route = f"/api/v1/workflow-runs/{run_id}"
            step, pending = (await client.get(route)).json()["steps"]
            child = step["platform_run_id"]
            assert pending["platform_run_id"] is None and await runs.get(child) is None
            for target in ({}, {"runtime_instance_id": instance.instance_id}):
                ordinary = await client.post("/api/v1/chat", json={"agent_id": "core_router", "query": "ordinary", "run_id": child, **target})
                assert ordinary.status_code == 409, ordinary.text
            assert not calls and not model.requests
            allow_claim.set()
            await asyncio.wait_for(before_terminal.wait(), 4)
            for response in await asyncio.gather(*(client.post(f"{route}/resume") for _ in range(2))):
                assert response.status_code == 202
                assert response.json()["steps"][1]["platform_run_id"] is None
            assert not model.requests and (await runs.get(child)).terminal_status is None
            allow_terminal.set()
            await asyncio.wait_for(before_native.wait(), 4)
            reserved_native = (await client.get(route)).json()["steps"][1]["platform_run_id"]
            ordinary = await client.post("/api/v1/chat", json={"agent_id": "core_router", "query": "ordinary", "run_id": reserved_native})
            assert ordinary.status_code == 409, ordinary.text
            request, native_kwargs = native_request
            different_agent = "code_expert"
            different_access = replace(native_kwargs["memory_access_context"], agent_id=different_agent)
            with pytest.raises(ChatRuntimeTransportError, match="RUN_ALREADY_ACCEPTED"):
                await execute(replace(request, agent_id=different_agent, expected_agent_version=None),
                              **{**native_kwargs, "memory_access_context": different_access})
            with pytest.raises(ChatRuntimeTransportError, match="RUN_ALREADY_ACCEPTED"):
                await execute(replace(request, input="wrong Native input"), **native_kwargs)
            with pytest.raises(ChatRuntimeTransportError, match="RUN_ALREADY_ACCEPTED"):
                await execute(request, **{**native_kwargs, "retrieval_cache_authz_domain": str(uuid4())})
            with pytest.raises(RunControlConflict):
                await services.durable_run_control.claim(reserved_native, "wrong-version", workflow_request=RuntimeRequest(
                    request.agent_id, request.input, "wrong-version", run_id=reserved_native, session_id=request.session_id,
                    memory_access_context=native_kwargs["memory_access_context"],
                    workflow_dispatch_token=native_kwargs["workflow_dispatch_token"],
                ))
            async with clean_database.session() as session:
                assert await session.get(RunControlRow, reserved_native) is None
            allow_native.set()
            final = await _when(service, owner, run_id, lambda view: view["status"] in {"SUCCEEDED", "FAILED", "PAUSED"})
            assert final["status"] == "SUCCEEDED" and final["result_available"], final
            external, native = final["steps"]
            assert external["platform_run_id"] == child != native["platform_run_id"] == reserved_native
            assert [step["runtime_kind"] for step in final["steps"]] == ["OPENCODE", "NATIVE"]
            assert (await build().get(owner, run_id)) == final
            assert (await client.post(create_route, json=payload)).json() == final
            assert len(model.requests) == 1
            assert calls == [("POST", "/session"), ("POST", "/session/ses_workflow/message")]
            native_id = native["platform_run_id"]
            with pytest.raises(ChatRuntimeTransportError, match="RUN_ALREADY_ACCEPTED"):
                await execute(ExecutionRequest("core_router", "duplicate", run_id=native_id),
                              require_new_run=True, memory_access_context=MemoryAccessContext.from_principal(owner, "core_router"))
            assert len(model.requests) == 1
    finally:
        allow_claim.set()
        allow_terminal.set()
        allow_native.set()
        await service.close()
        await adapter.close()


@pytest.mark.asyncio
async def test_unknown_is_reconciled_without_prompt_replay_and_resume_requires_owner(clean_database, tmp_path, monkeypatch):
    calls = []
    message_id = None
    confirmed = False

    async def upstream(request):
        nonlocal message_id
        calls.append((request.method, request.url.path))
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_unknown", "directory": instance.workspace})
        if request.method == "POST":
            message_id = json.loads(request.content)["messageID"]
            return httpx.Response(200, json={})
        return httpx.Response(200, json=[_assistant("ses_unknown", message_id, "confirmed")] if confirmed else [])

    owner, _, _, runs, adapter, instance, model, build, *_ = await _setup(clean_database, tmp_path, upstream)
    service, restarted, recovered = build(), build(), build()
    admin = _principal(await _create_user(clean_database, ("ADMIN",)), "ADMIN")
    other = _principal(await _create_user(clean_database, ("ADMIN",), tenant_id="other"), "ADMIN", tenant_id="other")
    try:
        response = await service.create(owner, DEFINITION.workflow_id, uuid4())
        run_id = response["workflow_run_id"]
        unknown = await _when(service, owner, run_id, lambda view: view["status"] == "PAUSED")
        child = unknown["steps"][0]["platform_run_id"]
        assert unknown["reason"] == "OPENCODE_RESULT_UNKNOWN"
        assert (await runs.get(child)).observation_state == "UNKNOWN"
        await service.close()
        assert await restarted.get(admin, run_id) == unknown
        with pytest.raises(AuthError):
            await restarted.resume(admin, run_id)
        with pytest.raises(AuthError):
            await restarted.get(other, run_id)
        with pytest.raises(AuthError):
            await restarted.create(admin, DEFINITION.workflow_id, uuid4())

        # 模拟应用在首步已落库与 Native 接纳之间关闭。
        monkeypatch.setattr(restarted._repository, "prepare", _stop_second(restarted._repository.prepare))
        confirmed = True
        await restarted.resume(owner, run_id)
        saved = await _when(restarted, owner, run_id, lambda view: view["steps"][0]["status"] == "SUCCEEDED")
        assert saved["steps"][1]["platform_run_id"] is None and not model.requests
        await restarted.close()
        await asyncio.gather(recovered.resume(owner, run_id), recovered.resume(owner, run_id))
        final = await _when(recovered, owner, run_id, lambda view: view["status"] in {"SUCCEEDED", "FAILED", "PAUSED"})
        assert final["status"] == "SUCCEEDED", final
        assert final["steps"][0]["platform_run_id"] == child
        assert calls.count(("POST", "/session/ses_unknown/message")) == calls.count(("POST", "/session")) == 1
        assert ("GET", "/session/ses_unknown/message") in calls and len(model.requests) == 1
    finally:
        await service.close()
        await restarted.close()
        await recovered.close()
        await adapter.close()


def _stop_second(prepare):
    async def wait_at_second(run_id, position, *args, **kwargs):
        if position == 1:
            await asyncio.Future()
        return await prepare(run_id, position, *args, **kwargs)

    return wait_at_second


@pytest.mark.asyncio
async def test_active_external_cancel_waits_for_bound_terminal_and_blocks_native(clean_database, tmp_path):
    calls = []
    prompt = asyncio.Event()
    confirmed = False
    message_id = None

    async def upstream(request):
        nonlocal message_id
        calls.append((request.method, request.url.path))
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_cancel", "directory": instance.workspace})
        if request.url.path.endswith("/abort"):
            return httpx.Response(200)
        if request.method == "GET":
            message = _assistant("ses_cancel", message_id, "")
            message["info"]["error"] = {"name": "MessageAbortedError"}
            return httpx.Response(200, json=[message] if confirmed else [])
        message_id = json.loads(request.content)["messageID"]
        prompt.set()
        await asyncio.Future()

    owner, _, services, _, adapter, instance, model, build, *_ = await _setup(clean_database, tmp_path, upstream)
    service = build()
    admin = _principal(await _create_user(clean_database, ("ADMIN",)), "ADMIN")
    try:
        run_id = (await service.create(owner, DEFINITION.workflow_id, uuid4()))["workflow_run_id"]
        await asyncio.wait_for(prompt.wait(), 4)
        received = await service.cancel(admin, run_id)
        child = received["steps"][0]["platform_run_id"]
        assert received["status"] == "CANCELLING" and received["cancel_requested"]
        assert (await services.durable_run_control.cancel_intent(child)).reason == "REQUEST_CANCELLED"
        await _when(service, owner, run_id, lambda view: view["steps"][0]["runtime_observation"] == "UNKNOWN")
        assert (await service.resume(owner, run_id))["status"] == "CANCELLING"
        attempts = len(calls)
        assert (await service.cancel(admin, run_id))["status"] == "CANCELLING"
        assert len(calls) == attempts
        confirmed = True
        final = await service.cancel(owner, run_id)
        assert final["status"] == final["steps"][0]["runtime_status"] == "CANCELLED", final
        assert final["steps"][1]["platform_run_id"] is None and not model.requests
        assert calls.count(("POST", "/session/ses_cancel/message")) == 1
    finally:
        await service.close()
        await adapter.close()


@pytest.mark.asyncio
async def test_reserved_external_dispatch_checks_binding_rotation_and_cancel(clean_database, tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def upstream(request):
        calls.append(request)
        raise AssertionError("reserved Run must not contact OpenCode")

    owner, app, services, runs, adapter, instance, model, build, *_ = await _setup(clean_database, tmp_path, upstream)
    service = build()
    attempts = []
    execute = app.execute

    async def held_execute(request, **kwargs):
        attempts.append((request, kwargs))
        entered.set()
        await release.wait()
        return await execute(request, **kwargs)

    monkeypatch.setattr(app, "execute", held_execute)
    other_instance = replace(instance, instance_id="other-opencode")
    other = OpenCodeRuntimeAdapter(other_instance, runs=runs, run_registry=services.run_registry, transport=httpx.MockTransport(upstream))
    changed = OpenCodeRuntimeAdapter(replace(instance, agent="changed"), runs=runs,
                                     run_registry=services.run_registry, transport=httpx.MockTransport(upstream))
    monkeypatch.setitem(app._runtime_instances, other_instance.instance_id, other)
    try:
        run_id = (await service.create(owner, DEFINITION.workflow_id, uuid4()))["workflow_run_id"]
        await asyncio.wait_for(entered.wait(), 4)
        request, kwargs = attempts[0]
        access = kwargs["memory_access_context"]
        for changed_request, changed_access in (
            (replace(request, input="wrong external input"), access),
            (replace(request, runtime_instance_id=other_instance.instance_id), access),
            (request, replace(access, user_id=str(uuid4()))),
            (request, replace(access, tenant_id="other-tenant")),
            (replace(request, session_id="other-session"), replace(access, session_id="other-session")),
        ):
            with pytest.raises(AgentApplicationError, match="RUN_ALREADY_ACCEPTED"):
                await execute(changed_request, **{**kwargs, "memory_access_context": changed_access})
        monkeypatch.setitem(app._runtime_instances, instance.instance_id, changed)
        with pytest.raises(AgentApplicationError, match="RUN_ALREADY_ACCEPTED"):
            await execute(request, **kwargs)
        monkeypatch.setitem(app._runtime_instances, instance.instance_id, adapter)
        # 模拟另一进程以 fresh Principal 重新取得尚未接纳的预留。
        repository = WorkflowRepository(clean_database)
        run, steps = await repository.read(run_id)
        reserved = await repository.prepare(run_id, 0, owner, access.conversation_id,
                                            definition_digest=run.definition_digest, runtime_target_digest=steps[0].runtime_target_digest)
        assert reserved.platform_run_id == request.run_id
        assert reserved.dispatch_token != kwargs["workflow_dispatch_token"]
        with pytest.raises(AgentApplicationError, match="RUN_ALREADY_ACCEPTED"):
            await execute(request, **kwargs)
        assert (await service.cancel(owner, run_id))["status"] == "CANCELLED"
        with pytest.raises(AgentApplicationError, match="RUN_ALREADY_ACCEPTED"):
            await execute(request, **{**kwargs, "workflow_dispatch_token": reserved.dispatch_token})
        assert await runs.get(request.run_id) is None
        assert not calls and not model.requests
    finally:
        release.set()
        await service.close()
        await adapter.close()
        await other.close()
        await changed.close()


@pytest.mark.asyncio
async def test_definition_drift_and_pending_requirements_fail_closed(clean_database, tmp_path, monkeypatch):
    async def upstream(request):
        raise AssertionError("invalid definition must not contact OpenCode")

    owner, _, _, _, adapter, _, model, build, *_ = await _setup(clean_database, tmp_path, upstream)
    original = build()
    changed = build(replace(DEFINITION, tasks=(replace(DEFINITION.tasks[0], instruction="changed"), DEFINITION.tasks[1])))
    gated = build(replace(DEFINITION, tasks=(DEFINITION.tasks[0], replace(DEFINITION.tasks[1], requires_approval=True))))
    async def leave_pending(*_):
        pass

    monkeypatch.setattr(original, "_start", leave_pending)
    try:
        run_id = (await original.create(owner, DEFINITION.workflow_id, uuid4()))["workflow_run_id"]
        await changed.resume(owner, run_id)
        paused = await _when(changed, owner, run_id, lambda view: view["status"] == "PAUSED")
        assert paused["reason"] == "WORKFLOW_DEFINITION_DRIFT"
        assert all(step["platform_run_id"] is None for step in paused["steps"])
        with pytest.raises(WorkflowError, match="WORKFLOW_DEFINITION_DRIFT"):
            await changed.create(owner, DEFINITION.workflow_id, uuid4())
        with pytest.raises(WorkflowError, match="WORKFLOW_STEP_CONSTRAINT_UNSUPPORTED"):
            await gated.create(owner, DEFINITION.workflow_id, uuid4())
        assert not model.requests
    finally:
        await original.close()
        await changed.close()
        await gated.close()
        await adapter.close()


@pytest.mark.asyncio
async def test_workflow_capacity_leaves_pending_without_tasks_and_releases_on_shutdown(clean_database, tmp_path, monkeypatch):
    async def upstream(request):
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_capacity", "directory": instance.workspace})
        message = json.loads(request.content)["messageID"]
        return httpx.Response(200, json=_assistant("ses_capacity", message, "external result"))

    owner, app, _, _, adapter, instance, _, build, _, supervisor, _ = await _setup(clean_database, tmp_path, upstream, capacity=1)
    service = build()
    first_entered, finish_first, second_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    execute = app.execute

    async def hold_external(request, **kwargs):
        if request.runtime_instance_id is not None:
            if first_entered.is_set():
                second_entered.set()
                await asyncio.Future()
            first_entered.set()
            await finish_first.wait()
        return await execute(request, **kwargs)

    monkeypatch.setattr(app, "execute", hold_external)
    try:
        request_id = uuid4()
        first = (await service.create(owner, DEFINITION.workflow_id, request_id))["workflow_run_id"]
        await asyncio.wait_for(first_entered.wait(), 4)
        pending = [await service.create(owner, DEFINITION.workflow_id, uuid4()) for _ in range(2)]
        second, third = [view["workflow_run_id"] for view in pending]
        assert set(service._workers) == {first} and supervisor._waiting == 0
        assert all(view["status"] == "PENDING" and all(step["platform_run_id"] is None for step in view["steps"]) for view in pending)
        assert (await service.create(owner, DEFINITION.workflow_id, request_id))["workflow_run_id"] == first
        assert (await service.resume(owner, second))["status"] == "PENDING"
        assert (await service.cancel(owner, third))["status"] == "CANCELLED"

        finish_first.set()
        await asyncio.wait_for(asyncio.shield(service._workers[first]), 8)
        assert (await service.get(owner, first))["status"] == "SUCCEEDED"
        assert (await service.get(owner, second))["status"] == "PENDING" and not service._workers
        await service.resume(owner, second)
        await asyncio.wait_for(second_entered.wait(), 4)
        assert set(service._workers) == {second}
        await service.close()
        assert not service._workers and supervisor._waiting == 0
        assert await supervisor.try_acquire_slot() is True
        supervisor.release_slot(True)
    finally:
        await service.close()
        await adapter.close()


@pytest.mark.asyncio
async def test_workflow_native_tool_recovery_uses_accepted_fence_and_invocation(clean_database, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import server
    from core.persistence.models import DurableToolInvocationRow
    from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
    from core.runtime.recovery_coordinator import RecoveryCoordinator
    from core.runtime.tool_idempotency import ProviderReconciliationResult, ToolInvocationState
    from tests.test_stage10_wp2_reconciliation_batch_b import _execution_root, _invocation

    class NoModel:
        def generate(self, *args, **kwargs):
            raise AssertionError("tool recovery must not invoke the model")

        def close(self):
            pass

    owner = _principal(await _create_user(clean_database, ("USER",)), "USER")
    instance = replace(_instance("workflow-opencode"), allowed_principals=frozenset({(owner.tenant_id, owner.user_id)}))
    private = Ed25519PrivateKey.generate()
    settings = replace(
        server.settings, llm_backend="local", database_url=clean_database.config.url, knowledge_base_required=False,
        embedding_model_path=str(tmp_path / "missing-embedding"), chroma_dir=str(tmp_path / "chroma"),
        snapshot_store_enabled=False, mcp_config_path=None, opencode_instances=(instance,), max_active_runs=3,
        jwt_issuer="test-issuer", jwt_audience="test-api",
        jwt_public_key=private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
    )

    async def upstream(request):
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_tool", "directory": instance.workspace})
        message = json.loads(request.content)["messageID"]
        return httpx.Response(200, json=_assistant("ses_tool", message, "external result"))

    async def idle_start(self):
        pass

    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "LocalLLMEngine", lambda **_: NoModel())
    monkeypatch.setattr(server, "VectorDBManager", lambda **_: (_ for _ in ()).throw(ValueError("synthetic KB unavailable")))
    monkeypatch.setattr(server, "OpenCodeRuntimeAdapter", lambda *args, **kwargs: OpenCodeRuntimeAdapter(
        *args, **kwargs, transport=httpx.MockTransport(upstream),
    ))
    monkeypatch.setattr(RecoveryCoordinator, "start", idle_start)
    for key in ("LOCAL_AGENT_STAGE13_DELIVERY_ENABLED", "LOCAL_AGENT_STAGE13_ENABLED"):
        monkeypatch.delenv(key, raising=False)

    async with server.lifespan(server.app):
        state = server.app.state
        workflow, recovery = state.workflow_service, state.recovery_coordinator
        control, durable = state.runtime_services.durable_run_control, state.runtime_services.durable_tool_invocation
        execute = state.agent_application_service.execute
        reservations = asyncio.Queue()

        async def hold_native(request, **kwargs):
            if request.runtime_instance_id is None:
                reservations.put_nowait((request, kwargs))
                await asyncio.Future()
            return await execute(request, **kwargs)

        monkeypatch.setattr(state.agent_application_service, "execute", hold_native)
        monkeypatch.setattr(state, "rate_limiter", _disabled_rate_limiter())

        async def reserve_native():
            await workflow.create(owner, DEFINITION.workflow_id, uuid4())
            return await asyncio.wait_for(reservations.get(), 6)

        async def seed_unknown(lease, access, query):
            root = _execution_root(lease.run_id)
            root = replace(root, resume_input={**root.resume_input, "user_query": query})
            await recovery.repository.initialize(root, lease=lease)
            await MemoryLifecycleRepository(clean_database).bind_run(lease, access, [], [])
            invocation = _invocation()
            await durable.prepare(lease=lease, step_id=root.plan.steps[0].step_id, invocation=invocation, tool_name=invocation.tool_name)
            await durable.start(lease=lease, invocation_id=invocation.invocation_id)
            await durable.unknown(lease=lease, invocation_id=invocation.invocation_id, reason="fixture response lost")
            await control.release(lease)
            return invocation

        async def accepted_unknown():
            request, kwargs = await reserve_native()
            access = kwargs["memory_access_context"]
            lease = await control.claim(request.run_id, "native-before-recovery", workflow_request=RuntimeRequest(
                request.agent_id, request.input, request.expected_agent_version,
                run_id=request.run_id, session_id=request.session_id, memory_access_context=access,
                workflow_dispatch_token=kwargs["workflow_dispatch_token"],
            ))
            return request.run_id, await seed_unknown(lease, access, request.input)

        auto_run, automatic = await accepted_unknown()
        calls = []

        def reconcile(record):
            calls.append((record.run_id, record.invocation_id))
            return ProviderReconciliationResult.NOT_COMMITTED

        registry = state.chat_service.router.tool_registry
        resolve = type(registry).resolve
        registration = SimpleNamespace(adapter=SimpleNamespace(reconcile_durable=reconcile))
        monkeypatch.setattr(type(registry), "resolve", lambda self, name: registration
                            if self is registry and name == automatic.tool_name else resolve(self, name))
        with pytest.raises(RunControlConflict):
            await control.claim(auto_run, "ordinary")
        assert await recovery.reconciliation_scan_once() == 1
        await asyncio.gather(*tuple(recovery._reconciliation_tasks))
        assert (await durable.get(automatic.invocation_id)).state is ToolInvocationState.NOT_COMMITTED

        manual_run, manual = await accepted_unknown()
        ordinary_run = str(uuid4())
        await state.authorization_service.bind_new(owner, "RUN", ordinary_run)
        lease = await control.claim(ordinary_run, "ordinary-native")
        ordinary = await seed_unknown(lease, MemoryAccessContext.from_principal(owner, "core_router"), "ordinary")
        unadmitted, _ = await reserve_native()
        await control.request_cancel(unadmitted.run_id, "REQUEST_CANCELLED")
        # 构造越过发现/路由预检查的边界记录，两个接缝仍须拒绝零 fence。
        unsupported = DurableToolInvocationRow(
            invocation_id=uuid4().hex, run_id=unadmitted.run_id, step_id="fixture-step", tool_name=automatic.tool_name,
            invocation_binding_digest="a" * 64, idempotency_key_digest="b" * 64,
            owner_id="fixture", fencing_token=0, state="UNKNOWN",
        )
        async with clean_database.transaction() as session:
            session.add(unsupported)
        with pytest.raises(RunControlConflict):
            await recovery._reconciliation_handler(unsupported)

        headers = {"Authorization": f"Bearer {_token(private, owner.user_id, ['USER'])}"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test", headers=headers) as client:
            payload = {"resolution": "NOT_COMMITTED", "reason": "provider confirms no commit"}
            response = await client.post(f"/api/v1/runs/{manual_run}/tool-invocations/{manual.invocation_id}/resolve", json=payload)
            assert response.status_code == 200 and response.json()["state"] == "NOT_COMMITTED", response.text
            response = await client.post(f"/api/v1/runs/{ordinary_run}/tool-invocations/{ordinary.invocation_id}/resolve", json=payload)
            assert response.status_code == 200 and response.json()["state"] == "NOT_COMMITTED", response.text
            denied = await client.post(f"/api/v1/runs/{unadmitted.run_id}/tool-invocations/{unsupported.invocation_id}/resolve", json=payload)
            assert denied.status_code == 409 and denied.json()["error"]["code"] == "TOOL_INVOCATION_FENCE_CONFLICT"
        async with clean_database.session() as session:
            assert (await session.get(RunControlRow, unadmitted.run_id)).fencing_token == 0
        assert calls == [(auto_run, automatic.invocation_id)]
