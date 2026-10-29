"""指定封存调查的人工评价；不产生业务根因真值。"""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import select

from core.incidents.contracts import Intent, CIError
from core.incidents.models import InvestigationRow, InvestigationFeedbackRow
from core.incidents.service import require_human
from core.ci_simulation.models import CIExecutionRow
from core.persistence.models import ObjectOwnershipRow, RuntimeRunExecutionRow, UserRow
from core.runtime.snapshot_serialization import sha256_digest


class FeedbackIntent(Intent):
    investigation_id: str = Field(min_length=1, max_length=128)
    report_version: int = Field(gt=0, strict=True)
    decision: Literal['ACCEPT', 'REJECT', 'NEEDS_CLARIFICATION', 'WITHDRAW']
    reason: str = Field(default='', max_length=2000)
    correction: str = Field(default='', max_length=2000)
    supersedes: UUID | None = None

    @model_validator(mode='after')
    def withdrawal_target(self):
        if self.decision == 'WITHDRAW' and self.supersedes is None:
            raise ValueError('CI_FEEDBACK_SUPERSEDES_REQUIRED')
        return self


def payload(row):
    return dict(feedback_id=row.feedback_id, incident_id=row.incident_id,
        investigation_id=row.investigation_id, created_by_user_id=str(row.created_by_user_id),
        created_at=row.created_at, request=row.request_payload, binding=row.binding,
        supersedes=row.supersedes)


async def submit(service, principal, incident_id, body):
    require_human(principal)
    normalized = dict(incident_id=incident_id, **body.model_dump(mode='json', exclude={'request_id'}))
    async with service.database.transaction() as session:
        # 同主体请求串行化，覆盖跨 Incident 的幂等键冲突。
        await session.get(UserRow, principal.user_id, with_for_update=True)
        incident = await service.owned_incident(session, principal, incident_id, lock=True)
        old = (await session.execute(select(InvestigationFeedbackRow).where(
            InvestigationFeedbackRow.created_by_user_id == principal.user_id,
            InvestigationFeedbackRow.tenant_id == principal.tenant_id,
            InvestigationFeedbackRow.request_id == str(body.request_id)))).scalar_one_or_none()
        if old:
            if old.request_payload != normalized:
                raise CIError('CI_FEEDBACK_IDEMPOTENCY_CONFLICT', 409)
            return payload(old), False
        investigation = await session.get(InvestigationRow, body.investigation_id)
        if investigation is None or investigation.incident_id != incident_id:
            raise CIError('CI_NOT_FOUND', 404)
        if (investigation.report_version != body.report_version or investigation.projection_status != 'READY'
                or investigation.projected_at is None or investigation.report_payload is None
                or not investigation.runtime_result_digest):
            raise CIError('CI_FEEDBACK_REPORT_NOT_SEALED_OR_VERSION_MISMATCH', 409)
        owner = await session.get(ObjectOwnershipRow, ('RUN', investigation.run_id))
        root = await session.get(RuntimeRunExecutionRow, investigation.run_id)
        identity = root.resume_input if root else {}
        if (owner is None or owner.owner_user_id != principal.user_id or owner.tenant_id != principal.tenant_id
                or root is None or root.status != 'SUCCEEDED'
                or identity.get('resolved_agent_id') != investigation.agent_id
                or identity.get('agent_version') != investigation.agent_version
                or identity.get('session_id') != investigation.session_id):
            raise CIError('CI_FEEDBACK_RUN_BINDING_INVALID', 409)
        if body.supersedes:
            target = await session.get(InvestigationFeedbackRow, str(body.supersedes))
            if (target is None or target.tenant_id != principal.tenant_id
                    or target.created_by_user_id != principal.user_id or target.incident_id != incident_id
                    or target.investigation_id != investigation.investigation_id):
                raise CIError('CI_NOT_FOUND', 404)
            successor = (await session.execute(select(InvestigationFeedbackRow.feedback_id).where(
                InvestigationFeedbackRow.supersedes == target.feedback_id))).scalar_one_or_none()
            if successor:
                raise CIError('CI_FEEDBACK_ALREADY_SUPERSEDED', 409)
        # 此领域的唯一入口为受控 ci_simulation；不接受客户端声明来源或可信度。
        execution = await session.get(CIExecutionRow, incident.execution_id)
        binding = dict(run_id=investigation.run_id, session_id=investigation.session_id,
            agent_id=investigation.agent_id, agent_version=investigation.agent_version,
            report_version=investigation.report_version, report_digest=sha256_digest(investigation.report_payload),
            runtime_result_digest=investigation.runtime_result_digest, execution_id=execution.execution_id,
            evidence_digest=execution.evidence_digest, evidence_refs=investigation.report_payload.get('supporting_evidence', []),
            source='SYNTHETIC', review='HUMAN', verification='UNKNOWN', digest_algorithm='localagent-canonical-json.v1')
        row = InvestigationFeedbackRow(feedback_id=str(uuid4()), incident_id=incident_id,
            investigation_id=investigation.investigation_id, tenant_id=principal.tenant_id,
            created_by_user_id=principal.user_id, request_id=str(body.request_id), request_payload=normalized,
            binding=binding, supersedes=str(body.supersedes) if body.supersedes else None,
            created_at=datetime.now(timezone.utc))
        session.add(row)
        await session.flush()
        return payload(row), True


async def history(service, principal, incident_id):
    require_human(principal)
    async with service.database.session() as session:
        await service.owned_incident(session, principal, incident_id)
        rows = (await session.execute(select(InvestigationFeedbackRow).where(
            InvestigationFeedbackRow.incident_id == incident_id,
            InvestigationFeedbackRow.tenant_id == principal.tenant_id,
            InvestigationFeedbackRow.created_by_user_id == principal.user_id)
            .order_by(InvestigationFeedbackRow.created_at, InvestigationFeedbackRow.feedback_id))).scalars().all()
        return {'items': [payload(row) for row in rows]}
