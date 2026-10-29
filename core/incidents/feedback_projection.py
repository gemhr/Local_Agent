"""反馈安全投影：不输出人工正文、工具正文或任意 Memory。"""
from sqlalchemy import select

from core.incidents.models import InvestigationFeedbackRow, InvestigationRow
from core.incidents.contracts import CIError
from core.incidents.service import require_human
from core.incidents.attribution import diagnose
from core.ci_simulation.models import CIExecutionRow
from core.persistence.models import ObjectOwnershipRow, RuntimeLoopActionRow
from core.persistence.repositories.execution import DurableExecutionRepository
from core.runtime.run_control import DurableRunControlService
from core.runtime.snapshot_serialization import sha256_digest
from core.runtime.agent_loop.quality_contract import catalog_digest
from core.agent_platform.ci_diagnosis import validate_report

CONTRACT = 'ci-feedback-projection.v1'


async def project(service, principal, incident_id, feedback_id):
    require_human(principal)
    # 与反馈写入沿用相同 incident 锁，避免解析修订链期间出现新后继。
    async with service.database.transaction() as session:
        incident = await service.owned_incident(session, principal, incident_id, lock=True)
        row = await session.get(InvestigationFeedbackRow, feedback_id)
        if (row is None or row.incident_id != incident_id or row.tenant_id != principal.tenant_id
                or row.created_by_user_id != principal.user_id):
            raise CIError('CI_NOT_FOUND', 404)
        successor = (await session.execute(select(InvestigationFeedbackRow.feedback_id).where(
            InvestigationFeedbackRow.supersedes == feedback_id))).scalar_one_or_none()
        state = 'SUPERSEDED' if successor else 'WITHDRAWN' if row.request_payload['decision'] == 'WITHDRAW' else 'ACTIVE'
        investigation = await session.get(InvestigationRow, row.investigation_id)
        execution = await session.get(CIExecutionRow, incident.execution_id)
        if (investigation is None or execution is None or investigation.incident_id != incident_id
                or investigation.projection_status != 'READY'
                or investigation.projected_at is None
                or investigation.report_version != row.binding['report_version']
                or sha256_digest(investigation.report_payload) != row.binding['report_digest']
                or investigation.runtime_result_digest != row.binding['runtime_result_digest']
                or investigation.run_id != row.binding['run_id']
                or execution.execution_id != row.binding['execution_id']
                or execution.evidence_digest != row.binding['evidence_digest']):
            raise CIError('CI_FEEDBACK_SOURCE_INVALID', 409)
        owner = await session.get(ObjectOwnershipRow, ('RUN', investigation.run_id))
        if owner is None or owner.tenant_id != principal.tenant_id or owner.owner_user_id != principal.user_id:
            raise CIError('CI_NOT_FOUND', 404)
        repo = DurableExecutionRepository(service.database, DurableRunControlService(service.database))
        image = await repo.load(investigation.run_id, session=session)
        if (image is None or image.root.status != 'SUCCEEDED'
                or image.root.resume_input.get('resolved_agent_id') != investigation.agent_id
                or image.root.resume_input.get('agent_version') != investigation.agent_version
                or image.root.resume_input.get('session_id') != investigation.session_id):
            raise CIError('CI_FEEDBACK_RUN_BINDING_INVALID', 409)
        actions = (await session.execute(select(RuntimeLoopActionRow).where(
            RuntimeLoopActionRow.run_id == investigation.run_id))).scalars().all()
        steps = [s for s in image.steps if s.status == 'SUCCEEDED']
        raw = (steps[0].typed_result_payload or {}).get('content', '') if len(steps) == 1 else ''
        proven = False
        try:
            from core.runtime.snapshot_serialization import text_digest
            proven = (text_digest(raw) == row.binding['runtime_result_digest']
                and validate_report(raw, investigation, incident, execution, image, actions) == investigation.report_payload)
        except CIError as exc:
            # validate_report 已完成实际读取与 Final 校验，最后才检查强结论必需证据。
            proven = exc.code == 'CI_REPORT_UNSUPPORTED'
        except (ValueError, TypeError, KeyError, StopIteration):
            # 不恢复或伪造缺失执行事实；只将该类证据标为 unavailable。
            proven = False
        report = investigation.report_payload
        evidence = {e['evidence_id']: e for e in execution.evidence_manifest}
        kinds = {evidence.get(ref.get('evidence_id'), {}).get('kind') for ref in report.get('supporting_evidence', [])}
        required = {'PARSER_LOG', 'ENVIRONMENT_RECORD', 'CASE_REQUIREMENTS'} <= kinds
        attribution = diagnose(row.request_payload['decision'], report, report_proven=proven,
            required_evidence_proven=required, capture_available=False)
        # 当前普通调查没有通用 quality receipt 的实际请求 capture 合同；明确缺失。
        observations = dict(tool_and_final_binding='AVAILABLE' if proven else 'UNAVAILABLE',
            context_capture='UNAVAILABLE', required_evidence='AVAILABLE' if proven and required else 'UNAVAILABLE')
        body = dict(contract_version=CONTRACT, source_feedback_id=row.feedback_id, feedback_revision_id=row.feedback_id,
            incident_id=incident_id, tenant_id=principal.tenant_id, owner_user_id=str(principal.user_id),
            investigation_id=row.investigation_id, root_run_id=investigation.run_id,
            report_version=investigation.report_version, report_digest=row.binding['report_digest'],
            runtime_result_digest=row.binding['runtime_result_digest'],
            evidence_bundle_digest=catalog_digest(observations), feedback_decision=row.request_payload['decision'],
            feedback_current_status=state, source_type='SYNTHETIC', source_trust='HUMAN',
            reviewed_at=row.created_at.isoformat(), observations=observations, attribution=attribution)
        return dict(projection=body, projection_digest=catalog_digest(body))
