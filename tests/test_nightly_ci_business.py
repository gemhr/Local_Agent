"""夜间顺序、证据关联与权限、WP2 衔接的三个真实 PG 业务测试。"""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4, UUID
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import select, func
from core.auth import AuthService, AuthError
from core.persistence.models import RoleRow, UserRoleRow
from core.ci_simulation.models import CIExecutionRow, ScenarioInstanceRow, NightlyVersionExecutionRow, NightlyCIBatchRow
from core.incidents.models import IncidentRow, IncidentGroupRow, UserNotificationRow
from core.incidents.service import IncidentService
from core.incidents.contracts import CreateNightly, NightlyClock, AdvanceNightly, GroupDecision, CIError
from core.incidents import nightly, grouping, notifications
from core.incidents.http import router
from core.incidents.nightly_http import router as nightly_router
from core.ci_simulation.nightly import ENVIRONMENTS
from core.ci_simulation.execution import execute, initial_state
from core.runtime.snapshot_serialization import sha256_digest
from tests.test_ci_incident_diagnosis import user


async def setup(database, size=4):
    operator = replace(await user(database), roles=frozenset({"USER", "OPERATOR"}))
    a, b = await user(database), await user(database)
    body = CreateNightly(request_id=uuid4(), business_date=date(2026, 10, 22), environments=[dict(
        environment_id=e["environment_id"], profile_key=e["profile_key"], owner_user_id=str((a, b)[i % 2].user_id),
        duration_profile="normal") for i, e in enumerate(ENVIRONMENTS[:size])])
    service = IncidentService(database)
    created = await nightly.create(service, operator, body)
    assert created["created"]
    assert (await nightly.create(service, operator, body))["batch_id"] == created["batch_id"]
    return service, operator, a, b, created["batch_id"]


async def move(service, operator, batch_id, steps):
    return await nightly.advance(service, operator, batch_id, AdvanceNightly(steps=steps))


async def set_clock(service, operator, batch_id, hour):
    return await nightly.clock(service, operator, batch_id, NightlyClock(target_business_time=datetime(2026, 10, 22, tzinfo=timezone(timedelta(hours=8))) + timedelta(hours=hour)))


async def finish(service, operator, batch_id, hour=0):
    while True:
        detail = await nightly.detail(service, operator, batch_id)
        if detail["status"] == "COMPLETED":
            return detail
        steps = [v["next_steps"][0] for v in detail["versions"] if v["next_steps"]]
        hour += 2
        await set_clock(service, operator, batch_id, hour)
        await move(service, operator, batch_id, steps)


@pytest.mark.asyncio
async def test_nightly_order_progress_seal_and_idempotency(clean_database):
    service, op, a, b, batch_id = await setup(clean_database)
    initial = await nightly.detail(service, op, batch_id)
    assert initial["provisional_progress"]["total"] == 0 and initial["frozen_summary"] is None
    va, vb = [v for v in initial["versions"] if v["environment_id"] == "env-parser-a"][:2]
    first = va["next_steps"][0]
    with pytest.raises(CIError, match="CI_NIGHTLY_NOT_DUE"):
        await move(service, op, batch_id, [first])
    await set_clock(service, op, batch_id, 0)
    await move(service, op, batch_id, [first])
    with pytest.raises(CIError, match="CI_NIGHTLY_ORDER_CONFLICT"):
        await move(service, op, batch_id, [dict(version_execution_id=vb["version_execution_id"], kind="START")])
    premature = dict(version_execution_id=va["version_execution_id"], kind="COMPLETE_CASE", case_id=va["slots"][0]["case_id"])
    with pytest.raises(CIError, match="CI_NIGHTLY_NOT_DUE"):
        await move(service, op, batch_id, [premature])
    await set_clock(service, op, batch_id, 2)
    one = await move(service, op, batch_id, [premature])
    retry = await move(service, op, batch_id, [premature])
    assert retry["results"][0]["reused"] and retry["results"][0]["execution_id"] == one["results"][0]["execution_id"]
    assert (await nightly.detail(service, op, batch_id))["provisional_progress"]["total"] == 1
    second = dict(premature, case_id=va["slots"][1]["case_id"])
    await move(service, op, batch_id, [second])
    await move(service, op, batch_id, [dict(version_execution_id=vb["version_execution_id"], kind="START")])
    middle = await nightly.detail(IncidentService(clean_database), op, batch_id)
    assert next(v for v in middle["versions"] if v["version_execution_id"] == vb["version_execution_id"])["status"] == "RUNNING"
    assert all(v["status"] == "PENDING" for v in middle["versions"] if v["environment_id"] == "env-parser-b")
    final = await finish(service, op, batch_id, 2)
    assert final["frozen_summary"]["total"] == 24
    assert sha256_digest(final["frozen_summary"]) == final["sealed_digest"]
    await move(service, op, batch_id, [premature])
    await move(service, op, batch_id, [first])
    async with clean_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(CIExecutionRow)) == 24
        assert await session.scalar(select(func.count()).select_from(ScenarioInstanceRow)) == 24
        rows = (await session.execute(select(UserNotificationRow))).scalars().all()
        assert len(rows) == 4 and len({(r.batch_id, r.recipient_user_id, r.kind) for r in rows}) == 4
        for version in (await session.execute(select(NightlyVersionExecutionRow))).scalars():
            assert version.frozen_cases[0]["initial_state"]["environment"] == version.frozen_cases[1]["initial_state"]["environment"]
            snapshots = (await session.execute(select(CIExecutionRow).where(CIExecutionRow.nightly_version_execution_id == version.version_execution_id))).scalars().all()
            assert snapshots[0].instance_id != snapshots[1].instance_id
            assert snapshots[0].execution_snapshot["environment"] == snapshots[1].execution_snapshot["environment"]
        original_request = (await session.get(NightlyCIBatchRow, batch_id)).request_id
    next_night = CreateNightly(request_id=uuid4(), business_date=date(2026, 10, 23), environments=[
        {key: e[key] for key in ("environment_id", "profile_key", "owner_user_id", "duration_profile")}
        for e in initial["frozen_plan"]["environments"]])
    await nightly.create(service, op, next_night)
    # request_id 和日期分别撞到两条既有计划，仍返回业务冲突而非多行查询异常。
    with pytest.raises(CIError, match="CI_REQUEST_CONFLICT"):
        await nightly.create(service, op, next_night.model_copy(update={"request_id": UUID(original_request)}))


@pytest.mark.asyncio
async def test_observable_grouping_correction_and_privacy(clean_database):
    service, op, a, b, batch_id = await setup(clean_database, 6)
    await set_clock(service, op, batch_id, 0)
    initial = await nightly.detail(service, op, batch_id)
    starts = [v["next_steps"][0] for v in initial["versions"] if v["next_steps"]]
    await move(service, op, batch_id, starts)
    await set_clock(service, op, batch_id, 2)
    running = await nightly.detail(service, op, batch_id)
    await move(service, op, batch_id, [v["next_steps"][0] for v in running["versions"] if v["next_steps"]])
    fails = (await nightly.failures(service, op, batch_id))["items"]
    assert any(f["grouping_status"] == "INSUFFICIENT_EVIDENCE" for f in fails)
    async with clean_database.transaction() as session:
        batch, manager = await nightly.scope(session, op, batch_id, lock=True)
        groups = (await grouping.list_groups(session, op, batch, manager))["items"]
        candidate = next(g for g in groups if g["state"] == "CANDIDATE")
        assert {m["environment_id"] for m in candidate["members"]} == {"env-parser-a", "env-parser-b"}
        chosen = candidate["members"][0]["incident_id"]
        body = GroupDecision(request_id=uuid4(), expected_group_revision=candidate["revision"], kind="CONFIRM",
            incident_ids=[chosen], reason="管理备注：env-parser-b 的详细说明不应给另一负责人")
        confirmed = await grouping.decide(session, op, batch, candidate["group_id"], True, body)
        assert confirmed == await grouping.decide(session, op, batch, candidate["group_id"], True, body)
    async with clean_database.transaction() as session:
        batch, _ = await nightly.scope(session, a, batch_id, lock=True)
        group = await grouping.get_group(session, a, batch, candidate["group_id"], False)
        scoped = await grouping.payload(session, a, group, False)
        assert all(m["owner_user_id"] == str(a.user_id) for m in scoped["members"])
        assert "管理备注" not in str(scoped)
        other = next(m["incident_id"] for m in candidate["members"] if m["owner_user_id"] == str(b.user_id))
        with pytest.raises(CIError, match="CI_NOT_FOUND"):
            await grouping.decide(session, a, batch, group.group_id, False, GroupDecision(request_id=uuid4(),
                expected_group_revision=group.revision, kind="EXCLUDE", incident_ids=[other], reason="无权操作"))
        mine = scoped["members"][0]["incident_id"]
        split = await grouping.decide(session, a, batch, group.group_id, False, GroupDecision(request_id=uuid4(),
            expected_group_revision=group.revision, kind="SPLIT", incident_ids=[mine], reason="人工拆出独立核对"))
        target = await grouping.get_group(session, a, batch, split["target_group_id"], False)
        await grouping.decide(session, a, batch, target.group_id, False, GroupDecision(request_id=uuid4(),
            expected_group_revision=1, kind="EXCLUDE", incident_ids=[mine], reason="排除该候选关联"))
        assert len(group.history) == 2 and len(target.history) == 2
    with pytest.raises(CIError):
        await nightly.detail(service, replace(a, tenant_id="other-tenant"), batch_id)
    with pytest.raises(CIError, match="CI_HUMAN_REQUIRED"):
        await nightly.detail(service, replace(a, principal_kind="SERVICE"), batch_id)
    with pytest.raises(CIError, match="CI_NOT_FOUND"):
        await service.detail(op, mine)
    # 用真实规则输出验证缺日志/摘要不符及不同前序，签名不读取模板或内部状态。
    for key in ("environment_configuration_failure", "stale_case_input", "buffer_behavior_change", "limited_diagnostics"):
        result = execute(initial_state(key), str(uuid4()))
        assert grouping.signature(SimpleNamespace(execution_id=result["summary"]["execution_id"], **result))[0] is None
    result = execute(initial_state("report_pipeline_failure"), str(uuid4()))
    result["execution_snapshot"] = {"irrelevant": True}
    assert grouping.signature(SimpleNamespace(execution_id=result["summary"]["execution_id"], **result))[0]
    result["evidence_manifest"][0]["digest"] = "bad"
    assert grouping.signature(SimpleNamespace(execution_id=result["summary"]["execution_id"], **result))[0] is None


@pytest.mark.asyncio
async def test_nightly_incident_wp2_and_notifications(clean_database):
    service, op, a, b, batch_id = await setup(clean_database)
    final = await finish(service, op, batch_id)
    original_digest = final["sealed_digest"]
    failed = next(i for i in final["frozen_summary"]["items"] if i["environment_id"] == "env-parser-a" and i["software_version"] == "product-2.0" and i["outcome"] == "FAIL")
    incident_id = failed["incident_id"]
    before = await service.detail(a, incident_id)
    inbox = await notifications.inbox(service, a)
    notification = inbox["items"][0]
    receipt = await notifications.mark_read(service, a, notification["notification_id"])
    assert receipt == await notifications.mark_read(service, a, notification["notification_id"])
    assert (await service.detail(a, incident_id))["status"] == "OPEN"
    with pytest.raises(CIError):
        await notifications.mark_read(service, b, notification["notification_id"])
    private = Ed25519PrivateKey.generate()
    async with clean_database.transaction() as session:
        for index, code in ((1, "USER"), (2, "OPERATOR")):
            session.add(RoleRow(id=UUID(f"00000000-0000-0000-0000-{index:012d}"), code=code))
        await session.flush()
        for principal in (op, a, b):
            for role in principal.roles:
                session.add(UserRoleRow(user_id=principal.user_id, role_id=UUID("00000000-0000-0000-0000-000000000001" if role == "USER" else "00000000-0000-0000-0000-000000000002")))
    auth = AuthService(clean_database, SimpleNamespace(jwt_public_key=private.public_key(), jwt_issuer="nightly-test",
        jwt_audience="nightly-test", jwt_allowed_algorithm="EdDSA", jwt_clock_skew_seconds=0))
    def headers(principal):
        token = jwt.encode(dict(iss="nightly-test", aud="nightly-test", sub=str(principal.user_id), roles=list(principal.roles), scopes=[],
            jti=uuid4().hex, iat=principal.issued_at, nbf=principal.issued_at, exp=principal.expires_at), private, algorithm="EdDSA")
        return {"Authorization": "Bearer " + token}
    app = FastAPI()
    app.state.incident_service = service
    @app.middleware("http")
    async def authenticate(request, call_next):
        try:
            request.state.principal = await auth.authenticate(request.headers.get("authorization"))
        except AuthError as exc:
            return JSONResponse({"detail": exc.code}, status_code=exc.status_code)
        return await call_next(request)
    app.include_router(router)
    app.include_router(nightly_router)
    base = f"/api/ci/incidents/{incident_id}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers(a)) as client:
        draft_response = await client.post(base + "/actions", json=dict(request_id=str(uuid4()), action_kind="UPGRADE_PARSER_DECODER", parameters={"target_version": "2.0"}))
        assert draft_response.status_code == 201, draft_response.text
        draft = draft_response.json()
        assert (await client.post(base + f'/actions/{draft["action_id"]}/confirm', json=dict(confirmed=True, preview_digest=draft["preview_digest"]))).status_code == 200
        cases = (await client.get(base + "/state")).json()["required_case_ids"]
        batches = []
        for mode, ids in (("SINGLE", cases[:1]), ("GROUP", cases)):
            response = await client.post(base + "/verifications", json=dict(request_id=str(uuid4()), mode=mode, case_ids=ids, expected_state_revision=2))
            assert response.status_code == 201 and response.json()["outcome"] == "PASS", response.text
            batches.append(response.json()["verification_id"])
        body = dict(request_id=str(uuid4()), action_id=draft["action_id"], expected_state_revision=2,
            single_verification_id=batches[0], group_verification_id=batches[1], root_cause="人工确认工具兼容性不足", fix_confirmation="工具升级并通过两例回归", regression_confirmed=True)
        assert (await client.post(base + "/close", json=body, headers=headers(op))).status_code == 404
        closed = await client.post(base + "/close", json=body)
        assert closed.status_code == 200 and closed.json()["status"] == "CLOSED", closed.text
    after = await service.detail(a, incident_id)
    assert before["execution"] == after["execution"] and before["evidence"] == after["evidence"]
    assert (await nightly.detail(service, op, batch_id))["sealed_digest"] == original_digest
    after_inbox = await notifications.inbox(service, a)
    assert len(after_inbox["current_todos"]) == len(inbox["current_todos"]) - 1
    assert after_inbox["items"][0]["original_failures"] == notification["original_failures"]
