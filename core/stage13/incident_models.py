"""WP03 业务表；共享 Guardian metadata 以保留真实 VersionExecution 外键。"""

from datetime import datetime
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from core.stage13.guardian_models import GuardianBase


class CollectionRow(GuardianBase):
    __tablename__ = "stage13_failure_collections"
    version_id: Mapped[str] = mapped_column(
        ForeignKey("stage13_version_executions.version_execution_id"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    token: Mapped[str | None] = mapped_column(String(36))
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(String(64))
    pages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING','CLAIMED','COMPLETE','MISSING') AND attempts BETWEEN 0 AND 3",
            name="ck_s13_collection",
        ),
        Index(
            "ix_s13_collection_due",
            "next_available_at",
            postgresql_where=text("state IN ('PENDING','CLAIMED')"),
        ),
    )


class IncidentRow(GuardianBase):
    __tablename__ = "stage13_global_incidents"
    incident_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    incident_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    project: Mapped[str] = mapped_column(String(128), nullable=False)
    suite: Mapped[str] = mapped_column(String(128), nullable=False)
    business_date: Mapped[str] = mapped_column(String(10), nullable=False)
    normalizer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String, nullable=False)
    components: Mapped[list] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    changes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    material_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    draft: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    evidence_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_manifest_digest: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        CheckConstraint(
            "state IN ('OPEN','SEALED') AND evidence_revision >= 0",
            name="ck_s13_incident",
        ),
        Index(
            "ix_s13_incident_day",
            "scope",
            "project",
            "suite",
            "business_date",
            "first_seen_at",
            "incident_key",
        ),
    )


class ClusterRow(GuardianBase):
    __tablename__ = "stage13_local_failure_clusters"
    cluster_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cluster_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("stage13_version_executions.version_execution_id"),
        nullable=False,
        index=True,
    )
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("stage13_global_incidents.incident_id"), nullable=False, index=True
    )
    normalizer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String, nullable=False)
    components: Mapped[list] = mapped_column(JSONB, nullable=False)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
    __table_args__ = (
        UniqueConstraint("incident_id", "cluster_id", name="uq_s13_incident_cluster"),
    )


class MembershipRow(GuardianBase):
    __tablename__ = "stage13_cluster_memberships"
    version_id: Mapped[str] = mapped_column(
        ForeignKey("stage13_version_executions.version_execution_id"), primary_key=True
    )
    case_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    cluster_id: Mapped[str] = mapped_column(
        ForeignKey("stage13_local_failure_clusters.cluster_id"),
        nullable=False,
        index=True,
    )
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("stage13_global_incidents.incident_id"), nullable=False, index=True
    )
    environment: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(128), nullable=False)
    product_version: Mapped[str] = mapped_column(String(128), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    availability: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('FAILED','ERROR') AND availability IN ('AVAILABLE','MISSING')",
            name="ck_s13_membership",
        ),
        Index(
            "ix_s13_representative",
            "incident_id",
            "channel",
            "environment",
            "ordinal",
            "case_id",
        ),
    )


class RevisionRow(GuardianBase):
    __tablename__ = "stage13_incident_evidence_revisions"
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("stage13_global_incidents.incident_id"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    material_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (CheckConstraint("revision > 0", name="ck_s13_revision"),)


class BudgetRow(GuardianBase):
    __tablename__ = "stage13_analysis_admission_budget"
    budget_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    project: Mapped[str] = mapped_column(String(128), nullable=False)
    suite: Mapped[str] = mapped_column(String(128), nullable=False)
    business_date: Mapped[str] = mapped_column(String(10), nullable=False)
    subject_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    lane: Mapped[str] = mapped_column(String(16), nullable=False, default="NIGHTLY")
    admitted_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cutoff: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        UniqueConstraint(
            "scope",
            "project",
            "suite",
            "business_date",
            "subject_digest",
            "lane",
            name="uq_s13_budget_scope",
        ),
        CheckConstraint(
            "admitted_total BETWEEN 0 AND 60 AND lane='NIGHTLY'",
            name="ck_s13_hard_budget",
        ),
    )


class AnalysisJobRow(GuardianBase):
    __tablename__ = "stage13_incident_analysis_jobs"
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    admission_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("stage13_global_incidents.incident_id"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    subject_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    budget_key: Mapped[str] = mapped_column(
        ForeignKey("stage13_analysis_admission_budget.budget_key"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    admitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["incident_id", "revision"],
            [
                "stage13_incident_evidence_revisions.incident_id",
                "stage13_incident_evidence_revisions.revision",
            ],
            name="fk_s13_job_revision",
        ),
        CheckConstraint(
            "status IN ('READY','RUNNING','COMPLETED','FAILED','UNRESOLVED','DEFERRED_BUDGET') AND kind IN ('INITIAL','REANALYSIS')",
            name="ck_s13_job",
        ),
        UniqueConstraint(
            "incident_id", "revision", "subject_digest", name="uq_s13_analysis_revision"
        ),
    )


INCIDENT_TABLES = tuple(
    name
    for name in GuardianBase.metadata.tables
    if name
    not in {
        "stage13_guardians",
        "stage13_daily_cycles",
        "stage13_version_executions",
        "stage13_environment_occupancy",
        "stage13_due_work",
        "stage13_observations",
        "stage13_scheduler_state",
    }
)
