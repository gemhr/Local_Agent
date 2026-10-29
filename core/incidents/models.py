"""Incident 业务事实；不复制 Runtime 状态。"""
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from core.persistence.models import PersistenceBase


class IncidentRow(PersistenceBase):
    __tablename__ = "incidents"
    incident_id: Mapped[str] = mapped_column(Text, primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("ci_executions.execution_id"), unique=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), index=True)
    owner_user_id: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    session_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    closed_by_user_id: Mapped[object | None] = mapped_column(Uuid, ForeignKey("users.id"))
    closure_payload: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint("status IN ('OPEN','PENDING_VERIFICATION','CLOSED')", name="ck_incident_status"),
        CheckConstraint("(status = 'CLOSED' AND closed_at IS NOT NULL AND closed_by_user_id IS NOT NULL AND closure_payload IS NOT NULL) OR (status <> 'CLOSED' AND closed_at IS NULL AND closed_by_user_id IS NULL AND closure_payload IS NULL)", name="ck_incident_closure"),
    )


class InvestigationRow(PersistenceBase):
    __tablename__ = "incident_investigations"
    investigation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.incident_id"), index=True)
    request_id: Mapped[str] = mapped_column(Text)
    report_version: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str] = mapped_column(Text, unique=True)
    session_id: Mapped[str] = mapped_column(Text)
    agent_id: Mapped[str] = mapped_column(Text)
    agent_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    projected_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    projection_status: Mapped[str] = mapped_column(Text)
    safe_error_code: Mapped[str | None] = mapped_column(Text)
    runtime_result_digest: Mapped[str | None] = mapped_column(Text)
    report_payload: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("incident_id", "request_id"), UniqueConstraint("incident_id", "report_version"),
        CheckConstraint("report_version > 0"), CheckConstraint("projection_status IN ('PENDING','READY','FAILED')"))


class IncidentActionRow(PersistenceBase):
    """人类预览与明确确认形成的操作事实。"""
    __tablename__ = "incident_actions"
    action_id: Mapped[str] = mapped_column(Text, primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.incident_id"), index=True)
    request_id: Mapped[str] = mapped_column(Text)
    action_kind: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(Text)
    created_by_user_id: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id"))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    base_state_revision: Mapped[int] = mapped_column(Integer)
    parameters: Mapped[dict] = mapped_column(JSONB)
    preview_payload: Mapped[dict] = mapped_column(JSONB)
    preview_digest: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    confirmed_by_user_id: Mapped[object | None] = mapped_column(Uuid, ForeignKey("users.id"))
    confirmed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    applied_state_revision: Mapped[int | None] = mapped_column(Integer)
    result_payload: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("incident_id", "request_id"),
        CheckConstraint("action_kind IN ('UPGRADE_PARSER_DECODER','SET_FEATURE_FLAG','REFRESH_CASE_INPUT','DELIVER_PRODUCT_FIX')"),
        CheckConstraint("source_kind = 'HUMAN_MANUAL'"), CheckConstraint("base_state_revision > 0"),
        CheckConstraint("(status = 'PREVIEWED' AND confirmed_by_user_id IS NULL AND confirmed_at IS NULL AND applied_state_revision IS NULL AND result_payload IS NULL) OR (status = 'APPLIED' AND confirmed_by_user_id IS NOT NULL AND confirmed_at IS NOT NULL AND applied_state_revision IS NOT NULL AND applied_state_revision > base_state_revision AND result_payload IS NOT NULL)"))


class IncidentVerificationRow(PersistenceBase):
    """同状态版本下最多两例的真实同步验证批次。"""
    __tablename__ = "incident_verifications"
    verification_id: Mapped[str] = mapped_column(Text, primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.incident_id"), index=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("ci_scenario_instances.instance_id"))
    request_id: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text)
    verification_policy_version: Mapped[str] = mapped_column(Text)
    state_revision: Mapped[int] = mapped_column(Integer)
    requested_case_ids: Mapped[list] = mapped_column(JSONB)
    items: Mapped[list] = mapped_column(JSONB)
    created_by_user_id: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id"))
    completed_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("incident_id", "request_id"), CheckConstraint("mode IN ('SINGLE','GROUP')"),
        CheckConstraint("state_revision > 0"),
        CheckConstraint("(mode = 'SINGLE' AND jsonb_array_length(items) = 1 AND jsonb_array_length(requested_case_ids) = 1) OR (mode = 'GROUP' AND jsonb_array_length(items) = 2 AND jsonb_array_length(requested_case_ids) = 2)"))


class IncidentGroupRow(PersistenceBase):
    __tablename__ = "incident_groups"
    group_id: Mapped[str] = mapped_column(Text, primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("nightly_ci_batches.batch_id"))
    rule_version: Mapped[str] = mapped_column(Text)
    signature_digest: Mapped[str] = mapped_column(Text)
    signature_payload: Mapped[dict] = mapped_column(JSONB)
    kind: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    history: Mapped[list] = mapped_column(JSONB)
    __table_args__ = (CheckConstraint("kind IN ('AUTO','MANUAL_SPLIT')"), CheckConstraint("revision > 0"),
        Index("uq_incident_auto_signature", "batch_id", "rule_version", "signature_digest", unique=True, postgresql_where=text("kind = 'AUTO'")))


class IncidentGroupMembershipRow(PersistenceBase):
    __tablename__ = "incident_group_memberships"
    group_id: Mapped[str] = mapped_column(ForeignKey("incident_groups.group_id"), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.incident_id"), primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("ci_executions.execution_id"))
    evidence_refs: Mapped[list] = mapped_column(JSONB)
    decision: Mapped[str] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    decided_by_user_id: Mapped[object | None] = mapped_column(Uuid, ForeignKey("users.id"))
    __table_args__ = (CheckConstraint("decision IN ('CANDIDATE','CONFIRMED','EXCLUDED')"),
        Index("uq_incident_current_group", "incident_id", unique=True, postgresql_where=text("decision <> 'EXCLUDED'")))


class UserNotificationRow(PersistenceBase):
    __tablename__ = "user_notifications"
    notification_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"))
    recipient_user_id: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id"))
    batch_id: Mapped[str] = mapped_column(ForeignKey("nightly_ci_batches.batch_id"))
    kind: Mapped[str] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("batch_id", "recipient_user_id", "kind"),
        CheckConstraint("kind IN ('INCREMENTAL_FAILURE','FINAL_SUMMARY')"))


class InvestigationFeedbackRow(PersistenceBase):
    """追加式人工反馈；binding 为服务端核实的稳定引用快照。"""
    __tablename__ = "incident_investigation_feedback"
    feedback_id: Mapped[str] = mapped_column(Text, primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.incident_id"), index=True)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("incident_investigations.investigation_id"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"))
    created_by_user_id: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id"))
    request_id: Mapped[str] = mapped_column(Text)
    request_payload: Mapped[dict] = mapped_column(JSONB)
    binding: Mapped[dict] = mapped_column(JSONB)
    supersedes: Mapped[str | None] = mapped_column(ForeignKey("incident_investigation_feedback.feedback_id"), unique=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("tenant_id", "created_by_user_id", "request_id"),)
