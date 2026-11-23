import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import select

from core.agent_platform.registrations import BUSINESS_REGISTRATION_BUNDLE
from core.agent_platform.contracts import BEFORE_STEP_DISPATCH
from core.agent_platform.workflow import WorkflowError
from core.agent_platform.workflow_artifacts import artifact_path
from core.auth import AuthError, AuthService
from core.persistence.models import DurableToolInvocationRow, RunControlRow, UserRow, WorkflowStepRunRow
from core.persistence.repositories.workflow_approvals import WorkflowApprovalError
from core.runtime.memory_scope import MemoryAccessContext
from tests.test_stage6_wp2_ownership import _create_user, _disabled_rate_limiter, _principal, _token
from tests.test_workflow_artifacts import _setup
from tests.test_workflow_execution import _when


REVIEW = next(item for item in BUSINESS_REGISTRATION_BUNDLE.workflows if item.workflow_id == "ci_artifact_review")


async def _start_review(service, q):
    created = await service.create(q.principal, REVIEW.workflow_id, uuid4(),
                                   incident_id=q.incident.incident_id, execution_id=q.execution.execution_id)
    run_id = created["workflow_run_id"]
    waiting = await _when(service, q.principal, run_id, lambda view: view["status"] in {
        "WAITING_APPROVAL", "FAILED", "PAUSED", "SUCCEEDED",
    })
    assert waiting["status"] == "WAITING_APPROVAL", waiting
    assert waiting["steps"][0]["runtime_status"] == waiting["steps"][0]["status"] == "SUCCEEDED"
    assert waiting["steps"][0]["artifact_ref"]["sha256"]
    assert waiting["steps"][1]["platform_run_id"] is None and not waiting["result_available"]
    assert waiting["approval"]["status"] == "PENDING" and waiting["approval"]["consumed_at"] is None
    assert waiting["frozen_ci_input"] == q.query
    return run_id, waiting


def _http(database, authorization, service, monkeypatch):
    import server

    private = Ed25519PrivateKey.generate()
    settings = replace(server.settings, jwt_issuer="test-issuer", jwt_audience="test-api",
                       jwt_public_key=private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode())
    for name, value in {
        "auth_service": AuthService(database, settings), "authorization_service": authorization,
        "rate_limiter": _disabled_rate_limiter(), "workflow_service": service,
    }.items():
        monkeypatch.setattr(server.app.state, name, value, raising=False)

    def headers(principal):
        return {"Authorization": f"Bearer {_token(private, principal.user_id, sorted(principal.roles))}"}

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test"), headers


@pytest.mark.asyncio
async def test_persisted_review_restarts_and_admin_approval_only_allows_owner_fast_path(clean_database, tmp_path, monkeypatch):
    q, _, calls, adapter, build = await _setup(clean_database, tmp_path, definition=REVIEW)
    service, restarted = build(), build()
    admin = _principal(await _create_user(clean_database, ("ADMIN",)), "ADMIN")
    try:
        run_id, waiting = await _start_review(service, q)
        _, (_, pending_native) = await service._repository.read(run_id)
        access = MemoryAccessContext.from_principal(q.principal, pending_native.agent_id, f"{run_id}:{pending_native.step_id}")
        with pytest.raises(WorkflowApprovalError, match="WORKFLOW_APPROVAL_REQUIRED"):
            await service._repository.prepare(run_id, 1, q.principal, access.conversation_id,
                                              effective_input=pending_native.bound_input)
        assert (await service.resume(q.principal, run_id))["status"] == "WAITING_APPROVAL"
        if worker := service._workers.get(run_id):
            await asyncio.wait_for(asyncio.shield(worker), 4)
        assert not service._workers
        supervisor = service._supervisor
        assert await supervisor.try_acquire_slot() is True
        assert await supervisor.try_acquire_slot() is True
        supervisor.release_slot(True)
        supervisor.release_slot(True)
        await service.close()

        _, steps, approval = await restarted._repository.snapshot(run_id)
        assert approval.approval_id == waiting["approval"]["approval_id"]
        assert approval.binding_digest == waiting["approval"]["binding_digest"]
        assert steps[0].platform_run_id == waiting["steps"][0]["platform_run_id"]
        assert steps[0].result_ref["message_id"] and steps[0].artifact_ref["sha256"] == waiting["steps"][0]["artifact_ref"]["sha256"]
        assert json.loads(steps[1].bound_input) == q.query

        client, headers = _http(clean_database, q.authorization, restarted, monkeypatch)
        path = f"/api/v1/workflow-runs/{run_id}"
        async with client:
            response = await client.post(f"{path}/approvals/{approval.approval_id}/decision", headers=headers(admin),
                                         json={"decision": "APPROVE", "binding_digest": approval.binding_digest})
            assert response.status_code == 200 and response.json()["status"] == "APPROVED", response.text
            assert response.json()["decided_by"] == str(admin.user_id) and response.json()["consumed_at"] is None
            accepted = (await client.get(path, headers=headers(admin))).json()
            assert accepted["status"] == "WAITING_APPROVAL" and accepted["requires_owner_resume"]
            assert accepted["steps"][1]["platform_run_id"] is None and not restarted._workers
            assert (await client.post(f"{path}/resume", headers=headers(admin))).status_code == 404
            resumed = await asyncio.gather(*(client.post(f"{path}/resume", headers=headers(q.principal)) for _ in range(2)))
            assert [response.status_code for response in resumed] == [202, 202]

        final = await _when(restarted, q.principal, run_id, lambda view: view["status"] in {"SUCCEEDED", "FAILED", "PAUSED"})
        assert final["status"] == "SUCCEEDED" and not final["requires_owner_resume"], final
        assert final["approval"]["status"] == "APPROVED" and final["approval"]["consumed_at"] is not None
        assert final["approval"]["decided_by"] == str(admin.user_id)
        native_id = final["steps"][1]["platform_run_id"]
        image = await q.repo.load(native_id)
        route = image.root.resume_input["adaptive_route"]
        assert route["query"] == q.query and route["principal"]["user_id"] == str(q.principal.user_id)
        assert image.root.execution_policy == "fixed-plan.v1"
        assert len(image.tool_invocations) == 1 and image.tool_invocations[0].state == "COMMITTED"
        delivery = (await q.factory.services.client_event_feed.read_after(native_id, 0, 100))[-1]
        assert json.loads(delivery.payload["output"])["summary"] == q.execution.summary
        assert not q.model.requests and not image.models
        assert calls.count(("POST", "/session/ses_handoff/message")) == 1
    finally:
        await service.close()
        await restarted.close()
        await adapter.close()


@pytest.mark.asyncio
async def test_approval_rejection_is_idempotent_and_unauthorized_users_cannot_decide(clean_database, tmp_path, monkeypatch):
    q, _, calls, adapter, build = await _setup(clean_database, tmp_path, definition=REVIEW)
    service = build()
    same_tenant_user = _principal(await _create_user(clean_database, ("USER",)), "USER")
    other_tenant_admin = _principal(await _create_user(clean_database, ("ADMIN",), tenant_id="other"), "ADMIN", tenant_id="other")
    disabled_admin = _principal(await _create_user(clean_database, ("ADMIN",)), "ADMIN")
    async with clean_database.transaction() as session:
        user = await session.get(UserRow, disabled_admin.user_id)
        user.disabled_at = datetime.now(UTC)
    try:
        unsupported = replace(REVIEW, workflow_id="unsupported_review", approval_phase=BEFORE_STEP_DISPATCH)
        service._workflows[unsupported.workflow_id] = unsupported
        with pytest.raises(WorkflowError, match="WORKFLOW_STEP_CONSTRAINT_UNSUPPORTED"):
            await service.create(q.principal, unsupported.workflow_id, uuid4(),
                                 incident_id=q.incident.incident_id, execution_id=q.execution.execution_id)
        assert not calls
        run_id, waiting = await _start_review(service, q)
        approval_id = waiting["approval"]["approval_id"]
        client, headers = _http(clean_database, q.authorization, service, monkeypatch)
        url = f"/api/v1/workflow-runs/{run_id}/approvals/{approval_id}/decision"
        async with client:
            for actor in (same_tenant_user, other_tenant_admin):
                assert (await client.post(url, headers=headers(actor), json={"decision": "APPROVE"})).status_code == 404
            stale = await client.post(url, headers=headers(q.principal), json={"decision": "APPROVE", "binding_digest": "0" * 64})
            assert stale.status_code == 409 and stale.json()["error"]["code"] == "WORKFLOW_APPROVAL_BINDING_MISMATCH"
            for principal in (
                replace(q.principal, expires_at=datetime.now(UTC) - timedelta(seconds=1)),
                replace(q.principal, roles=frozenset({"ADMIN"})),
                disabled_admin,
            ):
                with pytest.raises(AuthError):
                    await service.decide_approval(principal, run_id, approval_id, "APPROVE")
            reject = await client.post(url, headers=headers(q.principal), json={"decision": "REJECT"})
            repeated = await client.post(url, headers=headers(q.principal), json={"decision": "REJECT"})
            assert reject.status_code == repeated.status_code == 200 and reject.json() == repeated.json()
            assert reject.json()["decided_by"] == str(q.principal.user_id) and reject.json()["status"] == "REJECTED"
            opposite = await client.post(url, headers=headers(q.principal), json={"decision": "APPROVE"})
            assert opposite.status_code == 409 and opposite.json()["error"]["code"] == "WORKFLOW_APPROVAL_DECISION_CONFLICT"
        final = await service.resume(q.principal, run_id)
        assert final["status"] == "REJECTED" and final["steps"][0]["runtime_status"] == "SUCCEEDED"
        assert final["steps"][1]["platform_run_id"] is None and final["approval"]["consumed_at"] is None
        assert not service._workers and not q.model.requests and calls.count(("POST", "/session/ses_handoff/message")) == 1
    finally:
        await service.close()
        await adapter.close()


@pytest.mark.asyncio
async def test_approved_artifact_change_never_consumes_decision_or_claims_native(clean_database, tmp_path, monkeypatch):
    q, workspace, calls, adapter, build = await _setup(clean_database, tmp_path, definition=REVIEW)
    service, restarted = build(), build()
    control = q.factory.services.durable_run_control
    claim = control.claim
    replacement_done = False
    try:
        run_id, waiting = await _start_review(service, q)
        approval = await service.decide_approval(q.principal, run_id, waiting["approval"]["approval_id"], "APPROVE")

        async def replace_at_native_claim(child_run_id, owner_id, **kwargs):
            nonlocal replacement_done
            if not replacement_done and kwargs.get("workflow_request") is not None:
                path = workspace / artifact_path(run_id)
                replacement = path.with_suffix(".tmp")
                replacement.write_bytes(path.read_bytes() + b" ")
                replacement.replace(path)
                replacement_done = True
            return await claim(child_run_id, owner_id, **kwargs)

        monkeypatch.setattr(control, "claim", replace_at_native_claim)
        await service.resume(q.principal, run_id)
        paused = await _when(service, q.principal, run_id, lambda view: view["status"] in {"PAUSED", "SUCCEEDED", "FAILED"})
        assert paused["status"] == "PAUSED" and paused["reason"] == "WORKFLOW_ARTIFACT_CHANGED", paused
        native_id = paused["steps"][1]["platform_run_id"]
        assert native_id is not None and replacement_done and paused["approval"] == approval
        await service.close()

        # 修改持久 Workflow 绑定，检验旧决定在真正检查文件之前也无法放行。
        _, (producer, _), _ = await restarted._repository.snapshot(run_id)
        async with clean_database.transaction() as session:
            row = await session.get(WorkflowStepRunRow, (run_id, producer.step_id, producer.attempt))
            row.result_ref = {**row.result_ref, "message_id": "msg_unapproved"}
        await restarted.resume(q.principal, run_id)
        checked = await _when(restarted, q.principal, run_id, lambda view: view["reason"] == "WORKFLOW_APPROVAL_BINDING_MISMATCH")
        assert checked["status"] == "PAUSED" and checked["approval"] == approval
        assert checked["steps"][1]["platform_run_id"] == native_id
        async with clean_database.session() as session:
            assert await session.get(RunControlRow, native_id) is None
            assert await session.scalar(select(DurableToolInvocationRow.invocation_id).where(
                DurableToolInvocationRow.run_id == native_id,
            ).limit(1)) is None
        assert await q.repo.load(native_id) is None and not q.model.requests
        assert calls.count(("POST", "/session/ses_handoff/message")) == 1
    finally:
        await service.close()
        await restarted.close()
        await adapter.close()


@pytest.mark.asyncio
async def test_concurrent_approval_and_rejection_make_one_decision(clean_database, tmp_path):
    q, _, _, adapter, build = await _setup(clean_database, tmp_path, definition=REVIEW)
    service, other = build(), build()
    try:
        run_id, waiting = await _start_review(service, q)
        approval_id = waiting["approval"]["approval_id"]
        decisions = await asyncio.gather(
            service.decide_approval(q.principal, run_id, approval_id, "APPROVE"),
            other.decide_approval(q.principal, run_id, approval_id, "REJECT"),
            return_exceptions=True,
        )
        winner, = [result for result in decisions if isinstance(result, dict)]
        loser, = [result for result in decisions if isinstance(result, WorkflowError)]
        assert loser.code == "WORKFLOW_APPROVAL_DECISION_CONFLICT"
        final = await other.get(q.principal, run_id)
        assert final["approval"] == winner and final["steps"][1]["platform_run_id"] is None
        assert final["status"] == ("WAITING_APPROVAL" if winner["status"] == "APPROVED" else "REJECTED")
    finally:
        await service.close()
        await other.close()
        await adapter.close()


@pytest.mark.asyncio
async def test_cancel_and_approval_race_never_revives_workflow(clean_database, tmp_path):
    q, _, calls, adapter, build = await _setup(clean_database, tmp_path, definition=REVIEW)
    service, other = build(), build()
    try:
        run_id, waiting = await _start_review(service, q)
        approval_id = waiting["approval"]["approval_id"]
        decision, cancelled = await asyncio.gather(
            service.decide_approval(q.principal, run_id, approval_id, "APPROVE"),
            other.cancel(q.principal, run_id),
            return_exceptions=True,
        )
        assert cancelled["status"] == "CANCELLED" and cancelled["cancel_requested"]
        if isinstance(decision, WorkflowError):
            assert decision.code == "WORKFLOW_APPROVAL_INVALIDATED"
            assert cancelled["approval"]["status"] == "INVALIDATED"
        else:
            assert decision["status"] == cancelled["approval"]["status"] == "APPROVED"
        with pytest.raises(WorkflowError, match="WORKFLOW_APPROVAL_INVALIDATED"):
            await service.decide_approval(q.principal, run_id, approval_id, "APPROVE")
        final = await service.resume(q.principal, run_id)
        assert final["status"] == "CANCELLED" and final["steps"][0]["runtime_status"] == "SUCCEEDED"
        assert final["steps"][1]["platform_run_id"] is None and final["approval"]["consumed_at"] is None
        assert not q.model.requests and calls.count(("POST", "/session/ses_handoff/message")) == 1
    finally:
        await service.close()
        await other.close()
        await adapter.close()
