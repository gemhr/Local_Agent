"""任务快照、不可变报告、人工计划和执行事实。"""
from sqlalchemy import Text, Integer, ForeignKey, UniqueConstraint, DateTime, Uuid, CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from core.persistence.models import PersistenceBase


class TaskRow(PersistenceBase):
    __tablename__ = "feature_impact_tasks"
    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"))
    owner_user_id: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id"))
    request_id: Mapped[str] = mapped_column(Text)
    snapshot: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    exposed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "owner_user_id", "request_id"),)


class ReportRow(PersistenceBase):
    __tablename__ = "feature_impact_reports"
    report_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("feature_impact_tasks.task_id"))
    request_id: Mapped[str] = mapped_column(Text)
    intent_digest: Mapped[str] = mapped_column(Text)
    report_version: Mapped[int] = mapped_column(Integer)
    envelope: Mapped[dict] = mapped_column(JSONB)
    raw_output: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    rejection: Mapped[str | None] = mapped_column(Text)
    source_record: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("task_id", "request_id"), UniqueConstraint("task_id", "report_version"),
        CheckConstraint("(payload IS NULL) <> (rejection IS NULL)"),)


class PlanRow(PersistenceBase):
    __tablename__ = "feature_impact_plans"
    plan_id: Mapped[str] = mapped_column(Text, primary_key=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("feature_impact_reports.report_id"), unique=True)
    request_id: Mapped[str] = mapped_column(Text)
    intent_digest: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)


class ExecutionRow(PersistenceBase):
    __tablename__ = "feature_impact_executions"
    execution_id: Mapped[str] = mapped_column(Text, primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("feature_impact_plans.plan_id"))
    request_id: Mapped[str] = mapped_column(Text)
    intent_digest: Mapped[str] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSONB)
    internal_trace: Mapped[list] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("plan_id", "request_id"),)


class InvestigationRow(PersistenceBase):
    __tablename__ = "feature_impact_investigations"
    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("feature_impact_tasks.task_id"))
    session_id: Mapped[str] = mapped_column(Text, unique=True)
    request_id: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("task_id", "request_id"),)
