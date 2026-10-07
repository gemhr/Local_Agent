"""真实 Subject 的业务执行记录；不改变历史 Analysis Job / Runtime 表。"""

from datetime import datetime
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from core.stage13.incident_models import GuardianBase


class SubjectRow(GuardianBase):
    __tablename__ = "stage13_triage_subjects"
    subject_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_version: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    __table_args__ = (UniqueConstraint("subject_id", "subject_version"),)


class JobExecutionRow(GuardianBase):
    __tablename__ = "stage13_analysis_executions"
    job_id: Mapped[str] = mapped_column(
        ForeignKey("stage13_incident_analysis_jobs.job_id"), primary_key=True
    )
    owner: Mapped[str | None] = mapped_column(String(128))
    token: Mapped[str | None] = mapped_column(String(36))
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    initial_run_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    repair_run_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    selected_final_run_id: Mapped[str | None] = mapped_column(String(36))
    raw_answer_digest: Mapped[str | None] = mapped_column(String(64))
    structured_output_digest: Mapped[str | None] = mapped_column(String(64))
    validation: Mapped[dict | None] = mapped_column(JSONB)
    actual_subject_receipt_digest: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("epoch >= 0", name="ck_s13_execution_epoch"),)


class TriageRunRow(GuardianBase):
    __tablename__ = "stage13_triage_runs"
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    anchor_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    analysis_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("stage13_incident_analysis_jobs.job_id")
    )
    evaluation_attempt_id: Mapped[str | None] = mapped_column(String(36))
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    lane: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_digest: Mapped[str] = mapped_column(
        ForeignKey("stage13_triage_subjects.subject_digest"), nullable=False
    )
    query: Mapped[str] = mapped_column(String, nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    model_call: Mapped[dict | None] = mapped_column(JSONB)
    raw_answer: Mapped[str | None] = mapped_column(String)
    receipt: Mapped[dict | None] = mapped_column(JSONB)
    runtime_status: Mapped[str | None] = mapped_column(String(32))
    stop_reason: Mapped[str | None] = mapped_column(String(64))
    validation: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        UniqueConstraint("anchor_run_id", "role", name="uq_s13_run_role"),
        CheckConstraint(
            "role IN ('INITIAL','SCHEMA_REPAIR') AND lane IN ('NIGHTLY','CONTRACT_TEST','OFFLINE')",
            name="ck_s13_triage_run",
        ),
    )


class EvidenceReadRow(GuardianBase):
    __tablename__ = "stage13_triage_evidence_reads"
    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("stage13_triage_runs.run_id"), nullable=False, index=True
    )
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    successful: Mapped[bool] = mapped_column(nullable=False, default=False)


TRIAGE_TABLES = (
    SubjectRow.__tablename__,
    JobExecutionRow.__tablename__,
    TriageRunRow.__tablename__,
    EvidenceReadRow.__tablename__,
)
