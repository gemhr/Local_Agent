"""业务事务、访问范围与报告封存；不执行 Runtime。"""
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select, func
from core.ci_simulation import service as simulation
from core.ci_simulation.models import CIExecutionRow, ScenarioInstanceRow
from core.incidents.models import IncidentRow, InvestigationRow, IncidentActionRow, IncidentVerificationRow
from core.incidents.contracts import CIError
from core.persistence.models import ObjectOwnershipRow

AGENT_ID = "ci_incident_investigator"
AGENT_VERSION = "1.0.0"


def evidence_catalog(execution):
    return [{k: e[k] for k in ("evidence_id", "kind", "availability", "media_type", "digest")} for e in execution.evidence_manifest]


def require_human(principal):
    if principal.principal_kind != "HUMAN":
        raise CIError("CI_HUMAN_REQUIRED", 403)


class IncidentService:
    def __init__(self, database):
        self.database = database

    async def create_scenario(self, principal, key, request_id):
        async with self.database.transaction() as session:
            return await simulation.create_instance(session, principal, key, request_id)

    async def execute(self, principal, instance_id, request_id, **options):
        async with self.database.transaction() as session:
            execution, created = await simulation.execute_instance(session, principal, instance_id, request_id, **options)
            incident = (await session.execute(select(IncidentRow).where(IncidentRow.execution_id == execution.execution_id))).scalar_one_or_none()
            if created and execution.outcome == "FAIL":
                incident = IncidentRow(incident_id=str(uuid4()), execution_id=execution.execution_id,
                    tenant_id=principal.tenant_id, owner_user_id=principal.user_id, title="CI 执行失败 · " + execution.case_id,
                    status="OPEN", session_id=str(uuid4()), created_at=execution.completed_at)
                session.add(incident)
                await session.flush()
            return execution, incident, created

    async def owned_incident(self, session, principal, incident_id, *, lock=False):
        query = select(IncidentRow).where(IncidentRow.incident_id == incident_id,
            IncidentRow.tenant_id == principal.tenant_id, IncidentRow.owner_user_id == principal.user_id)
        row = (await session.execute(query.with_for_update().execution_options(populate_existing=True) if lock else query)).scalar_one_or_none()
        if row is None:
            raise CIError("CI_NOT_FOUND", 404)
        return row

    async def resolution_scope(self, session, principal, incident_id, *, lock=False):
        """人工操作统一按 instance → incident 顺序加锁。"""
        require_human(principal)
        incident = await self.owned_incident(session, principal, incident_id)
        origin = await session.get(CIExecutionRow, incident.execution_id)
        query = select(ScenarioInstanceRow).where(ScenarioInstanceRow.instance_id == origin.instance_id,
            ScenarioInstanceRow.owner_user_id == principal.user_id, ScenarioInstanceRow.tenant_id == principal.tenant_id)
        instance = (await session.execute(query.with_for_update() if lock else query)).scalar_one_or_none()
        if instance is None:
            raise CIError("CI_NOT_FOUND", 404)
        if lock:
            incident = await self.owned_incident(session, principal, incident_id, lock=True)
        return incident, origin, instance

    async def list_incidents(self, principal, limit):
        async with self.database.session() as session:
            rows = (await session.execute(select(IncidentRow).where(IncidentRow.tenant_id == principal.tenant_id,
                IncidentRow.owner_user_id == principal.user_id).order_by(IncidentRow.created_at.desc(), IncidentRow.incident_id).limit(limit))).scalars().all()
            result = []
            for row in rows:
                latest = (await session.execute(select(InvestigationRow.investigation_id).where(InvestigationRow.incident_id == row.incident_id)
                    .order_by(InvestigationRow.report_version.desc()).limit(1))).scalar_one_or_none()
                result.append(dict(incident_id=row.incident_id, title=row.title, status=row.status, execution_id=row.execution_id,
                    outcome="FAIL", created_at=row.created_at, latest_investigation_id=latest))
            return result

    async def detail(self, principal, incident_id):
        async with self.database.session() as session:
            row, execution, instance = await self.resolution_scope(session, principal, incident_id)
            investigations = (await session.execute(select(InvestigationRow).where(InvestigationRow.incident_id == incident_id)
                .order_by(InvestigationRow.report_version.desc()))).scalars().all()
            from core.incidents.operations import action_payload
            from core.incidents.verification import closure_eligibility, verification_payload
            actions = (await session.execute(select(IncidentActionRow).where(IncidentActionRow.incident_id == incident_id)
                .order_by(IncidentActionRow.created_at.desc(), IncidentActionRow.action_id))).scalars().all()
            verifications = (await session.execute(select(IncidentVerificationRow).where(IncidentVerificationRow.incident_id == incident_id)
                .order_by(IncidentVerificationRow.completed_at.desc(), IncidentVerificationRow.verification_id))).scalars().all()
            return dict(incident_id=row.incident_id, title=row.title, status=row.status, created_at=row.created_at,
                instance_id=instance.instance_id, state_revision=instance.state_revision,
                closed_at=row.closed_at, closed_by_user_id=row.closed_by_user_id, closure=row.closure_payload,
                actions=[action_payload(a, instance.state_revision) for a in actions],
                verifications=[await verification_payload(session, v, execution) for v in verifications],
                closure_eligibility=await closure_eligibility(session, row, execution, instance),
                execution=execution.summary, evidence=evidence_catalog(execution),
                investigations=[dict(investigation_id=i.investigation_id, run_id=i.run_id, report_version=i.report_version,
                    projection_status=i.projection_status) for i in investigations])

    async def related_execution(self, session, principal, incident_id, execution_id):
        incident, origin, instance = await self.resolution_scope(session, principal, incident_id)
        row = await session.get(CIExecutionRow, execution_id)
        if row is None or row.instance_id != instance.instance_id:
            raise CIError("CI_NOT_FOUND", 404)
        if execution_id != origin.execution_id:
            batches = (await session.execute(select(IncidentVerificationRow).where(IncidentVerificationRow.incident_id == incident_id))).scalars().all()
            if not any(item["execution_id"] == execution_id for batch in batches for item in batch.items):
                raise CIError("CI_NOT_FOUND", 404)
        return row

    async def execution_detail(self, principal, incident_id, execution_id, evidence_id=None):
        async with self.database.session() as session:
            row = await self.related_execution(session, principal, incident_id, execution_id)
            if evidence_id is not None:
                return self.evidence(row, incident_id, evidence_id)
            return dict(execution_id=row.execution_id, state_revision=row.state_revision,
                summary=row.summary, evidence=evidence_catalog(row))

    async def read_evidence(self, principal, incident_id, evidence_id):
        async with self.database.session() as session:
            row = await self.owned_incident(session, principal, incident_id)
            execution = await session.get(CIExecutionRow, row.execution_id)
            return self.evidence(execution, incident_id, evidence_id)

    @staticmethod
    def evidence(execution, incident_id, evidence_id):
        value = next((e for e in execution.evidence_manifest if e["evidence_id"] == evidence_id), None)
        if value is None:
            raise CIError("CI_EVIDENCE_SCOPE_DENIED", 404)
        return dict(incident_id=incident_id, **value)

    async def admit(self, principal, incident_id, request_id):
        async with self.database.transaction() as session:
            incident = await self.owned_incident(session, principal, incident_id, lock=True)
            old = (await session.execute(select(InvestigationRow).where(InvestigationRow.incident_id == incident_id,
                InvestigationRow.request_id == request_id))).scalar_one_or_none()
            if old:
                return old, False
            version = (await session.execute(select(func.max(InvestigationRow.report_version)).where(InvestigationRow.incident_id == incident_id))).scalar() or 0
            row = InvestigationRow(investigation_id=str(uuid4()), incident_id=incident_id, request_id=request_id,
                report_version=version + 1, run_id=str(uuid4()), session_id=incident.session_id, agent_id=AGENT_ID,
                agent_version=AGENT_VERSION, created_at=datetime.now(timezone.utc), projection_status="PENDING")
            session.add(row)
            await session.flush()
            return row, True

    async def investigation(self, principal, incident_id, investigation_id):
        async with self.database.session() as session:
            await self.owned_incident(session, principal, incident_id)
            row = await session.get(InvestigationRow, investigation_id)
            if row is None or row.incident_id != incident_id:
                raise CIError("CI_NOT_FOUND", 404)
            return row

    async def bound_execution(self, run_context, evidence_id=None):
        async with self.database.session() as session:
            row = (await session.execute(select(InvestigationRow).where(InvestigationRow.run_id == run_context.run_id))).scalar_one_or_none()
            if row is None:
                raise CIError("CI_EVIDENCE_SCOPE_DENIED", 404)
            incident = await session.get(IncidentRow, row.incident_id)
            owner = await session.get(ObjectOwnershipRow, ("RUN", row.run_id))
            access = run_context.memory_access_context
            if (owner is None or owner.owner_user_id != incident.owner_user_id or owner.tenant_id != incident.tenant_id
                    or row.agent_id != run_context.data.entry_agent_id or row.session_id != run_context.session_id
                    or access is None or access.tenant_id != incident.tenant_id or access.user_id != str(incident.owner_user_id)
                    or run_context.data.agent_version != row.agent_version):
                raise CIError("CI_EVIDENCE_SCOPE_DENIED", 404)
            execution = await session.get(CIExecutionRow, incident.execution_id)
            if evidence_id is not None:
                return self.evidence(execution, incident.incident_id, evidence_id)
            return dict(incident_id=incident.incident_id, execution_id=execution.execution_id,
                environment_id=execution.environment_id, case_id=execution.case_id, outcome=execution.outcome,
                completed_at=execution.completed_at.isoformat(), evidence=evidence_catalog(execution))

    async def seal(self, investigation_id, report, result_digest, error=None):
        async with self.database.transaction() as session:
            row = await session.get(InvestigationRow, investigation_id, with_for_update=True)
            if row.projection_status == "PENDING":
                row.projection_status = "FAILED" if error else "READY"
                row.report_payload, row.runtime_result_digest, row.safe_error_code = report, result_digest, error
                row.projected_at = datetime.now(timezone.utc)
            return row
