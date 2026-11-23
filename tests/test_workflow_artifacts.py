import asyncio
import hashlib
import json
import os
import re
from dataclasses import replace
from functools import partial
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import func, select

from core.agent_platform.application import AgentApplicationService
from core.agent_platform.ci_status import CIStatusService
from core.agent_platform.opencode_runtime_adapter import OpenCodeRuntimeAdapter
from core.agent_platform.registrations import BUSINESS_REGISTRATION_BUNDLE
from core.agent_platform.workflow import WorkflowService
from core.agent_platform.workflow_artifacts import artifact_path, verify_native_admission
from core.auth import AuthService
from core.persistence.models import DurableToolInvocationRow, RunControlRow, UserRoleRow
from core.persistence.repositories import runtime as runtime_repository
from core.persistence.repositories.external_runs import ExternalRunRepository
from core.persistence.repositories.workflows import WorkflowRepository
from core.runtime.run_control import DurableRunControlService
from tests.test_ci_status_routing import setup_query
from tests.test_opencode_runtime_adapter import _assistant, _instance
from tests.test_stage6_wp2_ownership import _ROLE_IDS, _disabled_rate_limiter, _ensure_roles, _token
from tests.test_workflow_execution import _when


DEFINITION = next(item for item in BUSINESS_REGISTRATION_BUNDLE.workflows if item.workflow_id == "ci_artifact_handoff")


async def _setup(database, tmp_path, *, mode="valid", definition=DEFINITION):
    from server import RunExecutionSupervisor

    runtime_instances = {}
    control = DurableRunControlService(
        database, lease_seconds=10,
        workflow_artifact_check=partial(verify_native_admission, runtime_instances=runtime_instances),
    )
    q = await setup_query(database, tmp_path, control=control)
    await _ensure_roles(database)
    async with database.transaction() as session:
        session.add(UserRoleRow(user_id=q.principal.user_id, role_id=_ROLE_IDS["USER"]))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    instance = replace(
        _instance("workflow-opencode"), workspace=str(workspace), use_instance_tool_permissions=True,
        allowed_principals=frozenset({(q.principal.tenant_id, q.principal.user_id)}),
    )
    calls = []
    stored = None

    async def upstream(request):
        nonlocal stored
        path = request.url.path
        calls.append((request.method, path))
        assert request.url.params["directory"] == str(workspace)
        if path == "/session":
            return httpx.Response(200, json={"id": "ses_handoff", "directory": str(workspace)})
        if request.method == "GET":
            assert stored is not None and path == "/session/ses_handoff/message/msg_assistanthandoff"
            return httpx.Response(200, json=stored)
        assert path == "/session/ses_handoff/message"
        payload = json.loads(request.content)
        prompt = payload["parts"][0]["text"]
        relative = re.search(r"workflow-runs/[0-9a-f-]{36}/ci-execution\.json", prompt)[0]
        output = json.loads(re.search(r"回复 JSON 对象 (\{.*?\})。", prompt)[1])
        artifact = json.loads(re.search(r"内容为 (\{.*?\})。", prompt)[1])
        assert output["artifact_path"] == relative and output["incident_id"] == q.incident.incident_id
        assert artifact["execution_id"] == q.execution.execution_id
        file = workspace / relative
        assert file.parent.is_dir() and not file.exists()
        if mode == "reparse":
            outside = tmp_path / "outside"
            outside.mkdir()
            file.parent.rmdir()
            if os.name == "nt":
                from _winapi import CreateJunction
                CreateJunction(str(outside), str(file.parent))
            else:
                file.parent.symlink_to(outside, target_is_directory=True)
        if mode == "wrong_execution":
            artifact["execution_id"] = str(uuid4())
        file.write_text(json.dumps(artifact), encoding="utf-8")
        if mode == "escape":
            output["artifact_path"] = "../ci-execution.json"
        text = json.dumps(output)
        if mode == "duplicate":
            text = text[:-1] + f',"incident_id":"{q.incident.incident_id}"' + "}"
        stored = _assistant("ses_handoff", payload["messageID"], text)
        # 故意让初始投影不含 CI JSON，证明 Workflow 取正式绑定消息。
        return httpx.Response(200, json=_assistant("ses_handoff", payload["messageID"], "initial text"))

    services = q.factory.services
    runs = ExternalRunRepository(database, services.client_event_feed)
    adapter = OpenCodeRuntimeAdapter(instance, runs=runs, run_registry=services.run_registry, transport=httpx.MockTransport(upstream))
    runtime_instances[instance.instance_id] = adapter
    app = AgentApplicationService(q.app._chat_service, q.factory._agent_registry,
                                  runtime_instances=runtime_instances, external_runs=runs)
    status = CIStatusService(q.incidents, app, q.authorization)
    supervisor = RunExecutionSupervisor(max_active_runs=2)

    def build():
        return WorkflowService(WorkflowRepository(database), {definition.workflow_id: definition}, app,
                               q.authorization, supervisor, services.run_registry, services.admission_gate, ci_status=status)

    return q, workspace, calls, adapter, build


async def _create(service, q, request_id=None):
    return await service.create(q.principal, DEFINITION.workflow_id, request_id or uuid4(),
                                incident_id=q.incident.incident_id, execution_id=q.execution.execution_id)


@pytest.mark.asyncio
async def test_ci_handoff_restarts_with_verified_input_and_real_fast_path(clean_database, tmp_path, monkeypatch):
    import server

    q, workspace, calls, adapter, build = await _setup(clean_database, tmp_path)
    service, restarted = build(), build()
    waiting = asyncio.Event()
    prepare = service._repository.prepare

    async def hold_native(run_id, position, *args, **kwargs):
        if position == 1:
            waiting.set()
            await asyncio.Future()
        return await prepare(run_id, position, *args, **kwargs)

    monkeypatch.setattr(service._repository, "prepare", hold_native)
    private = Ed25519PrivateKey.generate()
    settings = replace(server.settings, jwt_issuer="test-issuer", jwt_audience="test-api",
                       jwt_public_key=private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode())
    for name, value in {
        "auth_service": AuthService(clean_database, settings), "authorization_service": q.authorization,
        "rate_limiter": _disabled_rate_limiter(), "workflow_service": service,
    }.items():
        monkeypatch.setattr(server.app.state, name, value, raising=False)
    try:
        request_id = uuid4()
        headers = {"Authorization": f"Bearer {_token(private, q.principal.user_id, ['USER'])}"}
        create_body = {"request_id": str(request_id), "incident_id": q.incident.incident_id, "execution_id": q.execution.execution_id}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test", headers=headers) as client:
            url = f"/api/v1/workflows/{DEFINITION.workflow_id}/runs"
            denied = await client.post(url, json={**create_body, "execution_id": str(uuid4())})
            assert denied.status_code == 404 and not calls
            accepted = await client.post(url, json=create_body)
            assert accepted.status_code == 202, accepted.text
            run_id = accepted.json()["workflow_run_id"]
        await asyncio.wait_for(waiting.wait(), 8)
        await service.close()
        _, (producer, native) = await restarted._repository.read(run_id)
        assert producer.status == "SUCCEEDED" and native.platform_run_id is None
        raw = (workspace / artifact_path(run_id)).read_bytes()
        assert producer.artifact_ref["producer_run_id"] == producer.platform_run_id
        assert producer.artifact_ref["size"] == len(raw) and producer.artifact_ref["sha256"] == hashlib.sha256(raw).hexdigest()
        assert json.loads(native.bound_input) == q.query
        duplicate = await _create(restarted, q, request_id)
        assert duplicate["workflow_run_id"] == run_id
        assert duplicate["steps"][0]["artifact_ref"]["sha256"] == producer.artifact_ref["sha256"]
        await asyncio.gather(restarted.resume(q.principal, run_id), restarted.resume(q.principal, run_id))
        final = await _when(restarted, q.principal, run_id, lambda view: view["status"] in {"SUCCEEDED", "FAILED", "PAUSED"})
        assert final["status"] == "SUCCEEDED", final
        external, native = final["steps"]
        assert external["platform_run_id"] == producer.platform_run_id != native["platform_run_id"]
        native_id = native["platform_run_id"]
        image = await q.repo.load(native_id)
        assert image.root.resume_input["adaptive_route"]["query"] == q.query
        assert json.loads(image.root.resume_input["user_query"]) == q.query
        assert image.root.resume_input["adaptive_route"]["principal"]["user_id"] == str(q.principal.user_id)
        assert image.root.execution_policy == "fixed-plan.v1" and len(image.tool_invocations) == 1
        assert image.tool_invocations[0].tool_name == "ci_query_execution_status"
        assert image.tool_invocations[0].state == "COMMITTED" and not q.model.requests and not image.models
        feed = await q.factory.services.client_event_feed.read_after(native_id, 0, 100)
        output = json.loads(feed[-1].payload["output"])
        assert output["schema_version"] == "ci-status.v1" and output["summary"] == q.execution.summary
        assert calls == [("POST", "/session"), ("POST", "/session/ses_handoff/message"),
                         ("GET", "/session/ses_handoff/message/msg_assistanthandoff")]
    finally:
        await service.close()
        await restarted.close()
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "reason"), [
    ("duplicate", "WORKFLOW_OUTPUT_INVALID"),
    ("wrong_execution", "WORKFLOW_CI_BINDING_MISMATCH"),
    ("escape", "WORKFLOW_ARTIFACT_PATH_INVALID"),
    ("reparse", "WORKFLOW_ARTIFACT_PATH_INVALID"),
])
async def test_invalid_output_or_artifact_blocks_native_reservation(clean_database, tmp_path, mode, reason):
    q, _, _, adapter, build = await _setup(clean_database, tmp_path, mode=mode)
    service = build()
    try:
        run_id = (await _create(service, q))["workflow_run_id"]
        failed = await _when(service, q.principal, run_id, lambda view: view["status"] in {"FAILED", "PAUSED", "SUCCEEDED"})
        producer, native = failed["steps"]
        assert failed["status"] == "FAILED" and failed["reason"] == reason, failed
        assert producer["runtime_status"] == "SUCCEEDED" and producer["status"] == "FAILED"
        assert "artifact_ref" not in producer and native["platform_run_id"] is None and not q.model.requests
    finally:
        await service.close()
        await adapter.close()


@pytest.mark.asyncio
async def test_replaced_artifact_between_prepare_and_dispatch_never_claims_native(clean_database, tmp_path, monkeypatch):
    q, workspace, calls, adapter, build = await _setup(clean_database, tmp_path)
    service, restarted = build(), build()
    prepare = service._repository.prepare

    async def replace_after_reservation(run_id, position, *args, **kwargs):
        step = await prepare(run_id, position, *args, **kwargs)
        if position == 1 and step is not None:
            file = workspace / artifact_path(run_id)
            alternate = file.with_suffix(".tmp")
            alternate.write_bytes(file.read_bytes() + b" ")
            alternate.replace(file)
        return step

    monkeypatch.setattr(service._repository, "prepare", replace_after_reservation)
    try:
        run_id = (await _create(service, q))["workflow_run_id"]
        paused = await _when(service, q.principal, run_id, lambda view: view["status"] in {"PAUSED", "FAILED", "SUCCEEDED"})
        assert paused["status"] == "PAUSED" and paused["reason"] == "WORKFLOW_ARTIFACT_CHANGED", paused
        assert paused["steps"][0]["status"] == "SUCCEEDED"
        native_id = paused["steps"][1]["platform_run_id"]
        assert native_id is not None
        await service.close()
        await restarted.resume(q.principal, run_id)
        await asyncio.wait_for(asyncio.shield(restarted._workers[run_id]), 8)
        assert (await restarted.get(q.principal, run_id))["steps"][1]["platform_run_id"] == native_id
        async with clean_database.session() as session:
            assert await session.get(RunControlRow, native_id) is None
        assert await q.repo.load(native_id) is None and not q.model.requests
        assert calls.count(("POST", "/session/ses_handoff/message")) == 1
    finally:
        await service.close()
        await restarted.close()
        await adapter.close()


@pytest.mark.asyncio
async def test_changed_artifact_while_native_claim_waits_for_run_lock(clean_database, tmp_path, monkeypatch):
    q, workspace, calls, adapter, build = await _setup(clean_database, tmp_path)
    service, restarted = build(), build()
    native_reserved = asyncio.Queue()
    continue_native = asyncio.Event()
    claim_pid = asyncio.get_running_loop().create_future()
    prepare = service._repository.prepare
    lock_run = runtime_repository.lock_run_scope
    wanted_run = None

    async def hold_after_reservation(run_id, position, *args, **kwargs):
        step = await prepare(run_id, position, *args, **kwargs)
        if position == 1 and step is not None:
            native_reserved.put_nowait(step.platform_run_id)
            await continue_native.wait()
        return step

    async def observe_lock(session, run_id):
        if run_id == wanted_run and not claim_pid.done():
            claim_pid.set_result(await session.scalar(select(func.pg_backend_pid())))
        await lock_run(session, run_id)

    monkeypatch.setattr(service._repository, "prepare", hold_after_reservation)
    monkeypatch.setattr(runtime_repository, "lock_run_scope", observe_lock)
    try:
        run_id = (await _create(service, q))["workflow_run_id"]
        wanted_run = await asyncio.wait_for(native_reserved.get(), 8)
        async with clean_database.transaction() as blocker:
            await lock_run(blocker, wanted_run)
            blocking_pid = await blocker.scalar(select(func.pg_backend_pid()))
            continue_native.set()
            waiting_pid = await asyncio.wait_for(claim_pid, 5)
            async with asyncio.timeout(5), clean_database.session() as probe:
                while blocking_pid not in await probe.scalar(select(func.pg_blocking_pids(waiting_pid))):
                    pass
                assert await probe.get(RunControlRow, wanted_run) is None
            _, (producer, native) = await service._repository.read(run_id)
            assert native.platform_run_id == wanted_run and producer.status == "SUCCEEDED"
            path = workspace / artifact_path(run_id)
            original = path.read_bytes()
            assert hashlib.sha256(original).hexdigest() == producer.artifact_ref["sha256"]
            replacement = path.with_suffix(".tmp")
            replacement.write_bytes(original + b" ")
            replacement.replace(path)
            assert hashlib.sha256(path.read_bytes()).hexdigest() != producer.artifact_ref["sha256"]

        paused = await _when(service, q.principal, run_id, lambda view: view["status"] in {"PAUSED", "FAILED", "SUCCEEDED"})
        assert paused["status"] == "PAUSED" and paused["reason"] == "WORKFLOW_ARTIFACT_CHANGED", paused
        await service.close()
        await restarted.resume(q.principal, run_id)
        if task := restarted._workers.get(run_id):
            await asyncio.wait_for(asyncio.shield(task), 8)
        final = await restarted.get(q.principal, run_id)
        assert final["status"] == "PAUSED" and final["reason"] == "WORKFLOW_ARTIFACT_CHANGED"
        assert final["steps"][1]["platform_run_id"] == wanted_run and final["steps"][1]["attempt"] == 1
        async with clean_database.session() as session:
            assert await session.get(RunControlRow, wanted_run) is None
            assert not await session.scalar(select(DurableToolInvocationRow.invocation_id).where(
                DurableToolInvocationRow.run_id == wanted_run,
            ).limit(1))
        assert await q.repo.load(wanted_run) is None and not q.model.requests
        assert calls.count(("POST", "/session/ses_handoff/message")) == 1
    finally:
        continue_native.set()
        await service.close()
        await restarted.close()
        await adapter.close()


@pytest.mark.asyncio
async def test_preexisting_workflow_output_directory_is_rejected_before_outbound(clean_database, tmp_path, monkeypatch):
    q, workspace, calls, adapter, build = await _setup(clean_database, tmp_path)
    pending, resumed = build(), build()

    async def leave_pending(*_):
        pass

    monkeypatch.setattr(pending, "_start", leave_pending)
    try:
        run_id = (await _create(pending, q))["workflow_run_id"]
        path = workspace / artifact_path(run_id)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": "ci-execution-ref.v1", "execution_id": q.execution.execution_id}), encoding="utf-8")
        await resumed.resume(q.principal, run_id)
        paused = await _when(resumed, q.principal, run_id, lambda view: view["status"] == "PAUSED")
        assert paused["reason"] == "WORKFLOW_ARTIFACT_PREEXISTS"
        assert all(step["platform_run_id"] is None for step in paused["steps"]) and not calls
    finally:
        await pending.close()
        await resumed.close()
        await adapter.close()
