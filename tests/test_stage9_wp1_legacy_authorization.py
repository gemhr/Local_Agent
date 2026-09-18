from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

import server
from core.auth import AuthError, AuthorizationService, Principal
from core.persistence.models import (
    BusinessReviewRow,
    FeatureTestMissionRow,
    Stage8ExternalExecutionJobRow,
    Stage8TestPlanRow,
    Stage8TicketContinuationRow,
    TenantRow,
    UserRow,
)
from core.persistence.errors import PersistenceError
from core.stage8.service import MissionService
from core.stage8.specialists import FeatureUnderstandingRequest

pytest_plugins = ("tests._pg_fixtures",)


TENANT_A = "00000000-0000-0000-0000-000000000001"
TENANT_B = "tenant-b"


def _principal(
    user_id: uuid.UUID,
    *,
    tenant_id: str = TENANT_A,
    kind: str = "HUMAN",
    roles: tuple[str, ...] = ("USER",),
    scopes: tuple[str, ...] = (),
) -> Principal:
    now = datetime.now(UTC)
    return Principal(
        user_id,
        str(user_id),
        frozenset(roles),
        uuid.uuid4().hex,
        now,
        now + timedelta(minutes=5),
        kind,
        frozenset(scopes),
        tenant_id,
    )


async def _user(database, *, tenant_id: str = TENANT_A, kind: str = "HUMAN"):
    user_id = uuid.uuid4()
    async with database.transaction() as session:
        if await session.get(TenantRow, tenant_id) is None:
            session.add(TenantRow(tenant_id=tenant_id))
            await session.flush()
        session.add(
            UserRow(
                id=user_id,
                subject=str(user_id),
                display_name="wp1",
                principal_kind=kind,
                tenant_id=tenant_id,
            )
        )
    return user_id


def _request(principal: Principal, database, **services):
    state = SimpleNamespace(
        authorization_service=AuthorizationService(database),
        **services,
    )
    return SimpleNamespace(state=SimpleNamespace(principal=principal), app=SimpleNamespace(state=state))


async def _execution_job(database, mission_id: str, *, execution_id: str):
    async with database.transaction() as session:
        row = Stage8ExternalExecutionJobRow(
            job_id=uuid.uuid4().hex,
            mission_id=mission_id,
            execution_id=execution_id,
            plan_id="plan",
            case_id="case",
            environment_id="env",
            executor_id="executor",
            status="FAILED",
            plan_payload={},
        )
        session.add(row)
        await session.flush()
        return row.job_id


@pytest.mark.asyncio
async def test_foreign_owner_cannot_start_mission_planning(clean_database):
    owner_id = await _user(clean_database)
    foreign_id = await _user(clean_database)
    mission = await MissionService(clean_database).create_mission(
        "feature-a",
        mission_id="mission-planning",
        owner_user_id=owner_id,
        tenant_id=TENANT_A,
    )

    class _Specialists:
        calls = 0

        async def planning_workflow(self, _body):
            self.calls += 1
            raise AssertionError("planning must not run")

    specialists = _Specialists()
    request = _request(
        _principal(foreign_id),
        clean_database,
        stage8_specialist_service=specialists,
    )
    body = FeatureUnderstandingRequest(
        mission_id=mission.mission_id,
        context={"feature_id": "feature-a"},
    )
    with pytest.raises(AuthError) as denied:
        await server.stage8_planning_workflow(mission.mission_id, body, request)
    assert denied.value.status_code == 404
    assert specialists.calls == 0

    async with clean_database.session() as session:
        stored = await session.get(FeatureTestMissionRow, mission.mission_id)
        assert stored.status == "CREATED"
        assert await session.scalar(select(func.count()).select_from(Stage8TestPlanRow)) == 0
        assert await session.scalar(select(func.count()).select_from(BusinessReviewRow)) == 0


@pytest.mark.asyncio
async def test_provider_callback_and_triage_share_canonical_job(clean_database):
    owner_id = await _user(clean_database)
    service_id = await _user(clean_database, kind="SERVICE")
    cross_service_id = await _user(
        clean_database, tenant_id=TENANT_B, kind="SERVICE"
    )
    mission = await MissionService(clean_database).create_mission(
        "feature-callback",
        mission_id="mission-callback",
        owner_user_id=owner_id,
        tenant_id=TENANT_A,
    )
    job_id = await _execution_job(
        clean_database, mission.mission_id, execution_id="EXEC-CALLBACK"
    )

    class _Execution:
        ingested: list[str] = []
        triaged: list[str] = []

        async def ingest_result(self, _result, *, expected_job_id=None):
            self.ingested.append(expected_job_id)
            return {"job_id": expected_job_id}

        async def triage_job(self, resolved_job_id):
            self.triaged.append(resolved_job_id)
            return {"job_id": resolved_job_id}

    execution = _Execution()
    callback_body = server.Stage8ExecutionResultRequest(
        execution_id="EXEC-CALLBACK",
        status="FAILED",
        actual_result="failed",
    )
    allowed = _request(
        _principal(
            service_id,
            kind="SERVICE",
            roles=("SERVICE",),
            scopes=(server.STAGE8_RESULT_CALLBACK_SCOPE,),
        ),
        clean_database,
        stage8_execution_service=execution,
    )
    result = await server.stage8_ingest_execution_result(
        "EXEC-CALLBACK", callback_body, allowed
    )
    assert result["job_id"] == job_id
    assert execution.ingested == [job_id]

    cross = _request(
        _principal(
            cross_service_id,
            tenant_id=TENANT_B,
            kind="SERVICE",
            roles=("SERVICE",),
            scopes=(server.STAGE8_RESULT_CALLBACK_SCOPE,),
        ),
        clean_database,
        stage8_execution_service=execution,
    )
    with pytest.raises(AuthError) as cross_denied:
        await server.stage8_ingest_execution_result(
            "EXEC-CALLBACK", callback_body, cross
        )
    assert cross_denied.value.status_code == 404

    missing_scope = _request(
        _principal(service_id, kind="SERVICE", roles=("SERVICE",)),
        clean_database,
        stage8_execution_service=execution,
    )
    with pytest.raises(AuthError) as scope_denied:
        await server.stage8_ingest_execution_result(
            "EXEC-CALLBACK", callback_body, missing_scope
        )
    assert scope_denied.value.status_code == 403
    assert execution.ingested == [job_id]

    triage_request = _request(
        _principal(owner_id),
        clean_database,
        stage8_execution_service=execution,
    )
    triaged = await server.stage8_failure_triage(
        server.Stage8FailureTriageRunRequest(execution_id="EXEC-CALLBACK"),
        triage_request,
    )
    assert triaged["job_id"] == job_id
    assert execution.triaged == [job_id]


@pytest.mark.asyncio
async def test_process_ready_is_explicit_tenant_authorized_and_single_claim(clean_database):
    owner_id = await _user(clean_database)
    service_id = await _user(clean_database, kind="SERVICE")
    cross_service_id = await _user(
        clean_database, tenant_id=TENANT_B, kind="SERVICE"
    )
    mission = await MissionService(clean_database).create_mission(
        "feature-continuation",
        mission_id="mission-continuation",
        owner_user_id=owner_id,
        tenant_id=TENANT_A,
    )
    job_id = await _execution_job(
        clean_database, mission.mission_id, execution_id="EXEC-CONT"
    )
    async with clean_database.transaction() as session:
        session.add(
            Stage8TicketContinuationRow(
                continuation_id="continuation-a",
                mission_id=mission.mission_id,
                execution_job_id=job_id,
                triage_id="triage-a",
                ticket_draft_id="draft-a",
                approval_id="approval-a",
                tool_invocation_id="invocation-a",
                invocation_binding_digest="a" * 64,
                request_digest="b" * 64,
                request_snapshot={},
                state="READY",
            )
        )

    class _Continuation:
        calls: list[str] = []

        async def process_ready_once(self, continuation_id):
            self.calls.append(continuation_id)
            return {"continuation_id": continuation_id}

    continuation = _Continuation()
    body = server.Stage8ProcessContinuationRequest(
        continuation_id="continuation-a"
    )
    cross = _request(
        _principal(
            cross_service_id,
            tenant_id=TENANT_B,
            kind="SERVICE",
            roles=("SERVICE",),
            scopes=(server.STAGE8_PROCESS_SCOPE,),
        ),
        clean_database,
        stage8_ticket_continuation_service=continuation,
    )
    with pytest.raises(AuthError) as denied:
        await server.stage8_process_ticket_continuation(body, cross)
    assert denied.value.status_code == 404
    assert continuation.calls == []

    allowed = _request(
        _principal(
            service_id,
            kind="SERVICE",
            roles=("SERVICE",),
            scopes=(server.STAGE8_PROCESS_SCOPE,),
        ),
        clean_database,
        stage8_ticket_continuation_service=continuation,
    )
    result = await server.stage8_process_ticket_continuation(body, allowed)
    assert result["continuation_id"] == "continuation-a"
    assert continuation.calls == ["continuation-a"]


@pytest.mark.asyncio
async def test_mission_and_ownership_rollback_together(clean_database):
    owner_id = await _user(clean_database)
    await _user(clean_database, tenant_id=TENANT_B)
    with pytest.raises(PersistenceError):
        await MissionService(clean_database).create_mission(
            "feature-rollback",
            mission_id="mission-rollback",
            owner_user_id=owner_id,
            tenant_id=TENANT_B,
        )
    async with clean_database.session() as session:
        assert await session.get(FeatureTestMissionRow, "mission-rollback") is None
