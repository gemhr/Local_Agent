"""真实 PostgreSQL 与生产 HTTP、发布解析和接纳路径的定向测试。"""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

import server
from core.agent_platform.registrations import BUSINESS_REGISTRATION_BUNDLE
from core.agent_platform.workflow import WorkflowError
from core.auth import AuthError, AuthorizationService
from core.persistence.models import ObjectOwnershipRow, WorkspaceAgentSubmissionRow, WorkflowVersionRow
from core.runtime.model_invocation import ModelAdapterResponse
from core.workspace.configuration import WorkspaceConfiguration
from core.workspace.contracts import WorkflowCopy, WorkflowEdit
from core.workspace.service import WorkspaceService
from core.workspace.tasks import WorkspaceTasks
from tests.test_opencode_runtime_adapter import _assistant
from tests.test_runtime_adapter import _native_application
from tests.test_stage6_wp2_ownership import _create_user, _principal
from tests.test_workflow_approval import REVIEW, _http
from tests.test_workflow_artifacts import _setup as setup_ci
from tests.test_workflow_execution import _setup as setup_text, _when


TEMPLATES = {item.workflow_id: item for item in BUSINESS_REGISTRATION_BUNDLE.workflows}


def mount(monkeypatch, database, authz, app, supervisor, gate, workflow):
    config = WorkspaceConfiguration(database, app, workflow, TEMPLATES)
    for name, value in {
        "workspace_service": WorkspaceService(database),
        "workspace_configuration": config,
        "workspace_tasks": WorkspaceTasks(database, app, workflow, authz, supervisor, gate),
        "agent_application_service": app,
        "run_execution_supervisor": supervisor,
    }.items():
        monkeypatch.setattr(server.app.state, name, value, raising=False)
    return config


@pytest.mark.asyncio
async def test_workspace_agent_configuration_drives_native_and_retry_reuses_run(clean_database, tmp_path, monkeypatch):
    app, chat, factory, model, _ = await _native_application(
        clean_database, tmp_path, [ModelAdapterResponse("done", finish_reason="stop", tool_call_count=0)],
    )
    authz = AuthorizationService(clean_database)
    supervisor = server.RunExecutionSupervisor()
    mount(monkeypatch, clean_database, authz, app, supervisor, factory.services.admission_gate, None)
    monkeypatch.setattr(server.app.state, "chat_service", chat, raising=False)
    owner = _principal(await _create_user(clean_database, ("USER",)), "USER")
    member = _principal(await _create_user(clean_database, ("USER",)), "USER")
    admin = _principal(await _create_user(clean_database, ("ADMIN",)), "ADMIN")
    outsider = _principal(await _create_user(clean_database, ("USER",), tenant_id="other-tenant"), "USER", tenant_id="other-tenant")
    spaces = WorkspaceService(clean_database)
    space = (await spaces.create_team(owner, "Agent Team")).space
    await spaces.put_member(owner, space.id, member.user_id, "MEMBER")
    client, headers = _http(clean_database, authz, None, monkeypatch)
    path = f"/api/v1/spaces/{space.id}"
    cfg = {"name": "saved", "base_agent_id": "core_router", "base_agent_version": "builtin-1",
           "instructions": "workspace-configuration-original"}
    try:
        async with client:
            catalog = await client.get(f"{path}/execution-catalog", headers=headers(member))
            assert catalog.status_code == 200 and catalog.json()["agents"][0]["id"] == "core_router"
            for actor in (admin, outsider):
                assert (await client.get(f"{path}/agents", headers=headers(actor))).status_code == 404
            created = await client.post(f"{path}/agents", headers=headers(owner), json=cfg)
            assert created.status_code == 201, created.text
            agent = created.json()
            agent_path = f"{path}/agents/{agent['id']}"
            assert (await client.get(agent_path, headers=headers(member))).json() == agent
            update = {**cfg, "instructions": "workspace-configuration-edited", "expected_revision": 1}
            assert (await client.put(agent_path, headers=headers(member), json=update)).status_code == 403
            assert (await client.put(agent_path, headers=headers(owner), json={**update, "base_agent_id": "ci_execution_status"})).status_code == 422
            task = {"kind": "AGENT", "request_id": str(uuid4()), "config_id": agent["id"], "expected_revision": 1,
                    "runtime_kind": "NATIVE", "input": "workspace-task-input"}
            accepted = await client.post(f"{path}/tasks", headers=headers(member), json=task)
            assert accepted.status_code == 202, accepted.text
            run_id = accepted.json()["run_id"]
            changed = await client.put(agent_path, headers=headers(owner), json=update)
            assert changed.status_code == 200 and changed.json()["revision"] == 2
            retry = await client.post(f"{path}/tasks", headers=headers(member), json=task)
            assert retry.status_code == 202 and retry.json()["run_id"] == run_id
            assert (await client.post(f"{path}/tasks", headers=headers(member), json={**task, "input": "changed"})).status_code == 409
            assert (await client.post("/api/v1/chat", headers=headers(member),
                                      json={"agent_id": "core_router", "query": "duplicate", "run_id": run_id})).status_code == 404
            assert (await client.get(f"/api/v1/runs/{run_id}/result", headers=headers(admin))).status_code == 404
            async with asyncio.timeout(5):
                while not model.requests:
                    await asyncio.sleep(.01)
            assert "workspace-configuration-original" in repr(model.requests)
            assert "workspace-task-input" in repr(model.requests)
            assert "workspace-configuration-edited" not in repr(model.requests)
            async with clean_database.session() as session:
                submission = await session.get(WorkspaceAgentSubmissionRow, run_id)
                ownership = await session.get(ObjectOwnershipRow, ("RUN", run_id))
                assert submission.configuration["agent_config"]["revision"] == 1
                assert ownership.owner_user_id == member.user_id and ownership.space_id == space.id
            await spaces.remove_member(owner, space.id, member.user_id)
            assert (await client.post(f"{path}/tasks", headers=headers(member), json=task)).status_code == 404
            assert (await client.get(f"/api/v1/runs/{run_id}/result", headers=headers(member))).status_code == 404
    finally:
        await supervisor.close()


@pytest.mark.asyncio
async def test_published_text_workflow_resolves_original_instructions_after_new_release(clean_database, tmp_path, monkeypatch):
    sent = []

    async def upstream(request):
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_workspace", "directory": instance.workspace})
        body = json.loads(request.content)
        sent.append(body["parts"][0]["text"])
        return httpx.Response(200, json=_assistant("ses_workspace", body["messageID"], "external done"))

    owner, app, services, _, adapter, instance, model, build, _, supervisor, authz = await setup_text(
        clean_database, tmp_path, upstream, capacity=1,
    )
    service, restarted = build(), build()
    mount(monkeypatch, clean_database, authz, app, supervisor, services.admission_gate, service)
    space = (await WorkspaceService(clean_database).create_team(owner, "Workflow Team")).space
    client, headers = _http(clean_database, authz, service, monkeypatch)
    path = f"/api/v1/spaces/{space.id}"
    slot = await supervisor.acquire_slot()
    try:
        async with client:
            copied = await client.post(f"{path}/workflows", headers=headers(owner),
                                       json={"template_id": "runtime_handoff", "template_version": "1", "name": "original"})
            assert copied.status_code == 201, copied.text
            workflow_id = copied.json()["id"]
            draft_path = f"{path}/workflows/{workflow_id}"
            task = {"kind": "WORKFLOW", "request_id": str(uuid4()), "workflow_id": workflow_id, "version": "1"}
            assert (await client.post(f"{path}/tasks", headers=headers(owner), json=task)).status_code == 404
            edit = {"name": "original", "expected_revision": 1, "runtime_instance_id": instance.instance_id,
                    "external_instruction": "pinned external instruction", "native_instruction": "pinned native instruction"}
            assert (await client.put(draft_path, headers=headers(owner), json={**edit, "steps": ["NATIVE", "OPENCODE"]})).status_code == 422
            assert (await client.put(draft_path, headers=headers(owner), json=edit)).status_code == 200
            assert (await client.get(draft_path, headers=headers(owner))).json()["draft"]["native_instruction"] == edit["native_instruction"]
            published = await client.post(f"{draft_path}/publish", headers=headers(owner), json={"expected_revision": 2})
            assert published.status_code == 200, published.text
            assert published.json()["versions"][0]["version"] == "1"
            direct = await client.post(f"/api/v1/workflows/{workflow_id}/runs", headers=headers(owner),
                                       json={"request_id": str(uuid4()), "expected_workflow_version": "1"})
            assert direct.status_code == 404
            launch = await client.post(f"{path}/tasks", headers=headers(owner), json=task)
            assert launch.status_code == 202, launch.text
            run_id = launch.json()["workflow_run_id"]
            assert launch.json()["status"] == "PENDING"
            assert (await client.post(f"{path}/tasks", headers=headers(owner), json=task)).json()["workflow_run_id"] == run_id
            assert (await client.put(draft_path, headers=headers(owner), json={
                **edit, "name": "second", "external_instruction": "v2 external",
                "native_instruction": "v2 native", "expected_revision": 2,
            })).status_code == 200
            v2 = await client.post(f"{draft_path}/publish", headers=headers(owner), json={"expected_revision": 3})
            assert [version["version"] for version in v2.json()["versions"]] == ["2", "1"]
        await service.close()
        supervisor.release_slot(slot)
        slot = None
        original_target = adapter.workflow_target_digest
        monkeypatch.setattr(adapter, "workflow_target_digest", "0" * 64)
        await restarted.resume(owner, run_id)
        paused = await _when(restarted, owner, run_id, lambda view: view["status"] == "PAUSED")
        assert paused["reason"] == "WORKFLOW_DEFINITION_DRIFT" and not sent
        if worker := restarted._workers.get(run_id):
            await worker
        monkeypatch.setattr(adapter, "workflow_target_digest", original_target)
        resumed = await restarted.resume(owner, run_id)
        if worker := restarted._workers.get(run_id):
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=8)
            except TimeoutError:
                pytest.fail(f"Workflow {run_id} 恢复 Worker 未按时结束，Resume 响应：{resumed}")
        final = await restarted.get(owner, run_id)
        assert final["status"] == "SUCCEEDED", final
        assert sent == ["pinned external instruction"] and "pinned native instruction" in repr(model.requests)
        async with clean_database.session() as session:
            old = await session.get(WorkflowVersionRow, (workflow_id, "1"))
            assert old.definition_snapshot["definition"]["tasks"][0]["instruction"] == sent[0]
            assert old.definition_digest == published.json()["versions"][0]["definition_digest"]
            for step in final["steps"]:
                owner_row = await session.get(ObjectOwnershipRow, ("RUN", step["platform_run_id"]))
                assert owner_row.space_id == space.id and owner_row.owner_user_id == owner.user_id
    finally:
        if slot is not None:
            supervisor.release_slot(slot)
        await service.close()
        await restarted.close()
        await adapter.close()


@pytest.mark.asyncio
async def test_ci_artifact_instance_purpose_and_published_approval_resume(clean_database, tmp_path, monkeypatch):
    q, _, calls, adapter, build = await setup_ci(clean_database, tmp_path, definition=REVIEW)
    service, restarted = build(), build()
    app = service._application
    monkeypatch.setattr(server.app.state, "chat_service", app._chat_service, raising=False)
    config = mount(monkeypatch, clean_database, q.authorization, app, service._supervisor, service._admission_gate, service)
    owner = q.principal
    spaces = WorkspaceService(clean_database)
    space = (await spaces.create_team(owner, "CI Team")).space
    reviewer = _principal(
        await _create_user(clean_database, ("USER",), tenant_id=owner.tenant_id), "USER", tenant_id=owner.tenant_id,
    )
    await spaces.put_member(owner, space.id, reviewer.user_id, "MEMBER")
    client, headers = _http(clean_database, q.authorization, service, monkeypatch)
    path = f"/api/v1/spaces/{space.id}"
    try:
        async with client:
            runtime = await client.get(f"{path}/runtime-instances", headers=headers(owner))
            assert runtime.status_code == 200
            native, external = runtime.json()
            assert native["kind"] == "NATIVE"
            assert external["purposes"] == ["WORKFLOW_CI_ARTIFACT"] and external["deployment"] == "LOCAL"
            assert "workspace" not in external and "base_url" not in external
            blocked = await client.post(f"{path}/tasks", headers=headers(owner), json={
                "kind": "AGENT", "request_id": str(uuid4()), "agent_id": "core_router", "agent_version": "builtin-1",
                "runtime_kind": "OPENCODE", "runtime_instance_id": external["id"], "input": "ordinary",
            })
            assert blocked.status_code == 422 and blocked.json()["error"]["code"] == "RUNTIME_INSTANCE_PURPOSE_DENIED"
            for route in ("/api/v1/chat", "/api/runtime/execute"):
                response = await client.post(route, headers=headers(owner), json={
                    "agent_id": "core_router", "query": "ordinary", "run_id": str(uuid4()),
                    "runtime_instance_id": external["id"], "timeout_seconds": 30,
                })
                envelope = response.json()
                assert response.status_code == 422 and (
                    envelope.get("error", {}).get("code") or envelope.get("detail")
                ) == "RUNTIME_INSTANCE_PURPOSE_DENIED", response.text
            assert calls == []
            draft = await config.copy_workflow(owner, space.id, WorkflowCopy(
                template_id=REVIEW.workflow_id, template_version=REVIEW.workflow_version, name="Review",
            ))
            workflow_id = draft["id"]
            bad = await client.put(f"{path}/workflows/{workflow_id}", headers=headers(owner), json={
                "name": "Review", "expected_revision": 1, "runtime_instance_id": external["id"], "native_instruction": "override",
            })
            assert bad.status_code == 422
            await config.save_workflow(owner, space.id, workflow_id, WorkflowEdit(
                name="Review", expected_revision=1, runtime_instance_id=external["id"],
            ))
            await config.publish(owner, space.id, workflow_id, 2)
            sent = await client.post(f"{path}/tasks", headers=headers(owner), json={
                "kind": "WORKFLOW", "request_id": str(uuid4()), "workflow_id": workflow_id, "version": "1",
                "incident_id": q.incident.incident_id, "execution_id": q.execution.execution_id,
            })
            assert sent.status_code == 202, sent.text
            run_id = sent.json()["workflow_run_id"]
            waiting = await _when(service, owner, run_id, lambda view: view["status"] in {"WAITING_APPROVAL", "FAILED", "PAUSED"})
            assert waiting["status"] == "WAITING_APPROVAL", waiting
            await service.close()
            await config.save_workflow(owner, space.id, workflow_id, WorkflowEdit(
                name="New release", expected_revision=2, runtime_instance_id=external["id"],
            ))
            await config.publish(owner, space.id, workflow_id, 3)
            approval = waiting["approval"]
            with pytest.raises(AuthError) as denied:
                await restarted.decide_approval(reviewer, run_id, approval["approval_id"], "APPROVE")
            assert denied.value.status_code == 403
            await spaces.put_member(owner, space.id, reviewer.user_id, "ADMIN")
            await restarted.decide_approval(reviewer, run_id, approval["approval_id"], "APPROVE", binding_digest=approval["binding_digest"])
            with pytest.raises(AuthError) as denied:
                await restarted.resume(reviewer, run_id)
            assert denied.value.status_code == 404
            await restarted.resume(owner, run_id)
            final = await _when(restarted, owner, run_id, lambda view: view["status"] in {"SUCCEEDED", "FAILED", "PAUSED"})
            assert final["status"] == "SUCCEEDED" and final["workflow_version"] == "1", final
            assert final["approval"]["consumed_at"] is not None and not q.model.requests
            assert calls.count(("POST", "/session/ses_handoff/message")) == 1
            with pytest.raises(WorkflowError, match="WORKFLOW_DEFINITION_UNAVAILABLE"):
                await restarted.create(owner, workflow_id, uuid4())
    finally:
        await service.close()
        await restarted.close()
        await adapter.close()
