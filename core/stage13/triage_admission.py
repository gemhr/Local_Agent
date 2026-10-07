"""WP04A 显式真实主体准入，复用不可变 Revision 和原 hard60。"""

from datetime import timedelta
import json
from uuid import UUID, uuid4, uuid5
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from core.stage13.incident_models import (
    AnalysisJobRow,
    BudgetRow,
    IncidentRow,
    RevisionRow,
)
from core.stage13.triage_models import JobExecutionRow, SubjectRow
from core.stage13.contracts import business_key, canonical_bytes, sha256
from core.stage13.triage_subject import check_manifest


class RealSubjectAdmissionService:
    def __init__(self, database, scope, manifests):
        self.database, self.scope, self.manifests = database, scope, manifests

    async def register_subjects(self):
        async with self.database.transaction() as session:
            for manifest in self.manifests.values():
                digest = check_manifest(manifest, manifest)
                await session.execute(
                    insert(SubjectRow)
                    .values(
                        subject_digest=digest,
                        subject_id=manifest["subject_id"],
                        subject_version=manifest["subject_version"],
                        manifest=manifest,
                    )
                    .on_conflict_do_nothing()
                )
                row = await session.get(SubjectRow, digest)
                if row is None or row.manifest != manifest:
                    raise ValueError("SUBJECT_VERSION_CONFLICT")

    async def admit_for_subject(
        self, incident_id, evidence_revision, expected_subject_manifest, lane="NIGHTLY"
    ):
        manifest = expected_subject_manifest
        registered = (
            self.manifests.get(manifest.get("agent_id"))
            if isinstance(manifest, dict)
            else None
        )
        digest = check_manifest(manifest, registered)
        if lane != "NIGHTLY":
            raise ValueError("CONTRACT_PROBE_USE_RUN_HARNESS")
        async with self.database.transaction() as session:
            incident = await session.get(IncidentRow, incident_id, with_for_update=True)
            if incident is None or incident.scope != self.scope:
                raise ValueError("INCIDENT_SCOPE_DENIED")
            revision = await session.get(RevisionRow, (incident_id, evidence_revision))
            if (
                revision is None
                or sha256(canonical_bytes(revision.manifest))
                != revision.manifest_digest
            ):
                raise ValueError("REVISION_PROVENANCE_INVALID")
            akey = business_key("analysis", incident_id, evidence_revision, digest)
            existing = (
                await session.execute(
                    select(AnalysisJobRow).where(AnalysisJobRow.admission_key == akey)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing.job_id
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            if now < incident.first_seen_at + timedelta(seconds=600):
                raise ValueError("COALESCING_WINDOW")
            jobs = (
                (
                    await session.execute(
                        select(AnalysisJobRow)
                        .where(
                            AnalysisJobRow.incident_id == incident_id,
                            AnalysisJobRow.subject_digest == digest,
                            AnalysisJobRow.admitted_at.is_not(None),
                        )
                        .order_by(AnalysisJobRow.admitted_at)
                    )
                )
                .scalars()
                .all()
            )
            if len(jobs) >= 2 or (
                jobs
                and (
                    now < jobs[-1].admitted_at + timedelta(seconds=1800)
                    or evidence_revision <= jobs[-1].revision
                )
            ):
                raise ValueError("REANALYSIS_NOT_ELIGIBLE")
            bkey = business_key(
                "analysis-budget",
                self.scope,
                incident.project,
                incident.suite,
                incident.business_date,
                digest,
                lane,
            )
            await session.execute(
                insert(BudgetRow)
                .values(
                    budget_key=bkey,
                    scope=self.scope,
                    project=incident.project,
                    suite=incident.suite,
                    business_date=incident.business_date,
                    subject_digest=digest,
                    lane=lane,
                    admitted_total=0,
                )
                .on_conflict_do_nothing()
            )
            budget = await session.get(BudgetRow, bkey, with_for_update=True)
            if budget.admitted_total >= 60:
                raise ValueError("DEFERRED_BUDGET")
            payload = json.loads(canonical_bytes(revision.manifest))
            payload["incident_ref"] = {
                "incident_id": incident_id,
                "evidence_revision": evidence_revision,
                "manifest_digest": revision.manifest_digest,
            }
            payload["evidence_policy"]["deadline_at"] = (
                now + timedelta(seconds=180)
            ).isoformat()
            encoded = canonical_bytes(payload)
            if (
                len(encoded) > 128 * 1024
                or len(payload["visible_evidence"]) > 32
                or len(payload["environment_samples"]) > 8
            ):
                raise ValueError("INPUT_BUDGET_EXCEEDED")
            job_id = str(uuid4())
            session.add(
                AnalysisJobRow(
                    job_id=job_id,
                    admission_key=akey,
                    incident_id=incident_id,
                    revision=evidence_revision,
                    subject_digest=digest,
                    subject_manifest=manifest,
                    budget_key=bkey,
                    status="READY",
                    kind="REANALYSIS" if jobs else "INITIAL",
                    admitted_at=now,
                    input=payload,
                    input_digest=sha256(encoded),
                )
            )
            await session.flush()
            session.add(
                JobExecutionRow(
                    job_id=job_id,
                    initial_run_id=str(uuid5(UUID(job_id), "INITIAL")),
                    epoch=0,
                )
            )
            budget.admitted_total += 1
            budget.cutoff = {
                "first_seen_at": incident.first_seen_at.isoformat(),
                "incident_key": incident.incident_key,
            }
            return job_id
