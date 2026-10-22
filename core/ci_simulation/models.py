"""CI 场景与追加执行的业务表。"""
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text, UniqueConstraint, Uuid
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
    __table_args__ = (UniqueConstraint("instance_id", "request_id"), CheckConstraint("outcome IN ('PASS','FAIL')"),
        CheckConstraint("state_revision > 0", name="ck_ci_execution_state_revision"))
