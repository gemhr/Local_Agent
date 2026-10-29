"""反馈投影的真实 PG 绑定、安全白名单与有限归因。"""
import json
from dataclasses import replace
from uuid import UUID

import pytest
from core.incidents import feedback
from core.incidents.feedback_projection import project
from core.incidents.attribution import diagnose
from core.incidents.contracts import CIError
from core.runtime.agent_loop.quality_contract import catalog_digest
from tests.test_ci_investigation_feedback import sealed, intent
from tests.test_ci_incident_resolution import setup_incident


@pytest.mark.asyncio
async def test_projection_binding_safety_and_revision(clean_database):
    service, principal, _, _, incident = await setup_incident(clean_database)
    row = await sealed(service, principal, incident)
    saved, _ = await feedback.submit(service, principal, incident.incident_id,
        intent(row).model_copy(update={'decision': 'REJECT', 'reason': 'SECRET_REASON', 'correction': 'SECRET_CORRECTION'}))
    wire = await project(service, principal, incident.incident_id, saved['feedback_id'])
    body = wire['projection']
    assert wire['projection_digest'] == catalog_digest(body)
    assert body['root_run_id'] == row.run_id and body['report_digest'] == saved['binding']['report_digest']
    assert body['attribution']['observation'] == ['OBSERVED_OUTCOME_DISAGREEMENT']
    assert body['attribution']['verification_status'] == 'INCONCLUSIVE'
    assert body['attribution']['candidate_causes'] == []
    assert 'SECRET_' not in json.dumps(wire) and 'reason' not in body and 'correction' not in body
    with pytest.raises(CIError):
        await project(service, replace(principal, tenant_id='wrong'), incident.incident_id, saved['feedback_id'])
    await feedback.submit(service, principal, incident.incident_id,
        intent(row).model_copy(update={'decision': 'WITHDRAW', 'supersedes': UUID(saved['feedback_id'])}))
    assert (await project(service, principal, incident.incident_id, saved['feedback_id']))['projection']['feedback_current_status'] == 'SUPERSEDED'


def test_attribution_three_boundaries():
    disagreement = diagnose('REJECT', {}, report_proven=True, required_evidence_proven=True, capture_available=True)
    strong = diagnose('ACCEPT', {'diagnosis_status': 'SUPPORTED'}, report_proven=True, required_evidence_proven=False, capture_available=True)
    missing = diagnose('ACCEPT', {}, report_proven=False, required_evidence_proven=False, capture_available=False)
    assert disagreement['observation'] == ['OBSERVED_OUTCOME_DISAGREEMENT']
    assert strong['observation'] == ['STRONG_CONCLUSION_WITHOUT_REQUIRED_EVIDENCE']
    assert missing['observation'] == ['INCONCLUSIVE'] and missing['uncertainty']
    assert all(v['candidate_causes'] == [] and v['verification_status'] == 'INCONCLUSIVE' for v in (disagreement, strong, missing))


@pytest.mark.asyncio
async def test_projection_rejects_changed_binding_and_wrong_owner(clean_database):
    from core.incidents.models import InvestigationFeedbackRow
    from tests.test_ci_incident_diagnosis import user
    service, principal, _, _, incident = await setup_incident(clean_database)
    row = await sealed(service, principal, incident)
    saved, _ = await feedback.submit(service, principal, incident.incident_id, intent(row))
    other = await user(clean_database)
    with pytest.raises(CIError):
        await project(service, other, incident.incident_id, saved['feedback_id'])
    # 在 ORM 读取边界注入错误绑定，不修改不可变持久事实。
    original = service.database.transaction
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def corrupt():
        async with original() as session:
            record = await session.get(InvestigationFeedbackRow, saved['feedback_id'])
            record.binding = {**record.binding, 'report_digest': '0' * 64}
            with session.no_autoflush:
                yield session
            await session.rollback()
    service.database.transaction = corrupt
    with pytest.raises(CIError, match='CI_FEEDBACK_SOURCE_INVALID'):
        await project(service, principal, incident.incident_id, saved['feedback_id'])
