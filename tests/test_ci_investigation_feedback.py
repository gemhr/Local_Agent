"""WP1 反馈合同的定向 PostgreSQL 回归。"""
from datetime import datetime, timezone
from dataclasses import replace
from uuid import UUID, uuid4
import pytest
from sqlalchemy import text
from core.persistence.errors import PersistenceError
from core.incidents import feedback
from core.incidents.contracts import CIError
from core.persistence.models import ObjectOwnershipRow
from core.persistence.repositories.execution import DurableExecutionRepository
from core.runtime.run_control import DurableRunControlService
from core.runtime.execution_aggregate import ExecutionRootInput
from core.runtime.planning import create_single_step_plan
from core.runtime.planning import TaskCapabilityRequirements
from core.runtime.events import RuntimeEvent, RuntimeEventType, RunCompletedPayload
from core.runtime.event_journal_store import PostgresRunEventJournal
from tests.test_ci_incident_resolution import setup_incident
from tests.test_ci_incident_diagnosis import user


async def sealed(service, principal, incident):
    row, _ = await service.admit(principal, incident.incident_id, str(uuid4()))
    control = DurableRunControlService(service.database)
    repo = DurableExecutionRepository(service.database, control)
    lease = await control.claim(row.run_id, 'feedback-test')
    await repo.initialize(ExecutionRootInput(run_id=row.run_id,
        resume_input=dict(resolved_agent_id=row.agent_id, agent_version=row.agent_version, session_id=row.session_id),
        plan=create_single_step_plan(row.agent_id, TaskCapabilityRequirements()), absolute_deadline=None,
        budget_totals={}, budget_reserved={}, budget_consumed={}), lease=lease)
    # 通过 Runtime Owner 封存合成测试 Run；不直接修改持久化状态。
    event = RuntimeEvent(schema_version=RuntimeEvent.CURRENT_SCHEMA_VERSION, event_id=str(uuid4()),
        run_id=row.run_id, trace_id=row.run_id, sequence=1, event_type=RuntimeEventType.RUN_COMPLETED,
        emitted_at=datetime.now(timezone.utc), component='test', payload=RunCompletedPayload(status='SUCCEEDED', stop_reason='COMPLETED'))
    await repo.finalize_terminal(lease, event=event, journal=PostgresRunEventJournal(service.database))
    async with service.database.transaction() as session:
        session.add(ObjectOwnershipRow(object_type='RUN', object_id=row.run_id, owner_user_id=principal.user_id, tenant_id=principal.tenant_id))
    return await service.seal(row.investigation_id, {'supporting_evidence': [], 'failure_stage': 'UNKNOWN'}, 'a' * 64)


def intent(row, **kwargs):
    return feedback.FeedbackIntent(request_id=uuid4(), investigation_id=row.investigation_id,
        report_version=row.report_version, decision='ACCEPT', **kwargs)


@pytest.mark.asyncio
async def test_exact_version_and_authorization(clean_database):
    service, principal, _, _, incident = await setup_incident(clean_database)
    first = await sealed(service, principal, incident)
    second = await sealed(service, principal, incident)
    pending, _ = await service.admit(principal, incident.incident_id, str(uuid4()))
    body = intent(first)
    saved, _ = await feedback.submit(service, principal, incident.incident_id, body)
    assert saved['binding']['run_id'] == first.run_id != second.run_id
    other = await user(clean_database)
    _, _, _, _, other_incident = await setup_incident(clean_database, principal=principal)
    for who, iid, request in [(other, incident.incident_id, body),
            (replace(principal, tenant_id='other'), incident.incident_id, body),
            (principal, other_incident.incident_id, body),
            (principal, incident.incident_id, body.model_copy(update={'report_version': second.report_version})),
            (principal, incident.incident_id, intent(pending)),
            (principal, incident.incident_id, body.model_copy(update={'investigation_id': str(uuid4())}))]:
        with pytest.raises(CIError):
            await feedback.submit(service, who, iid, request)
    with pytest.raises(CIError):
        await feedback.history(service, other, incident.incident_id)


@pytest.mark.asyncio
async def test_idempotency_and_conflict(clean_database):
    import asyncio
    service, principal, _, _, incident = await setup_incident(clean_database)
    row = await sealed(service, principal, incident)
    body = intent(row)
    results = await asyncio.gather(*(feedback.submit(service, principal, incident.incident_id, body) for _ in range(3)))
    assert len({item[0]['feedback_id'] for item in results}) == 1
    assert sum(item[1] for item in results) == 1
    with pytest.raises(CIError, match='CI_FEEDBACK_IDEMPOTENCY_CONFLICT'):
        await feedback.submit(service, principal, incident.incident_id, body.model_copy(update={'reason': '不同内容'}))


@pytest.mark.asyncio
async def test_revision_withdrawal_and_immutable_history(clean_database):
    service, principal, _, _, incident = await setup_incident(clean_database)
    row = await sealed(service, principal, incident)
    first, _ = await feedback.submit(service, principal, incident.incident_id, intent(row))
    revision = intent(row).model_copy(update={'decision': 'REJECT', 'supersedes': UUID(first['feedback_id'])})
    second, _ = await feedback.submit(service, principal, incident.incident_id, revision)
    withdrawal = intent(row).model_copy(update={'decision': 'WITHDRAW', 'supersedes': UUID(second['feedback_id'])})
    await feedback.submit(service, principal, incident.incident_id, withdrawal)
    history = (await feedback.history(service, principal, incident.incident_id))['items']
    assert len(history) == 3 and history[0] == first
    assert all(item['binding']['source'] == 'SYNTHETIC' and item['binding']['verification'] == 'UNKNOWN'
        and item['binding']['review'] == 'HUMAN' for item in history)
    with pytest.raises(CIError):
        await feedback.submit(service, principal, incident.incident_id, revision.model_copy(update={'request_id': uuid4()}))
    with pytest.raises(PersistenceError):
        async with clean_database.transaction() as session:
            await session.execute(text('DELETE FROM incident_investigation_feedback WHERE feedback_id = :id'), {'id': first['feedback_id']})



@pytest.mark.asyncio
async def test_http_contract_and_revision_scope(clean_database):
    import httpx
    from fastapi import FastAPI, Request
    from core.incidents.http import router
    service, principal, _, _, incident = await setup_incident(clean_database)
    first = await sealed(service, principal, incident)
    second = await sealed(service, principal, incident)
    app = FastAPI()
    app.state.incident_service = service
    @app.middleware('http')
    async def identity(request: Request, call_next):
        if request.headers.get('X-Test-Human'):
            request.state.principal = principal
        return await call_next(request)
    app.include_router(router)
    path = f'/api/ci/incidents/{incident.incident_id}/feedback'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        body = intent(first).model_dump(mode='json')
        assert (await client.post(path, json=body)).status_code == 403
        headers = {'X-Test-Human': '1'}
        result = await client.post(path, json=body, headers=headers)
        assert result.status_code == 201
        assert (await client.post(path, json=body, headers=headers)).json() == result.json()
        invalid = intent(second).model_dump(mode='json') | {'supersedes': result.json()['feedback_id']}
        assert (await client.post(path, json=invalid, headers=headers)).status_code == 404
        assert (await client.post(path, json=body | {'root_run_id': 'forged'}, headers=headers)).status_code == 422
        assert (await client.post(path, json=body | {'decision': 'WITHDRAW', 'supersedes': None}, headers=headers)).status_code == 422
        assert len((await client.get(path, headers=headers)).json()['items']) == 1
