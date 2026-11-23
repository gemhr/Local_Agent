"""使用原工作流与持久记录验证工作台观察和权限边界。"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

import server
from core.auth import AuthorizationService
from core.persistence.models import UserRow, WorkspaceAgentSubmissionRow
from core.persistence.repositories.runtime import lock_run_scope
from core.runtime.client_event_feed import PostgresClientEventFeed
from core.runtime.model_invocation import ModelAdapterResponse
from core.workspace.contracts import WorkflowCopy, WorkflowEdit
from core.workspace.service import WorkspaceService
from tests.test_runtime_adapter import _native_application
from tests.test_stage6_wp2_ownership import _create_user, _principal
from tests.test_workflow_approval import REVIEW, _http
from tests.test_workflow_artifacts import _setup
from tests.test_workflow_execution import _when
from tests.test_workspace_execution import mount


@pytest.mark.asyncio
async def test_legacy_run_sse_reauth_stops_delivery_after_user_disable(clean_database, monkeypatch):
    authz = AuthorizationService(clean_database)
    owner = _principal(await _create_user(clean_database, ("USER",)), "USER")
    run_id = str(uuid4())
    await authz.bind_new(owner, "RUN", run_id)
    feed = PostgresClientEventFeed(clean_database)
    monkeypatch.setattr(server.app.state, "client_event_feed", feed, raising=False)
    client, headers = _http(clean_database, authz, None, monkeypatch)
    url = f"/api/v1/runs/{run_id}/events"
    frames: asyncio.Queue = asyncio.Queue()

    async def record_stream(scope, receive, send):
        async def record(message):
            await send(message)
            frames.put_nowait(message)
        await server.app(scope, receive, record)

    async def append(text):
        async with clean_database.transaction() as session:
            await lock_run_scope(session, run_id)
            await feed.append_external_in_transaction(session, run_id, "output.delta", {"text": text})

    async def next_frame():
        try:
            return await asyncio.wait_for(frames.get(), 6)
        except TimeoutError:
            pytest.fail(f"SSE 测试 Run {run_id} 未收到下一个帧")

    await append("before-cursor")
    await append("initial")
    async with client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=record_stream), base_url="http://test",
    ) as observer:
        stream = asyncio.create_task(observer.get(url, headers={**headers(owner), "Last-Event-ID": "1"}))
        try:
            started = await next_frame()
            assert started.get("status") == 200, started
            assert b'id: 2\nevent: output.delta\ndata: {"text":"initial"}' in (await next_frame())["body"]
            assert (await next_frame())["body"] == b": heartbeat\n\n"
            await append("after-valid-recheck")
            assert b'id: 3\nevent: output.delta\ndata: {"text":"after-valid-recheck"}' in (await next_frame())["body"]

            async with clean_database.transaction() as session:
                user = await session.get(UserRow, owner.user_id)
                user.disabled_at = datetime.now(UTC)
            denied = await client.get(url, headers=headers(owner))
            assert denied.status_code == 401 and denied.json()["error"]["code"] == "AUTH_PRINCIPAL_DISABLED", denied.text
            revoked = await next_frame()
            assert revoked.get("body") == b"event: access.revoked\ndata: {}\n\n", revoked
            await append("after-revocation")
            completed = await asyncio.wait_for(stream, 6)
            assert completed.text.count("event: output.delta") == 2, completed.text
            assert "before-cursor" not in completed.text and "after-revocation" not in completed.text, completed.text
        finally:
            if not stream.done():
                stream.cancel()
                await asyncio.gather(stream, return_exceptions=True)


@pytest.mark.asyncio
async def test_workspace_agent_board_admission_result_and_team_scope(clean_database, tmp_path, monkeypatch):
    application, _, factory, _, _ = await _native_application(
        clean_database, tmp_path, [ModelAdapterResponse("persisted response", finish_reason="stop", tool_call_count=0)],
    )
    services = factory.services
    authz = AuthorizationService(clean_database)
    supervisor = server.RunExecutionSupervisor()
    mount(monkeypatch, clean_database, authz, application, supervisor, services.admission_gate, None)
    monkeypatch.setattr(server.app.state, "client_event_feed", services.client_event_feed, raising=False)
    owner = _principal(await _create_user(clean_database, ("USER",)), "USER")
    reader = _principal(await _create_user(clean_database, ("USER",)), "USER")
    other = _principal(await _create_user(clean_database, ("ADMIN",), tenant_id="other"), "ADMIN", tenant_id="other")
    spaces = WorkspaceService(clean_database)
    space = (await spaces.create_team(owner, "Shared")).space
    await spaces.put_member(owner, space.id, reader.user_id, "MEMBER")
    async with clean_database.transaction() as session:
        session.add(WorkspaceAgentSubmissionRow(
            run_id=str(uuid4()), request_id=uuid4(), space_id=space.id, tenant_id=space.tenant_id,
            owner_user_id=owner.user_id, request_digest="9" * 64, configuration={"runtime_kind": "NATIVE"},
        ))
    client, headers = _http(clean_database, authz, None, monkeypatch)
    board = "/api/v1/workspace/tasks"
    try:
        async with client:
            launch = await client.post(f"/api/v1/spaces/{space.id}/tasks", headers=headers(owner), json={
                "kind": "AGENT", "request_id": str(uuid4()), "agent_id": "core_router", "agent_version": "builtin-1",
                "input": "input", "runtime_kind": "NATIVE",
            })
            assert launch.status_code == 202, launch.text
            run_id = launch.json()["run_id"]
            if worker := supervisor._tasks.get(run_id):
                await asyncio.wait_for(asyncio.shield(worker), 10)
            result = await client.get(f"{board}/AGENT/{run_id}", headers=headers(reader))
            assert result.status_code == 200, result.text
            detail = result.json()
            assert detail["status"] == "SUCCEEDED" and detail["owner_user_id"] == str(owner.user_id)
            assert detail["result"] == {"availability": "CONFIRMED", "output": "persisted response"}
            assert not detail["can_cancel"]
            page = (await client.get(f"{board}?kind=AGENT&status=SUCCEEDED&limit=1", headers=headers(reader))).json()
            assert [item["task_id"] for item in page["items"]] == [run_id] and page["next_offset"] is None
            assert (await client.get(f"{board}?mine=true", headers=headers(reader))).json()["items"] == []
            assert (await client.get(board, headers=headers(other))).json()["items"] == []
            assert (await client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers(reader))).status_code == 404
            history = f"/api/v1/runs/{run_id}/events/history"
            first = (await client.get(f"{history}?limit=1", headers=headers(reader))).json()
            assert first["events"][0]["event_type"] == "run.started" and first["has_more"]
            response = await client.get(f"/api/v1/runs/{run_id}/events",
                                        headers={**headers(reader), "Last-Event-ID": str(first["next_cursor"])})
            assert response.status_code == 200 and "event: run.completed" in response.text and "event: run.started" not in response.text
            await spaces.remove_member(owner, space.id, reader.user_id)
            assert (await client.get(history, headers=headers(reader))).status_code == 404
            assert (await client.get(f"{board}/AGENT/{run_id}", headers=headers(reader))).status_code == 404
    finally:
        await supervisor.close()


@pytest.mark.asyncio
async def test_workspace_workflow_board_approval_artifact_and_revocation(clean_database, tmp_path, monkeypatch):
    q, _, calls, adapter, build = await _setup(clean_database, tmp_path, definition=REVIEW)
    workflow = build()
    app = workflow._application
    owner = q.principal
    spaces = WorkspaceService(clean_database)
    config = mount(monkeypatch, clean_database, q.authorization, app, workflow._supervisor, workflow._admission_gate, workflow)
    feed = q.factory.services.client_event_feed
    monkeypatch.setattr(server.app.state, "client_event_feed", feed, raising=False)
    team = (await spaces.create_team(owner, "CI Shared")).space
    reviewer = _principal(await _create_user(clean_database, ("USER",), tenant_id=owner.tenant_id), "USER", tenant_id=owner.tenant_id)
    reader = _principal(await _create_user(clean_database, ("USER",), tenant_id=owner.tenant_id), "USER", tenant_id=owner.tenant_id)
    await spaces.put_member(owner, team.id, reviewer.user_id, "ADMIN")
    await spaces.put_member(owner, team.id, reader.user_id, "MEMBER")
    draft = await config.copy_workflow(owner, team.id, WorkflowCopy(template_id=REVIEW.workflow_id, template_version="1", name="Review"))
    await config.save_workflow(owner, team.id, draft["id"], WorkflowEdit(
        name="Review", expected_revision=1, runtime_instance_id="workflow-opencode",
    ))
    await config.publish(owner, team.id, draft["id"], 2)
    client, headers = _http(clean_database, q.authorization, workflow, monkeypatch)
    try:
        async with client:
            created = await client.post(f"/api/v1/spaces/{team.id}/tasks", headers=headers(owner), json={
                "kind": "WORKFLOW", "request_id": str(uuid4()), "workflow_id": draft["id"], "version": "1",
                "incident_id": q.incident.incident_id, "execution_id": q.execution.execution_id,
            })
            assert created.status_code == 202, created.text
            run_id = created.json()["workflow_run_id"]
            await _when(workflow, owner, run_id, lambda detail: detail["status"] == "WAITING_APPROVAL")
            base = "/api/v1/workspace"
            source = f"/api/v1/workflow-runs/{run_id}"
            detail = (await client.get(f"{base}/tasks/WORKFLOW/{run_id}", headers=headers(reader))).json()
            assert detail["status"] == "WAITING_APPROVAL" and not detail["can_approve"]
            assert (await client.get(f"{base}/tasks", headers=headers(reader))).json()["items"][0]["task_id"] == run_id
            assert (await client.get(f"{base}/tasks?kind=AGENT", headers=headers(reader))).json()["items"] == []
            approval = detail["workflow"]["approval"]
            producer = detail["workflow"]["steps"][0]
            metadata = producer["artifact_ref"]
            child = producer["platform_run_id"]
            assert metadata["producer_run_id"] == child and metadata["workflow_run_id"] == run_id and metadata["sha256"]
            assert "file_identity" not in metadata and "workspace_target_digest" not in metadata
            assert (await client.get(source, headers=headers(reader))).status_code == 200
            assert (await client.get(f"/api/v1/runs/{child}/result", headers=headers(reader))).status_code == 200
            queue = f"{base}/approvals?status=PENDING"
            assert (await client.get(queue, headers=headers(reader))).json()["items"] == []
            assert (await client.get(queue, headers=headers(reviewer))).json()["items"][0]["approval_id"] == approval["approval_id"]
            decision = f"{source}/approvals/{approval['approval_id']}/decision"
            vote = {"decision": "APPROVE", "binding_digest": approval["binding_digest"]}
            assert (await client.post(decision, headers=headers(reader), json=vote)).status_code == 403
            assert (await client.post(decision, headers=headers(reviewer), json=vote)).status_code == 200
            pending = (await client.get(f"{base}/tasks/WORKFLOW/{run_id}", headers=headers(owner))).json()
            assert pending["status"] == "AWAITING_OWNER" and pending["can_resume"]
            assert pending["workflow"]["steps"][1]["platform_run_id"] is None
            assert (await client.post(f"{source}/resume", headers=headers(reviewer))).status_code == 404
            history = (await client.get(f"/api/v1/runs/{child}/events/history", headers=headers(reader))).json()
            polled = asyncio.Event()
            read = feed.read_after

            async def observe(*args, **kwargs):
                polled.set()
                return await read(*args, **kwargs)

            monkeypatch.setattr(feed, "read_after", observe)
            streaming = asyncio.create_task(client.get(
                f"/api/v1/runs/{child}/events?after_cursor={history['next_cursor']}", headers=headers(reader),
            ))
            try:
                await asyncio.wait_for(polled.wait(), 3)
                await spaces.remove_member(owner, team.id, reader.user_id)
                closed_stream = await asyncio.wait_for(streaming, 5)
                assert "event: access.revoked" in closed_stream.text
                assert "event: output.delta" not in closed_stream.text
            finally:
                if not streaming.done():
                    streaming.cancel()
                    await asyncio.gather(streaming, return_exceptions=True)
            assert (await client.get(source, headers=headers(reader))).status_code == 404
            assert (await client.get(f"/api/v1/runs/{child}/events", headers=headers(reader))).status_code == 404
            assert (await client.get(f"/api/v1/runs/{child}/result", headers=headers(reader))).status_code == 404
            assert (await client.post(f"{source}/resume", headers=headers(owner))).status_code == 202
            final = await _when(workflow, owner, run_id, lambda value: value["status"] == "SUCCEEDED")
            assert final["approval"]["consumed_at"] and calls.count(("POST", "/session/ses_handoff/message")) == 1
            if worker := workflow._workers.get(run_id):
                await asyncio.wait_for(asyncio.shield(worker), 5)
            slots = [await workflow._supervisor.acquire_slot() for _ in range(2)]
            try:
                pending = await client.post(f"/api/v1/spaces/{team.id}/tasks", headers=headers(owner), json={
                    "kind": "WORKFLOW", "request_id": str(uuid4()), "workflow_id": draft["id"], "version": "1",
                    "incident_id": q.incident.incident_id, "execution_id": q.execution.execution_id,
                })
                assert pending.status_code == 202 and pending.json()["status"] == "PENDING"
                pending_id = pending.json()["workflow_run_id"]
                cancel = f"/api/v1/workflow-runs/{pending_id}/cancel"
                assert (await client.post(cancel, headers=headers(reviewer))).status_code == 404
                assert (await client.post(cancel, headers=headers(owner))).status_code == 202
                cancelled = (await client.get(f"{base}/tasks?status=CANCELLED", headers=headers(reviewer))).json()
                assert [item["task_id"] for item in cancelled["items"]] == [pending_id]
            finally:
                for slot in slots:
                    workflow._supervisor.release_slot(slot)
    finally:
        await workflow.close()
        await adapter.close()
