"""CI 场景与追加执行的业务表。"""
from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from core.persistence.models import PersistenceBase


class ScenarioInstanceRow(PersistenceBase):
    __tablename__ = "ci_scenario_instances"
    instance_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), index=True)
    owner_user_id: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id"))
    request_id: Mapped[str] = mapped_column(Text)
    scenario_key: Mapped[str] = mapped_column(Text)
    template_version: Mapped[str] = mapped_column(Text)
    state_revision: Mapped[int] = mapped_column(Integer)
    current_state: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "owner_user_id", "request_id"), CheckConstraint("state_revision > 0"))


class CIExecutionRow(PersistenceBase):
    __tablename__ = "ci_executions"
    execution_id: Mapped[str] = mapped_column(Text, primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("ci_scenario_instances.instance_id"))
    request_id: Mapped[str] = mapped_column(Text)
    environment_id: Mapped[str] = mapped_column(Text)
    case_id: Mapped[str] = mapped_column(Text)
    feature_id: Mapped[str] = mapped_column(Text)
    state_revision: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(Text)
    execution_snapshot: Mapped[dict] = mapped_column(JSONB)
    summary: Mapped[dict] = mapped_column(JSONB)
    evidence_manifest: Mapped[list] = mapped_column(JSONB)
    evidence_digest: Mapped[str] = mapped_column(Text)
    nightly_version_execution_id: Mapped[str | None] = mapped_column(ForeignKey("nightly_version_executions.version_execution_id"))
    business_completed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("instance_id", "request_id"), CheckConstraint("outcome IN ('PASS','FAIL')"),
        CheckConstraint("state_revision > 0", name="ck_ci_execution_state_revision"),
        CheckConstraint("(nightly_version_execution_id IS NULL) = (business_completed_at IS NULL)", name="ck_ci_nightly_link"),
        Index("uq_ci_nightly_case", "nightly_version_execution_id", "case_id", unique=True,
            postgresql_where=text("nightly_version_execution_id IS NOT NULL")))


class NightlyCIBatchRow(PersistenceBase):
    __tablename__ = "nightly_ci_batches"
    batch_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"))
    created_by_user_id: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id"))
    request_id: Mapped[str] = mapped_column(Text)
    business_date: Mapped[object] = mapped_column(Date)
    plan_key: Mapped[str] = mapped_column(Text)
    plan_version: Mapped[str] = mapped_column(Text)
    plan_digest: Mapped[str] = mapped_column(Text)
    frozen_plan: Mapped[dict] = mapped_column(JSONB)
    simulated_now: Mapped[object] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    sealed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    sealed_summary: Mapped[dict | None] = mapped_column(JSONB)
    sealed_digest: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("tenant_id", "created_by_user_id", "request_id"),
        UniqueConstraint("tenant_id", "created_by_user_id", "business_date", "plan_key"),
        CheckConstraint("status IN ('PLANNED','RUNNING','COMPLETED')"),
        CheckConstraint("(status = 'COMPLETED' AND sealed_at IS NOT NULL AND sealed_summary IS NOT NULL AND sealed_digest IS NOT NULL) OR (status <> 'COMPLETED' AND sealed_at IS NULL AND sealed_summary IS NULL AND sealed_digest IS NULL)"))


class NightlyVersionExecutionRow(PersistenceBase):
    __tablename__ = "nightly_version_executions"
    version_execution_id: Mapped[str] = mapped_column(Text, primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("nightly_ci_batches.batch_id"))
    environment_id: Mapped[str] = mapped_column(Text)
    owner_user_id: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    software_version: Mapped[str] = mapped_column(Text)
    frozen_cases: Mapped[list] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    next_case_index: Mapped[int] = mapped_column(Integer)
    started_business_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    completed_business_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    started_by_user_id: Mapped[object | None] = mapped_column(Uuid, ForeignKey("users.id"))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("batch_id", "environment_id", "ordinal"),
        Index("uq_nightly_running_environment", "batch_id", "environment_id", unique=True, postgresql_where=text("status = 'RUNNING'")),
        CheckConstraint("ordinal BETWEEN 1 AND 3"), CheckConstraint("jsonb_array_length(frozen_cases) = 2"),
        CheckConstraint("status IN ('PENDING','RUNNING','COMPLETED')"), CheckConstraint("next_case_index BETWEEN 0 AND 2"),
        CheckConstraint("(status = 'PENDING' AND next_case_index = 0 AND started_business_at IS NULL AND started_by_user_id IS NULL AND completed_business_at IS NULL) OR (status = 'RUNNING' AND next_case_index < 2 AND started_business_at IS NOT NULL AND started_by_user_id IS NOT NULL AND completed_business_at IS NULL) OR (status = 'COMPLETED' AND next_case_index = 2 AND started_business_at IS NOT NULL AND started_by_user_id IS NOT NULL AND completed_business_at IS NOT NULL)"))
