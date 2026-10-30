"""CI Owner 的合成准备；仅本地事务，不 claim 或执行 Runtime。"""
from datetime import datetime, timezone
from uuid import uuid4, uuid5, NAMESPACE_URL
from sqlalchemy import select, func
from core.incidents.models import IncidentRow, InvestigationRow, SyntheticEvaluationBindingRow as Binding
from core.persistence.models import UserRow, RuntimeAgentEvaluationReceiptRow
from core.persistence.repositories.agent_quality import AgentQualityReceiptRepository
from core.ci_simulation import service as simulation
from core.ci_simulation.execution import initial_state
from core.runtime.agent_loop.quality_contract import catalog_digest

TEMPLATE = 'report_pipeline_failure'


def frozen_input():
    return dict(template_id=TEMPLATE, template_version='2', input_digest=catalog_digest(initial_state(TEMPLATE)))


async def prepare(service, principal, request, definition, manifest, profile, *, request_counting_exception=None):
    quality = request.quality
    run = str(quality.run_id)
    normalized = request.model_dump(mode='json')
    fingerprint = catalog_digest(normalized)
    repository = AgentQualityReceiptRepository(service.database)
    async with service.database.transaction() as session:
        await session.get(UserRow, principal.user_id, with_for_update=True)
        old = await session.get(Binding, run)
        if old:
            if (old.tenant_id != principal.tenant_id or old.principal_id != principal.user_id
                    or old.project_id != str(quality.evaluation_project_id) or old.fingerprint != fingerprint):
                raise ValueError('CI_SYNTHETIC_REQUEST_CONFLICT')
            return False, old.payload
        if await session.get(RuntimeAgentEvaluationReceiptRow, run) is not None:
            raise ValueError('CI_SYNTHETIC_REQUEST_CONFLICT')
        # User 锁使同 principal 的配额与准备串行；同模式仅一次新准入。
        rows = (await session.execute(select(Binding).where(Binding.tenant_id==principal.tenant_id,
            Binding.principal_id==principal.user_id, Binding.project_id==str(quality.evaluation_project_id)))).scalars().all()
        if any(r.payload['mode']==request.mode for r in rows):
            raise ValueError('CI_SYNTHETIC_ATTEMPT_LIMIT')
        winner, _ = await repository.admit_in_transaction(session, correlation=quality.correlation(),
            principal=principal, manifest=manifest, profile=profile)
        if not winner:
            raise ValueError('CI_SYNTHETIC_REQUEST_CONFLICT')
        instance,_ = await simulation.create_instance(session, principal, TEMPLATE, 'ci-evaluation:'+run)
        execution,_ = await simulation.execute_instance(session, principal, instance.instance_id, 'ci-evaluation:'+run)
        if execution.outcome != 'FAIL':
            raise ValueError('CI_SYNTHETIC_EXPECTED_FAIL')
        now = datetime.now(timezone.utc)
        session_id = str(uuid5(NAMESPACE_URL, 'agent-quality-session:'+run))
        incident = IncidentRow(incident_id=str(uuid4()),execution_id=execution.execution_id,
            tenant_id=principal.tenant_id,owner_user_id=principal.user_id,title='SYNTHETIC EVALUATION',
            status='OPEN',session_id=session_id,created_at=now)
        session.add(incident)
        await session.flush()
        investigation = InvestigationRow(investigation_id=str(uuid4()),incident_id=incident.incident_id,
            request_id=str(quality.request_id),report_version=1,run_id=run,session_id=session_id,
            agent_id=definition.agent_id,agent_version=definition.agent_version,created_at=now,projection_status='PENDING')
        session.add(investigation)
        await session.flush()
        binding = dict(contract_version=request.contract_version,source_type='SYNTHETIC',run_id=run,
            session_id=session_id,agent_id=definition.agent_id,agent_version=definition.agent_version,
            instance_id=instance.instance_id,execution_id=execution.execution_id,incident_id=incident.incident_id,
            investigation_id=investigation.investigation_id,report_version=1,
            frozen_ci=request.frozen_ci.model_dump(mode='json'),manifest_digest=request.candidate_manifest_digest,
            mode=request.mode,quality_correlation=quality.correlation())
        session.add(Binding(run_id=run,tenant_id=principal.tenant_id,principal_id=principal.user_id,
            project_id=str(quality.evaluation_project_id),fingerprint=fingerprint,payload=binding,
            investigation_id=investigation.investigation_id,instance_id=instance.instance_id,
            execution_id=execution.execution_id,incident_id=incident.incident_id))
        if request_counting_exception is not None:
            from core.runtime.request_counting_admission import match_request, issue_record
            trusted=match_request(request_counting_exception,request,principal,definition)
            receipt=await session.get(RuntimeAgentEvaluationReceiptRow,run)
            receipt.evaluation_capture={**receipt.evaluation_capture,
                'e2e_test_admission':issue_record(trusted,binding,receipt)}
        await session.flush()
        return True, binding
