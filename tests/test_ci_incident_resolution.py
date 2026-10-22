"""人工状态变更、真实验证事实与主动关闭的三个核心测试。"""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4
import httpx
import jwt
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, func
from core.auth import AuthService, AuthError
from core.persistence.models import UserRow, UserRoleRow, RoleRow
from core.ci_simulation.execution import initial_state, execute, case_catalog
from core.ci_simulation.actions import ACTION_OPTIONS, preview, apply_preview
from core.ci_simulation.models import ScenarioInstanceRow, CIExecutionRow
from core.incidents.contracts import ActionIntent, ConfirmAction, VerificationIntent, CIError
from core.incidents.models import IncidentRow
from core.incidents.service import IncidentService
from core.incidents import operations, verification
from core.incidents.http import router
from tests.test_ci_incident_diagnosis import user


def action_body(kind):
    option = next(o for o in ACTION_OPTIONS if o["action_kind"] == kind)
    return ActionIntent(request_id=uuid4(), action_kind=kind, parameters=option["parameters"])


async def setup_incident(database, key="report_pipeline_failure", principal=None, state=None):
    principal = principal or await user(database)
    service = IncidentService(database)
    instance, _ = await service.create_scenario(principal, key, str(uuid4()))
    if state is not None:
        # 仅设定首个执行之前的合成业务初态，不改执行结果或历史事实。
        async with database.transaction() as session:
            row = await session.get(ScenarioInstanceRow, instance.instance_id)
            row.current_state = state
    execution, incident, _ = await service.execute(principal, instance.instance_id, str(uuid4()))
    assert execution.outcome == "FAIL"
    return service, principal, instance, execution, incident


@pytest.mark.asyncio
async def test_manual_repair_preview_confirmation_and_state_revision(clean_database):
    service, principal, instance, original, incident = await setup_incident(clean_database)
    old_evidence, old_digest = deepcopy(original.evidence_manifest), original.evidence_digest
    body = action_body("UPGRADE_PARSER_DECODER")
    draft = await operations.create_action(service, principal, incident.incident_id, body)
    stale = await operations.create_action(service, principal, incident.incident_id, action_body("UPGRADE_PARSER_DECODER"))
    assert draft["status"] == "PREVIEWED" and draft["source_kind"] == "HUMAN_MANUAL"
    assert (await operations.current_state(service, principal, incident.incident_id))["state_revision"] == 1
    confirm = ConfirmAction(confirmed=True, preview_digest=draft["preview_digest"])
    applied = await operations.confirm_action(service, principal, incident.incident_id, draft["action_id"], confirm)
    repeated = await operations.confirm_action(service, principal, incident.incident_id, draft["action_id"], confirm)
    assert repeated == applied and applied["applied_state_revision"] == 2
    with pytest.raises(CIError, match="CI_ACTION_STALE"):
        await operations.confirm_action(service, principal, incident.incident_id, stale["action_id"],
            ConfirmAction(confirmed=True, preview_digest=stale["preview_digest"]))
    new, _, _ = await service.execute(principal, instance.instance_id, str(uuid4()), expected_state_revision=2)
    assert new.outcome == "PASS" and new.execution_id != original.execution_id and new.state_revision == 2
    async with clean_database.session() as session:
        saved = await session.get(CIExecutionRow, original.execution_id)
        assert saved.outcome == "FAIL" and saved.evidence_manifest == old_evidence and saved.evidence_digest == old_digest


@pytest.mark.asyncio
async def test_state_driven_rules_and_group_verification(clean_database):
    for key, kind in (("environment_configuration_failure", "SET_FEATURE_FLAG"),
            ("stale_case_input", "REFRESH_CASE_INPUT"), ("buffer_behavior_change", "DELIVER_PRODUCT_FIX")):
        state = initial_state(key)
        assert execute(state, str(uuid4()))["outcome"] == "FAIL"
        unrelated = deepcopy(state)
        unrelated["environment"]["parser_version"] = "parser-unrelated"
        assert execute(unrelated, str(uuid4()))["outcome"] == "FAIL"
        body = action_body(kind)
        frozen = preview(state, kind, None, body.parameters)
        repaired = apply_preview(state, kind, None, body.parameters, frozen)
        assert execute(repaired, str(uuid4()))["outcome"] == "PASS"
        assert repaired["test_case"]["golden_id"] == state["test_case"]["golden_id"]
    state = initial_state("buffer_behavior_change")
    state["environment"]["buffer_behavior"] = "legacy"
    assert execute(state, "legacy-main")["outcome"] == "PASS"
    assert execute(state, "legacy-modern", case_catalog(state)[1]["case_id"])["outcome"] == "FAIL"
    service, principal, instance, original, incident = await setup_incident(clean_database, "buffer_behavior_change")
    cases = [c["case_id"] for c in case_catalog(initial_state("buffer_behavior_change"))]
    failed = await verification.create_verification(service, principal, incident.incident_id,
        VerificationIntent(request_id=uuid4(), mode="GROUP", case_ids=cases, expected_state_revision=1))
    assert failed["outcome"] == "FAIL" and len(failed["items"]) == 2
    async with clean_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(IncidentRow)) == 1
    draft = await operations.create_action(service, principal, incident.incident_id, action_body("DELIVER_PRODUCT_FIX"))
    await operations.confirm_action(service, principal, incident.incident_id, draft["action_id"],
        ConfirmAction(confirmed=True, preview_digest=draft["preview_digest"]))
    body = VerificationIntent(request_id=uuid4(), mode="GROUP", case_ids=cases, expected_state_revision=2)
    passed = await verification.create_verification(service, principal, incident.incident_id, body)
    assert passed == await verification.create_verification(service, principal, incident.incident_id, body)
    assert passed["outcome"] == "PASS" and {i["state_revision"] for i in passed["items"]} == {2}
    async with clean_database.session() as session:
        assert await session.scalar(select(func.count()).select_from(CIExecutionRow)) == 5
        assert await session.scalar(select(func.count()).select_from(IncidentRow)) == 1


@pytest.mark.asyncio
async def test_human_close_requires_current_single_and_group_results(clean_database):
    owner, other = await user(clean_database), await user(clean_database)
    service_principal = replace(await user(clean_database), principal_kind="SERVICE", roles=frozenset({"SERVICE"}))
    async with clean_database.transaction() as session:
        session.add_all([RoleRow(id=UUID("00000000-0000-0000-0000-000000000001"), code="USER"),
            RoleRow(id=UUID("00000000-0000-0000-0000-000000000004"), code="SERVICE")])
        await session.flush()
        service_user = await session.get(UserRow, service_principal.user_id)
        service_user.principal_kind = "SERVICE"
        for principal in (owner, other, service_principal):
            session.add(UserRoleRow(user_id=principal.user_id,
                role_id=UUID("00000000-0000-0000-0000-000000000004" if principal.principal_kind == "SERVICE" else "00000000-0000-0000-0000-000000000001")))
    state = initial_state("buffer_behavior_change")
    state["environment"].update(buffer_behavior="legacy", parser_supported_field_revision=1, decoder_supported_field_revision=1)
    service, _, instance, original, incident = await setup_incident(clean_database, "buffer_behavior_change", owner, state)
    private = Ed25519PrivateKey.generate()
    settings = SimpleNamespace(jwt_public_key=private.public_key(), jwt_issuer="ci-test", jwt_audience="ci-test",
        jwt_allowed_algorithm="EdDSA", jwt_clock_skew_seconds=0)
    auth = AuthService(clean_database, settings)
    def token(principal):
        return jwt.encode(dict(iss="ci-test", aud="ci-test", sub=str(principal.user_id), roles=list(principal.roles),
            scopes=[], jti=uuid4().hex, iat=principal.issued_at, nbf=principal.issued_at, exp=principal.expires_at), private, algorithm="EdDSA")
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
    base = "/api/ci/incidents/" + incident.incident_id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer " + token(owner)}) as client:
        async def post(path, body, expected=200):
            response = await client.post(base + path, json=body)
            assert response.status_code == expected, response.text
            return response.json()
        draft = await post("/actions", action_body("UPGRADE_PARSER_DECODER").model_dump(mode="json"), 201)
        await post(f'/actions/{draft["action_id"]}/confirm', dict(confirmed=True, preview_digest=draft["preview_digest"]))
        single = await post("/verifications", dict(request_id=str(uuid4()), mode="SINGLE", case_ids=[original.case_id], expected_state_revision=2), 201)
        close = dict(request_id=str(uuid4()), expected_state_revision=2, action_id=draft["action_id"],
            single_verification_id=single["verification_id"], group_verification_id=str(uuid4()),
            root_cause="人工确认工具与Buffer兼容问题", fix_confirmation="完成工具修复并检查两类Feature", regression_confirmed=True)
        assert (await post("/close", close, 409))["detail"] == "CI_VERIFICATION_INCOMPLETE"
        group = await post("/verifications", dict(request_id=str(uuid4()), mode="GROUP", case_ids=[c["case_id"] for c in case_catalog(state)], expected_state_revision=2), 201)
        close["group_verification_id"] = group["verification_id"]
        assert (await post("/close", close, 409))["detail"] == "CI_VERIFICATION_FAILED"
        repair = await post("/actions", action_body("DELIVER_PRODUCT_FIX").model_dump(mode="json"), 201)
        await post(f'/actions/{repair["action_id"]}/confirm', dict(confirmed=True, preview_digest=repair["preview_digest"]))
        close.update(action_id=repair["action_id"], expected_state_revision=3)
        assert (await post("/close", close, 409))["detail"] == "CI_VERIFICATION_INCOMPLETE"
        single = await post("/verifications", dict(request_id=str(uuid4()), mode="SINGLE", case_ids=[original.case_id], expected_state_revision=3), 201)
        group = await post("/verifications", dict(request_id=str(uuid4()), mode="GROUP", case_ids=[c["case_id"] for c in case_catalog(state)], expected_state_revision=3), 201)
        close.update(single_verification_id=single["verification_id"], group_verification_id=group["verification_id"])
        for principal, status in ((service_principal, 403), (other, 404)):
            response = await client.post(base + "/close", json=close, headers={"Authorization": "Bearer " + token(principal)})
            assert response.status_code == status
        closed = await post("/close", close)
        assert closed == await post("/close", close) and closed["status"] == "CLOSED"
        detail = (await client.get(base)).json()
        assert detail["closure"] == closed["closure"] and detail["execution"]["outcome"] == "FAIL" and not detail["investigations"]
