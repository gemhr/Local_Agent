#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PostgreSQL Canonical Schema 的 SQLAlchemy 2.x 声明式模型。

Owner：本模块是 LocalAgent 关系型持久化的 Model Owner。表的物理 shape 只由
Alembic revision 产生（见 ``alembic/versions/``）；运行时代码**不**执行 DDL。

与 SQLite 的映射原则：

* 业务身份（run_id / event_id / snapshot_id / memory_id / exchange_id）保持
  既有 stable UUID-as-TEXT contract，不因换库改写 identity 格式；
* ``runtime_event_journal.safe_payload`` 保持 canonical JSON **文本**，因为
  ``event_digest`` 是该表的持久化身份算法，jsonb 的数值/键规范化会改变
  digest source（Runtime Contract 优先于存储形式便利）；
* Advanced / Project Memory 的 ``payload`` 使用 ``jsonb``：该处没有摘要
  identity，jsonb 更贴近 PostgreSQL 且可被 partial index 直接索引；
* ``messages.timestamp`` 由数据库产生（``now()``），读取时按既有 wire 格式
  投影，保持 Desktop 客户端兼容。
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    BigInteger,
    CHAR,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class PersistenceBase(DeclarativeBase):
    """所有持久化模型的公共 Base；不提供通用 CRUD。"""


class TenantRow(PersistenceBase):
    """Stage9 最小 Tenant identity；不承载组织层级或成员关系。"""
    __tablename__ = "tenants"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class FeatureTestMissionRow(PersistenceBase):
    """Stage8 业务 Mission；不承载 Runtime Run 状态。"""
    __tablename__ = "stage8_feature_test_missions"
    mission_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    feature_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"), onupdate=text("now()"))
    __table_args__ = (CheckConstraint("version > 0", name="ck_stage8_mission_version"),)


class MissionRunReferenceRow(PersistenceBase):
    """Mission 到 AgentCore Run 的引用，不改变 Run 生命周期。"""
    __tablename__ = "stage8_mission_run_refs"
    reference_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("stage8_feature_test_missions.mission_id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    run_purpose: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    __table_args__ = (UniqueConstraint("mission_id", "run_id", "run_purpose", name="uq_stage8_mission_run_ref"),)


class BusinessReviewRow(PersistenceBase):
    """Stage8 业务 Review；独立于 Runtime Tool Approval。"""
    __tablename__ = "stage8_business_reviews"
    review_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("stage8_feature_test_missions.mission_id", ondelete="CASCADE"), nullable=False)
    review_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    subject_digest: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    decided_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)


class Stage8TestPlanRow(PersistenceBase):
    """已校验的 TestPlan subject；payload 保存业务结果快照。"""
    __tablename__ = "stage8_test_plans"
    subject_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("stage8_feature_test_missions.mission_id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    subject_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    __table_args__ = (UniqueConstraint("mission_id", "subject_id", "version", name="uq_stage8_test_plan_version"),)


class Stage8GeneratedCaseArtifactRow(PersistenceBase):
    """正式 Case Platform 结果及其 approved TestPlan snapshot 绑定。"""
    __tablename__ = "stage8_generated_case_artifacts"
    artifact_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("stage8_feature_test_missions.mission_id", ondelete="CASCADE"), nullable=False)
    test_plan_subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    test_plan_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    test_plan_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_case_id: Mapped[str] = mapped_column(String(255), nullable=False)
    case_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'GENERATED'"))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    __table_args__ = (UniqueConstraint("mission_id", "test_plan_subject_id", "test_plan_version", "test_plan_digest", "scenario_id", name="uq_stage8_generated_case_binding"),)


class Stage8ExternalExecutionJobRow(PersistenceBase):
    """Stage8 外部执行 Job；是外部测试生命周期的业务 Authority。"""
    __tablename__ = "stage8_external_execution_jobs"
    job_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("stage8_feature_test_missions.mission_id", ondelete="CASCADE"), nullable=False)
    execution_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    plan_id: Mapped[str] = mapped_column(String(255), nullable=False)
    case_id: Mapped[str] = mapped_column(String(255), nullable=False)
    environment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    executor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    attempt_no: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    auto_repair_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    plan_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    result_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    triage_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    started_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Stage8TicketContinuationRow(PersistenceBase):
    """PRODUCT failure 的 durable downstream ticket action。"""
    __tablename__ = "stage8_ticket_continuations"
    continuation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("stage8_feature_test_missions.mission_id", ondelete="CASCADE"), nullable=False)
    execution_job_id: Mapped[str] = mapped_column(ForeignKey("stage8_external_execution_jobs.job_id", ondelete="CASCADE"), nullable=False)
    triage_id: Mapped[str] = mapped_column(String(255), nullable=False)
    ticket_draft_id: Mapped[str] = mapped_column(String(255), nullable=False)
    approval_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    tool_invocation_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    invocation_binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'PENDING_APPROVAL'"))
    external_ticket_id: Mapped[str | None] = mapped_column(String(255))
    external_ticket_url: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    __table_args__ = (
        CheckConstraint("state IN ('PENDING_APPROVAL', 'REJECTED', 'READY', 'PROCESSING', 'SUCCEEDED', 'FAILED', 'UNKNOWN')", name="ck_stage8_ticket_continuation_state"),
        UniqueConstraint("execution_job_id", "ticket_draft_id", name="uq_stage8_ticket_continuation_draft"),
        Index("ix_stage8_ticket_continuations_state", "state"),
    )


class Stage8CIRunRow(PersistenceBase):
    __tablename__ = "stage8_ci_runs"
    ci_run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    suite_id: Mapped[str] = mapped_column(String(255), nullable=False)
    started_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    branch: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class Stage8CIAnalysisRow(PersistenceBase):
    __tablename__ = "stage8_ci_analysis"
    analysis_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    ci_run_id: Mapped[str] = mapped_column(ForeignKey("stage8_ci_runs.ci_run_id", ondelete="CASCADE"), nullable=False, unique=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class UserRow(PersistenceBase):
    __tablename__ = "users"
    id: Mapped[object] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
        nullable=False, server_default=text("'00000000-0000-0000-0000-000000000001'"),
    )
    subject: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    disabled_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    principal_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'HUMAN'"))
    service_scopes: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))

    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_users_id_tenant"),
    )


class RoleRow(PersistenceBase):
    __tablename__ = "roles"
    id: Mapped[object] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)

    __table_args__ = (
        CheckConstraint(
            "code IN ('USER', 'OPERATOR', 'ADMIN', 'SERVICE')",
            name="ck_roles_code",
        ),
    )


class UserRoleRow(PersistenceBase):
    __tablename__ = "user_roles"
    user_id: Mapped[object] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[object] = mapped_column(Uuid(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)


class ObjectOwnershipRow(PersistenceBase):
    """对象归属的窄持久化绑定；不承接 Runtime 状态或执行 Owner。"""

    __tablename__ = "object_ownership"
    object_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner_user_id: Mapped[object] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
        nullable=False, server_default=text("'00000000-0000-0000-0000-000000000001'"),
    )
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "object_type IN ('RUN', 'CONVERSATION', 'MISSION', 'REVIEW', 'TEST_PLAN', 'ARTIFACT', 'GENERATED_CASE_ARTIFACT', 'EXTERNAL_EXECUTION_JOB', 'APPROVAL', 'TICKET_CONTINUATION', 'EVALUATION_JOB')",
            name="ck_object_ownership_type",
        ),
        ForeignKeyConstraint(
            ["owner_user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_object_ownership_owner_tenant",
            ondelete="RESTRICT",
        ),
        Index("ix_object_ownership_owner_type", "owner_user_id", "object_type"),
    )


class EvaluationJobRow(PersistenceBase):
    """持久化 Evaluation Job；Job 状态的 PostgreSQL Authority。"""

    __tablename__ = "evaluation_jobs"

    id: Mapped[object] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_user_id: Mapped[object] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    evaluator_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    request_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'QUEUED'")
    )
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    queued_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    started_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    terminal_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    worker_claim_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    worker_claim_token: Mapped[object | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    worker_claim_deadline: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_evaluation_jobs_status",
        ),
        CheckConstraint("attempt >= 0", name="ck_evaluation_jobs_attempt"),
        CheckConstraint("version >= 1", name="ck_evaluation_jobs_version"),
        CheckConstraint(
            "(worker_claim_owner IS NULL AND worker_claim_token IS NULL AND worker_claim_deadline IS NULL) "
            "OR (worker_claim_owner IS NOT NULL AND worker_claim_token IS NOT NULL AND worker_claim_deadline IS NOT NULL)",
            name="ck_evaluation_jobs_worker_claim_tuple",
        ),
        Index(
            "ix_evaluation_jobs_active",
            "status",
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
        Index(
            "ix_evaluation_jobs_owner_created",
            "owner_user_id",
            text("created_at DESC"),
        ),
    )


class EvaluationResultRow(PersistenceBase):
    """Evaluation 成功结果的持久化记录；每个 Job 至多一条。"""

    __tablename__ = "evaluation_results"

    id: Mapped[object] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    job_id: Mapped[object] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("evaluation_jobs.id", ondelete="RESTRICT"),
        nullable=False, unique=True,
    )
    result_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class OutboxEventRow(PersistenceBase):
    """Transactional Outbox；只保存待发布或已发布事件。"""

    __tablename__ = "outbox_events"

    event_id: Mapped[object] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[object] = mapped_column(Uuid(as_uuid=True), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    traceparent: Mapped[str | None] = mapped_column(String(55), nullable=True)
    tracestate: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'PENDING'")
    )


    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    available_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    claim_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim_token: Mapped[object | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    claim_deadline: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "schema_version > 0", name="ck_outbox_events_schema_version"
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_outbox_events_attempt_count"
        ),
        CheckConstraint(
            "status IN ('PENDING', 'PUBLISHED')", name="ck_outbox_events_status"
        ),
        CheckConstraint(
            "(claim_owner IS NULL AND claim_token IS NULL AND claim_deadline IS NULL) "
            "OR (claim_owner IS NOT NULL AND claim_token IS NOT NULL AND claim_deadline IS NOT NULL)",
            name="ck_outbox_events_claim_tuple",
        ),
        CheckConstraint(
            "(status = 'PENDING' AND published_at IS NULL) "
            "OR (status = 'PUBLISHED' AND published_at IS NOT NULL)",
            name="ck_outbox_events_publication_state",
        ),
        Index(
            "ix_outbox_events_pending_available",
            "available_at",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )


class ConsumerProcessedEventRow(PersistenceBase):
    """PostgreSQL business-processing evidence for Kafka at-least-once delivery."""

    __tablename__ = "consumer_processed_events"

    consumer_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[object] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    partition: Mapped[int] = mapped_column(Integer, nullable=False)
    offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    processed_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('SUCCEEDED', 'FAILED', 'CANCELLED_NOOP', 'TERMINAL_NOOP', 'DUPLICATE')",
            name="ck_consumer_processed_events_outcome",
        ),
        Index("ix_consumer_processed_events_event", "event_id"),
    )


# ---------------------------------------------------------------------------
# Runtime persistence
# ---------------------------------------------------------------------------

_TERMINAL_EVENT_TYPE_SQL = "RUN_COMPLETED"


class RuntimeEventJournalRow(PersistenceBase):
    """Run Event Journal 的 append-only 记录。"""

    __tablename__ = "runtime_event_journal"

    journal_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    emitted_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    journaled_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    component: Mapped[str] = mapped_column(String(128), nullable=False)
    step_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    step_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    span_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    safe_payload: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    event_digest: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        # (run_id, sequence) 是 Journal 的 identity；event_id 必须全局唯一。
        UniqueConstraint("event_id", name="uq_runtime_event_journal_event_id"),
        Index("ix_runtime_event_journal_run_type", "run_id", "event_type"),
        # terminal invariant 的数据库 Authority：每个 Run 至多一个终态事件。
        Index(
            "uq_runtime_event_journal_terminal_per_run",
            "run_id",
            unique=True,
            postgresql_where=text(f"event_type = '{_TERMINAL_EVENT_TYPE_SQL}'"),
        ),
        CheckConstraint("sequence > 0", name="ck_runtime_event_journal_sequence"),
        CheckConstraint(
            "journal_schema_version > 0",
            name="ck_runtime_event_journal_journal_schema_version",
        ),
        CheckConstraint(
            "event_schema_version > 0",
            name="ck_runtime_event_journal_event_schema_version",
        ),
        CheckConstraint(
            "step_id IS NOT NULL OR step_sequence IS NULL",
            name="ck_runtime_event_journal_step_pairing",
        ),
    )


class ClientDeliveryEventRow(PersistenceBase):
    """Client-safe durable delivery projection; it is not Run authority."""

    __tablename__ = "client_delivery_events"
    run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    cursor: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    __table_args__ = (
        Index("ix_client_delivery_events_run_cursor", "run_id", "cursor"),
        CheckConstraint("cursor > 0", name="ck_client_delivery_events_cursor"),
    )


class ToolResolutionSnapshotRow(PersistenceBase):
    """每个 Run 唯一且不可变的 Tool contract snapshot。"""

    __tablename__ = "tool_resolution_snapshots"
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runtime_run_control.run_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    snapshot_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    snapshot_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    registry_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    selection_algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    selection_query_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    tool_items: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "snapshot_schema_version > 0",
            name="ck_tool_resolution_snapshot_schema_version",
        ),
    )


class RunControlRow(PersistenceBase):
    """Durable Run control aggregate；不承接 Journal terminal truth。"""

    __tablename__ = "runtime_run_control"

    run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_until: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'ACTIVE'"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    cancel_command_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancel_requested_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminal_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("state IN ('ACTIVE', 'CLOSED')", name="ck_runtime_run_control_state"),
        CheckConstraint("fencing_token >= 0", name="ck_runtime_run_control_fencing_token"),
        CheckConstraint("version > 0", name="ck_runtime_run_control_version"),
    )


class RunControlCommandRow(PersistenceBase):
    """Durable idempotency record for Run control commands."""

    __tablename__ = "runtime_run_control_commands"

    command_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    command_type: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("run_id", "command_type", name="uq_runtime_run_control_command_type"),
        CheckConstraint("command_type = 'CANCEL'", name="ck_runtime_run_control_command_type"),
    )


class DurableApprovalRow(PersistenceBase):
    """PostgreSQL canonical HITL approval aggregate (no raw invocation data)."""

    __tablename__ = "runtime_tool_approvals"
    approval_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    step_id: Mapped[str] = mapped_column(String(255), nullable=False)
    invocation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    invocation_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key_digest: Mapped[str | None] = mapped_column(String(64))
    resource_key_digest: Mapped[str | None] = mapped_column(String(64))
    invocation_binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str | None] = mapped_column(String(64))
    risk_facts: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'PENDING'"))
    decision: Mapped[str | None] = mapped_column(String(16))
    actor_id_digest: Mapped[str | None] = mapped_column(String(64))
    invalidated_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    decided_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    __table_args__ = (
        UniqueConstraint("run_id", "invocation_id", name="uq_runtime_tool_approval_invocation"),
        CheckConstraint("state IN ('PENDING', 'APPROVED', 'REJECTED', 'INVALIDATED')", name="ck_runtime_tool_approval_state"),
        CheckConstraint("version > 0", name="ck_runtime_tool_approval_version"),
        Index("ix_runtime_tool_approvals_run_state", "run_id", "state"),
    )


class DurableToolExecutionClaimRow(PersistenceBase):
    """One durable authorization claim per immutable approved invocation."""

    __tablename__ = "runtime_tool_execution_claims"
    claim_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    approval_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    invocation_binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class DurableToolInvocationRow(PersistenceBase):
    """PostgreSQL side-effect invocation aggregate owned by Tool Runtime."""

    __tablename__ = "runtime_tool_invocations"

    invocation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    step_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Stage10 recovery identity/result binding. Nullable keeps existing Tool
    # rows readable until the Tool Runtime batch populates the safe digest.
    arguments_digest: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    invocation_binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_key_digest: Mapped[str | None] = mapped_column(String(64))
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(255))
    execution_claim_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'PREPARED'"))
    provider_operation_id: Mapped[str | None] = mapped_column(String(255))
    committed_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    committed_result_digest: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    uncertainty_reason: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    started_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    committed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    unknown_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    reconciled_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    reconcile_attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    last_reconcile_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    last_safe_error_code: Mapped[str | None] = mapped_column(String(128))
    next_reconcile_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    manual_required: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))

    __table_args__ = (
        UniqueConstraint("run_id", "step_id", "invocation_id", name="uq_runtime_tool_invocation_identity"),
        UniqueConstraint(
            "run_id",
            "tool_name",
            "idempotency_key_digest",
            name="uq_runtime_tool_invocation_idempotency",
        ),
        CheckConstraint(
            "state IN ('PREPARED', 'STARTED', 'COMMITTED', 'UNKNOWN', 'NOT_COMMITTED')",
            name="ck_runtime_tool_invocation_state",
        ),
        CheckConstraint("version > 0", name="ck_runtime_tool_invocation_version"),
        CheckConstraint("reconcile_attempt_count >= 0", name="ck_runtime_tool_invocation_reconcile_attempts"),
        Index("ix_runtime_tool_invocations_run_state", "run_id", "state"),
        Index("ix_runtime_tool_invocations_reconcile", "state", "next_reconcile_at", "manual_required"),
        Index(
            "ix_runtime_tool_invocations_recovery_identity",
            "run_id", "step_id", "tool_name", "arguments_digest",
        ),
    )


class ManualToolResolutionAuditRow(PersistenceBase):
    """审计人工副作用收口；不保存 Tool 参数或 Provider 正文。"""

    __tablename__ = "runtime_tool_manual_resolution_audit"

    audit_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    step_id: Mapped[str] = mapped_column(String(255), nullable=False)
    invocation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    resolution: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    occurred_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "resolution IN ('COMMITTED', 'NOT_COMMITTED')",
            name="ck_runtime_tool_manual_resolution_audit_resolution",
        ),
        Index(
            "ix_runtime_tool_manual_resolution_audit_invocation",
            "invocation_id",
            "occurred_at",
        ),
        Index(
            "ix_runtime_tool_manual_resolution_audit_run",
            "run_id",
            "occurred_at",
        ),
    )


class DurableContinuationRow(PersistenceBase):
    """Generic durable resume scheduling aggregate; not a side-effect authority."""

    __tablename__ = "runtime_continuations"
    continuation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runtime_run_control.run_id", ondelete="RESTRICT"), nullable=False)
    continuation_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'WAITING'"))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    claim_token: Mapped[str | None] = mapped_column(String(255))
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    claim_deadline_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    __table_args__ = (
        CheckConstraint("state IN ('WAITING', 'READY', 'PROCESSING', 'SUCCEEDED', 'FAILED', 'CANCELLED')", name="ck_runtime_continuation_state"),
        CheckConstraint("attempt_count >= 0", name="ck_runtime_continuation_attempt_count"),
        Index("ix_runtime_continuations_ready", "state", "created_at"),
        Index("ix_runtime_continuations_expired", "state", "claim_deadline_at"),
        Index("ix_runtime_continuations_run", "run_id", "created_at"),
    )


class RuntimeRunExecutionRow(PersistenceBase):
    """Canonical durable execution root; the source for rehydration."""

    __tablename__ = "runtime_run_executions"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runtime_run_control.run_id", ondelete="RESTRICT"), primary_key=True
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    execution_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    execution_policy: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'fixed-plan.v1'"))
    resume_input: Mapped[dict] = mapped_column(JSONB, nullable=False)
    plan_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    plan_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'ACTIVE'"))
    stop_reason: Mapped[str | None] = mapped_column(String(64))
    final_result_binding: Mapped[dict | None] = mapped_column(JSONB)
    absolute_deadline: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    budget_totals: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    budget_reserved: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    budget_consumed: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    budget_operations: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    recovery_supported: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    recovery_attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    last_recovery_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    last_recovery_error: Mapped[str | None] = mapped_column(String(128))
    next_recovery_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    manual_required: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    last_committed_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="ck_runtime_run_execution_schema_version"),
        CheckConstraint("execution_version > 0", name="ck_runtime_run_execution_version"),
        CheckConstraint(
            "plan_version > 0", name="ck_runtime_run_execution_plan_version"
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'BLOCKED')",
            name="ck_runtime_run_execution_status",
        ),
        CheckConstraint("recovery_attempt_count >= 0", name="ck_runtime_run_execution_recovery_attempts"),
        CheckConstraint(
            "last_committed_fencing_token >= 0",
            name="ck_runtime_run_execution_fencing_token",
        ),
        Index("ix_runtime_run_executions_recovery", "status", "recovery_supported", "next_recovery_at"),
    )


class RuntimeStepExecutionRow(PersistenceBase):
    """Canonical durable step state and complete typed result binding."""

    __tablename__ = "runtime_step_executions"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runtime_run_executions.run_id", ondelete="CASCADE"),
        primary_key=True,
    )
    step_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'PENDING'"))
    current_attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    execution_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    risk_classification: Mapped[str] = mapped_column(String(32), nullable=False)
    typed_result_payload: Mapped[dict | None] = mapped_column(JSONB)
    result_digest: Mapped[str | None] = mapped_column(CHAR(64))
    safe_error: Mapped[str | None] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    last_committed_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    __table_args__ = (
        UniqueConstraint("run_id", "step_id", name="uq_runtime_step_execution_identity"),
        CheckConstraint("current_attempt >= 0", name="ck_runtime_step_execution_attempt"),
        CheckConstraint("plan_version > 0", name="ck_runtime_step_execution_plan_version"),
        CheckConstraint("version > 0", name="ck_runtime_step_execution_version"),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'BLOCKED', 'SKIPPED')",
            name="ck_runtime_step_execution_status",
        ),
        CheckConstraint(
            "last_committed_fencing_token >= 0",
            name="ck_runtime_step_execution_fencing_token",
        ),
        Index("ix_runtime_step_executions_run_status", "run_id", "status"),
    )


class RuntimeModelInvocationRow(PersistenceBase):
    """Durable model attempt; UNKNOWN is explicit and never inferred as success."""

    __tablename__ = "runtime_model_invocations"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runtime_run_executions.run_id", ondelete="CASCADE"),
        primary_key=True,
    )
    step_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    attempt_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_attempt_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    loop_frame_id: Mapped[str | None] = mapped_column(String(255))
    loop_iteration_no: Mapped[int | None] = mapped_column(Integer)
    invocation_group: Mapped[str | None] = mapped_column(String(255))
    resume_generation: Mapped[int | None] = mapped_column(BigInteger)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'DECISION'"))
    memory_operation_id: Mapped[str | None] = mapped_column(String(255))
    context_snapshot_id: Mapped[str | None] = mapped_column(String(255))
    context_snapshot_version: Mapped[int | None] = mapped_column(Integer)
    context_snapshot_digest: Mapped[str | None] = mapped_column(CHAR(64))
    compaction_operation_id: Mapped[str | None] = mapped_column(String(255))
    request_assessment: Mapped[dict | None] = mapped_column(JSONB)
    request_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    provider_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'NOT_STARTED'"))
    result_binding: Mapped[dict | None] = mapped_column(JSONB)
    result_digest: Mapped[str | None] = mapped_column(CHAR(64))
    usage_evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    cost_evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    safe_error: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    last_committed_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    __table_args__ = (
        ForeignKeyConstraint(["loop_frame_id", "loop_iteration_no"], ["runtime_loop_iterations.frame_id", "runtime_loop_iterations.iteration_no"], name="fk_runtime_model_loop_iteration", use_alter=True),
        CheckConstraint("(loop_frame_id IS NULL AND loop_iteration_no IS NULL AND invocation_group IS NULL AND resume_generation IS NULL) OR (loop_frame_id IS NOT NULL AND ((purpose = 'DECISION' AND loop_iteration_no IS NOT NULL AND loop_iteration_no > 0) OR (purpose IN ('COMPACTION','FORMATION') AND loop_iteration_no IS NULL)) AND invocation_group IS NOT NULL AND resume_generation IS NOT NULL AND resume_generation >= 0)", name="ck_runtime_model_loop_binding"),
        ForeignKeyConstraint(["memory_operation_id"], ["memory_operations.operation_id"], name="fk_model_memory_operation", use_alter=True),
        ForeignKeyConstraint(["loop_frame_id", "context_snapshot_id", "context_snapshot_version", "context_snapshot_digest"], ["runtime_context_snapshots.frame_id", "runtime_context_snapshots.snapshot_id", "runtime_context_snapshots.version", "runtime_context_snapshots.snapshot_digest"], name="fk_model_context_snapshot", use_alter=True),
        ForeignKeyConstraint(["loop_frame_id", "compaction_operation_id"], ["runtime_context_operations.frame_id", "runtime_context_operations.operation_id"], name="fk_model_context_operation", use_alter=True),
        CheckConstraint("(purpose = 'FORMATION' AND compaction_operation_id IS NULL AND context_snapshot_id IS NULL) OR (purpose = 'DECISION' AND compaction_operation_id IS NULL) OR (purpose = 'COMPACTION' AND compaction_operation_id IS NOT NULL AND loop_frame_id IS NOT NULL AND loop_iteration_no IS NULL AND context_snapshot_id IS NULL)", name="ck_model_context_purpose"),
        CheckConstraint("(context_snapshot_id IS NULL AND context_snapshot_version IS NULL AND context_snapshot_digest IS NULL) OR (context_snapshot_id IS NOT NULL AND context_snapshot_version IS NOT NULL AND context_snapshot_version > 0 AND context_snapshot_digest IS NOT NULL)", name="ck_model_context_tuple"),
        CheckConstraint("attempt_number > 0", name="ck_runtime_model_attempt_number"),
        CheckConstraint("model_attempt_number > 0", name="ck_runtime_model_model_attempt_number"),
        CheckConstraint("state IN ('NOT_STARTED', 'STARTED', 'COMPLETED', 'UNKNOWN', 'REJECTED')", name="ck_runtime_model_state"),
        CheckConstraint(
            "last_committed_fencing_token >= 0",
            name="ck_runtime_model_fencing_token",
        ),
        Index("ix_runtime_model_invocations_run_state", "run_id", "state"),
    )


class RuntimeLoopFrameRow(PersistenceBase):
    """单 Step Loop 的恢复事实；正文仅属于受控 execution aggregate。"""

    __tablename__ = "runtime_loop_frames"
    frame_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    step_id: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    decision_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    context_seed_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    execution_policy: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'durable-agent-loop.v1'"))
    frame_role: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'ROOT'"))
    execution_agent_id: Mapped[str | None] = mapped_column(String(255))
    parent_frame_id: Mapped[str | None] = mapped_column(String(255))
    delegation_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    resume_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    last_committed_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'NEED_DECISION'"))
    current_iteration: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    committed_observation_cursor: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    context_seed: Mapped[dict] = mapped_column(JSONB, nullable=False)
    seed_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    pinned_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    config_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    loop_policy: Mapped[dict] = mapped_column(JSONB, nullable=False)
    context_policy: Mapped[dict | None] = mapped_column(JSONB)
    current_context_snapshot_id: Mapped[str | None] = mapped_column(String(255))
    current_context_snapshot_version: Mapped[int | None] = mapped_column(Integer)
    current_context_snapshot_digest: Mapped[str | None] = mapped_column(CHAR(64))
    sealed_context_evidence: Mapped[dict | None] = mapped_column(JSONB)
    stop_code: Mapped[str | None] = mapped_column(String(128))
    stop_details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    result_decision_id: Mapped[str | None] = mapped_column(String(255))
    final_delivery_receipt: Mapped[dict | None] = mapped_column(JSONB)
    sealed_evidence: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "step_id"], ["runtime_step_executions.run_id", "runtime_step_executions.step_id"], ondelete="CASCADE", name="fk_runtime_loop_frame_step"),
        Index("uq_runtime_loop_root_step", "run_id", "step_id", unique=True, postgresql_where=text("frame_role = 'ROOT'")),
        UniqueConstraint("frame_id", "run_id", "step_id", name="uq_runtime_loop_frame_identity"),
        ForeignKeyConstraint(["frame_id", "current_context_snapshot_id", "current_context_snapshot_version", "current_context_snapshot_digest"], ["runtime_context_snapshots.frame_id", "runtime_context_snapshots.snapshot_id", "runtime_context_snapshots.version", "runtime_context_snapshots.snapshot_digest"], name="fk_frame_context_snapshot", use_alter=True),
        CheckConstraint("(schema_version = 1 AND decision_schema_version = 1 AND context_seed_version = 1 AND execution_policy = 'durable-agent-loop.v1' AND frame_role = 'ROOT' AND parent_frame_id IS NULL AND delegation_id IS NULL) OR (schema_version = 2 AND decision_schema_version = 2 AND context_seed_version = 2 AND execution_policy = 'durable-agent-loop.v2' AND execution_agent_id IS NOT NULL)", name="ck_runtime_loop_frame_versions"),
        CheckConstraint("(frame_role = 'ROOT' AND parent_frame_id IS NULL AND delegation_id IS NULL) OR (frame_role = 'CHILD' AND parent_frame_id IS NOT NULL AND delegation_id IS NOT NULL AND schema_version = 2)", name="ck_loop_frame_role"),
        CheckConstraint("row_version > 0 AND resume_generation >= 0 AND last_committed_fencing_token >= 0 AND current_iteration >= 0 AND committed_observation_cursor >= 0 AND committed_observation_cursor <= current_iteration", name="ck_runtime_loop_frame_counters"),
        CheckConstraint("phase IN ('NEED_DECISION','DECISION_ACCEPTED','WAIT_TOOL','WAIT_ACTIONS','WAIT_CHILD','RESULT_READY','STOPPED')", name="ck_runtime_loop_frame_phase"),
    )


class RuntimeLoopIterationRow(PersistenceBase):
    """唯一已接受决策及确认 Observation；不拥有模型和工具执行状态。"""

    __tablename__ = "runtime_loop_iterations"
    frame_id: Mapped[str] = mapped_column(ForeignKey("runtime_loop_frames.frame_id", ondelete="CASCADE"), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    iteration_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    step_id: Mapped[str] = mapped_column(String(255), nullable=False)
    context_input_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    context_cursor: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'OPEN'"))
    decision_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    decision_kind: Mapped[str | None] = mapped_column(String(16))
    decision_digest: Mapped[str | None] = mapped_column(CHAR(64))
    accepted_model_attempt: Mapped[int | None] = mapped_column(Integer)
    accepted_model_attempt_number: Mapped[int | None] = mapped_column(Integer)
    accepted_generation: Mapped[int | None] = mapped_column(BigInteger)
    action_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    action_recipe: Mapped[dict | None] = mapped_column(JSONB)
    tool_invocation_id: Mapped[str | None] = mapped_column(ForeignKey("runtime_tool_invocations.invocation_id", ondelete="RESTRICT"), unique=True)
    assistant_record: Mapped[dict | None] = mapped_column(JSONB)
    observation: Mapped[dict | None] = mapped_column(JSONB)
    observation_source_digest: Mapped[str | None] = mapped_column(CHAR(64))
    observation_digest: Mapped[str | None] = mapped_column(CHAR(64))
    observation_cursor: Mapped[int | None] = mapped_column(Integer)
    observed_generation: Mapped[int | None] = mapped_column(BigInteger)
    context_snapshot_id: Mapped[str | None] = mapped_column(String(255))
    context_snapshot_version: Mapped[int | None] = mapped_column(Integer)
    context_snapshot_digest: Mapped[str | None] = mapped_column(CHAR(64))
    context_rebind_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    model_recovery_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    __table_args__ = (
        ForeignKeyConstraint(["frame_id", "run_id", "step_id"], ["runtime_loop_frames.frame_id", "runtime_loop_frames.run_id", "runtime_loop_frames.step_id"], name="fk_runtime_loop_iteration_frame_identity"),
        ForeignKeyConstraint(["run_id", "step_id", "accepted_model_attempt", "accepted_model_attempt_number"], ["runtime_model_invocations.run_id", "runtime_model_invocations.step_id", "runtime_model_invocations.attempt_number", "runtime_model_invocations.model_attempt_number"], name="fk_runtime_loop_iteration_accepted_model"),
        ForeignKeyConstraint(["frame_id", "context_snapshot_id", "context_snapshot_version", "context_snapshot_digest"], ["runtime_context_snapshots.frame_id", "runtime_context_snapshots.snapshot_id", "runtime_context_snapshots.version", "runtime_context_snapshots.snapshot_digest"], name="fk_iteration_context_snapshot", use_alter=True),
        CheckConstraint("context_rebind_count BETWEEN 0 AND 1", name="ck_context_rebind_count"),
        CheckConstraint("iteration_no > 0 AND context_cursor >= 0", name="ck_runtime_loop_iteration_counters"),
        CheckConstraint("model_recovery_count >= 0 AND model_recovery_count <= 2", name="ck_runtime_loop_model_recovery_count"),
        CheckConstraint("state IN ('OPEN','ACCEPTED','OBSERVED','CLOSED')", name="ck_runtime_loop_iteration_state"),
        CheckConstraint("(schema_version = 1 AND (decision_kind IS NULL OR decision_kind IN ('TOOL_CALL','FINAL','REFUSAL'))) OR (schema_version = 2 AND (decision_kind IS NULL OR decision_kind IN ('TOOL_BATCH','HANDOFF','FINAL','REFUSAL')))", name="ck_runtime_loop_decision_kind"),
        CheckConstraint("(decision_id IS NULL AND decision_kind IS NULL AND decision_digest IS NULL AND accepted_model_attempt IS NULL AND accepted_model_attempt_number IS NULL AND accepted_generation IS NULL) OR (decision_id IS NOT NULL AND decision_kind IS NOT NULL AND decision_digest IS NOT NULL AND accepted_model_attempt > 0 AND accepted_model_attempt_number > 0 AND accepted_generation >= 0)", name="ck_runtime_loop_accepted_tuple"),
        CheckConstraint("(decision_kind = 'TOOL_CALL' AND action_id IS NOT NULL AND action_recipe IS NOT NULL) OR (decision_kind IS DISTINCT FROM 'TOOL_CALL' AND action_id IS NULL AND action_recipe IS NULL AND tool_invocation_id IS NULL)", name="ck_runtime_loop_action_tuple"),
        CheckConstraint("(observation IS NULL AND observation_source_digest IS NULL AND observation_digest IS NULL AND observation_cursor IS NULL AND observed_generation IS NULL) OR ((decision_kind = 'TOOL_CALL' AND tool_invocation_id IS NOT NULL OR schema_version = 2 AND decision_kind IN ('TOOL_BATCH','HANDOFF')) AND observation IS NOT NULL AND observation_source_digest IS NOT NULL AND observation_digest IS NOT NULL AND observation_cursor > 0 AND observed_generation >= 0)", name="ck_runtime_loop_observation_tuple"),
    )


class RuntimeLoopActionRow(PersistenceBase):
    """批次的不可变执行安排；结果状态仍由原 Tool Owner 拥有。"""
    __tablename__ = "runtime_loop_actions"
    action_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    frame_id: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    step_id: Mapped[str] = mapped_column(String(255), nullable=False)
    iteration_no: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    protocol_call_id: Mapped[str] = mapped_column(String(255), nullable=False)
    recipe: Mapped[dict] = mapped_column(JSONB, nullable=False)
    recipe_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    invocation_id: Mapped[str | None] = mapped_column(ForeignKey("runtime_tool_invocations.invocation_id"), unique=True)
    result_projection: Mapped[dict | None] = mapped_column(JSONB)
    result_digest: Mapped[str | None] = mapped_column(CHAR(64))
    source_digest: Mapped[str | None] = mapped_column(CHAR(64))
    not_executed: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        ForeignKeyConstraint(["frame_id", "iteration_no"], ["runtime_loop_iterations.frame_id", "runtime_loop_iterations.iteration_no"]),
        ForeignKeyConstraint(["frame_id", "run_id", "step_id"], ["runtime_loop_frames.frame_id", "runtime_loop_frames.run_id", "runtime_loop_frames.step_id"]),
        ForeignKeyConstraint(["run_id", "step_id", "invocation_id"], ["runtime_tool_invocations.run_id", "runtime_tool_invocations.step_id", "runtime_tool_invocations.invocation_id"]),
        UniqueConstraint("frame_id", "iteration_no", "ordinal", name="uq_loop_action_ordinal"),
        UniqueConstraint("frame_id", "protocol_call_id", name="uq_loop_action_call_id"),
        CheckConstraint("ordinal BETWEEN 0 AND 3", name="ck_loop_action_ordinal"),
        CheckConstraint("invocation_id IS NULL OR invocation_id = action_id", name="ck_loop_action_invocation"),
        CheckConstraint("(result_projection IS NULL AND result_digest IS NULL AND source_digest IS NULL) OR (result_projection IS NOT NULL AND result_digest IS NOT NULL AND source_digest IS NOT NULL AND invocation_id IS NOT NULL AND not_executed IS NULL)", name="ck_loop_action_result"),
    )


class RuntimeAgentDelegationRow(PersistenceBase):
    """同一 Run/Step 的有限子 Frame 与内部结果，不是 Run Terminal。"""
    __tablename__ = "runtime_agent_delegations"
    delegation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    step_id: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_frame_id: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    child_frame_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    frozen: Mapped[dict] = mapped_column(JSONB, nullable=False)
    frozen_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    absolute_deadline: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    envelope_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    result: Mapped[dict | None] = mapped_column(JSONB)
    result_digest: Mapped[str | None] = mapped_column(CHAR(64))
    observed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    accepted_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    __table_args__ = (
        UniqueConstraint("parent_frame_id", "parent_iteration", name="uq_delegation_parent_iteration"),
        ForeignKeyConstraint(["parent_frame_id", "parent_iteration"], ["runtime_loop_iterations.frame_id", "runtime_loop_iterations.iteration_no"]),
        ForeignKeyConstraint(["parent_frame_id", "run_id", "step_id"], ["runtime_loop_frames.frame_id", "runtime_loop_frames.run_id", "runtime_loop_frames.step_id"]),
        ForeignKeyConstraint(["child_frame_id", "run_id", "step_id"], ["runtime_loop_frames.frame_id", "runtime_loop_frames.run_id", "runtime_loop_frames.step_id"], deferrable=True, initially="DEFERRED"),
        CheckConstraint("child_frame_id <> parent_frame_id AND accepted_generation >= 0", name="ck_delegation_identity"),
        CheckConstraint("(result IS NULL AND result_digest IS NULL AND NOT observed) OR (result IS NOT NULL AND result_digest IS NOT NULL)", name="ck_delegation_result"),
    )


class RuntimeContextSnapshotRow(PersistenceBase):
    """接受后不可变的业务模型输入，不属于安全 RunSnapshot。"""
    __tablename__ = "runtime_context_snapshots"
    snapshot_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    frame_id: Mapped[str] = mapped_column(ForeignKey("runtime_loop_frames.frame_id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    step_id: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    validation: Mapped[dict] = mapped_column(JSONB, nullable=False)
    accepted_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    accepted_fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    __table_args__ = (
        UniqueConstraint("frame_id", "version", name="uq_context_snapshot_version"),
        UniqueConstraint("frame_id", "snapshot_id", "version", "snapshot_digest", name="uq_context_snapshot_binding"),
        ForeignKeyConstraint(["frame_id", "run_id", "step_id"], ["runtime_loop_frames.frame_id", "runtime_loop_frames.run_id", "runtime_loop_frames.step_id"], name="fk_context_snapshot_identity"),
        CheckConstraint("version > 0 AND accepted_generation >= 0 AND created_generation >= 0 AND accepted_fence >= 0", name="ck_context_snapshot_counters"),
    )


class RuntimeContextOperationRow(PersistenceBase):
    """一个有限压缩操作的工作记录；费用仅由 Model Owner 结算。"""
    __tablename__ = "runtime_context_operations"
    operation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    frame_id: Mapped[str] = mapped_column(ForeignKey("runtime_loop_frames.frame_id", ondelete="CASCADE"), nullable=False)
    target_iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    source_cursor: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(255))
    prepared: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'PREPARED'"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    candidate: Mapped[dict | None] = mapped_column(JSONB)
    validation: Mapped[dict | None] = mapped_column(JSONB)
    candidate_digest: Mapped[str | None] = mapped_column(CHAR(64))
    validation_digest: Mapped[str | None] = mapped_column(CHAR(64))
    result_digest: Mapped[str | None] = mapped_column(CHAR(64))
    reason: Mapped[str | None] = mapped_column(String(64))
    accepted_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("runtime_context_snapshots.snapshot_id"))
    __table_args__ = (
        UniqueConstraint("frame_id", "target_iteration", name="uq_context_operation_slot"),
        UniqueConstraint("frame_id", "operation_id", name="uq_context_operation_binding"),
        CheckConstraint("attempt_count BETWEEN 0 AND 2 AND target_iteration > 0 AND source_cursor >= 0", name="ck_context_operation_counters"),
        CheckConstraint("state IN ('PREPARED','RESULT_READY','ACCEPTED','REJECTED','FAILED')", name="ck_context_operation_state"),
    )


class RuntimeSnapshotRow(PersistenceBase):
    """Snapshot 持久检查点证据；不是 active-run recovery authority。"""

    __tablename__ = "runtime_snapshots"

    snapshot_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index(
            "ix_runtime_snapshots_run_created",
            "run_id",
            text("created_at DESC"),
        ),
        CheckConstraint(
            "snapshot_schema_version > 0",
            name="ck_runtime_snapshots_schema_version",
        ),
    )


class EventConsumptionCheckpointRow(PersistenceBase):
    """Runtime projection / local consumer 的消费进度 checkpoint。"""

    __tablename__ = "event_consumption_checkpoint"

    consumer_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    processed_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "consumer_id",
            "run_id",
            "sequence",
            name="uq_event_consumption_checkpoint_progress",
        ),
        Index(
            "ix_event_consumption_checkpoint_run",
            "consumer_id",
            "run_id",
        ),
        CheckConstraint(
            "sequence > 0", name="ck_event_consumption_checkpoint_sequence"
        ),
    )


# ---------------------------------------------------------------------------
# Memory persistence
# ---------------------------------------------------------------------------

_MESSAGE_ROLES = ("user", "assistant", "system")
_MEMORY_SCOPES_ALLOWED = ("direct", "orchestration")
_EXCHANGE_STATES = ("PENDING", "COMMITTED")
_SEMANTIC_STATUSES = ("ACTIVE", "SUPERSEDED", "DELETED")


class MemoryScopeRow(PersistenceBase):
    """Scope 是分区及 mutation 锁，不是独立 Memory 服务。"""
    __tablename__ = "memory_scopes"
    scope_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_user_id: Mapped[object | None] = mapped_column(Uuid(as_uuid=True))
    session_id: Mapped[str | None] = mapped_column(String(255))
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'ACTIVE'"))
    authz_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    grants: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    policy: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    __table_args__ = (
        ForeignKeyConstraint(["owner_user_id", "tenant_id"], ["users.id", "users.tenant_id"], name="fk_memory_scope_owner"),
        CheckConstraint("(scope_kind = 'SESSION' AND owner_user_id IS NOT NULL AND session_id IS NOT NULL AND project_id IS NULL) OR (scope_kind = 'USER' AND owner_user_id IS NOT NULL AND session_id IS NULL AND project_id IS NULL) OR (scope_kind = 'PROJECT' AND owner_user_id IS NULL AND session_id IS NULL AND project_id IS NOT NULL)", name="ck_memory_scope_partition"),
        CheckConstraint("authz_epoch > 0 AND row_version > 0 AND status IN ('ACTIVE','REVOKED')", name="ck_memory_scope_state"),
        Index("uq_memory_scope_partition", "tenant_id", "scope_kind", "agent_id", text("COALESCE(owner_user_id::text, '')"), text("COALESCE(session_id, '')"), text("COALESCE(project_id, '')"), unique=True),
    )


class MemoryOperationRow(PersistenceBase):
    """Formation/Mutation 身份及抑制凭据；不另建执行队列。"""
    __tablename__ = "memory_operations"
    operation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    scope_id: Mapped[str] = mapped_column(ForeignKey("memory_scopes.scope_id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_identity: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    candidate_digest: Mapped[str | None] = mapped_column(CHAR(64))
    candidate_body: Mapped[dict | None] = mapped_column(JSONB)
    dependencies: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    expected_version: Mapped[int | None] = mapped_column(BigInteger)
    result_version: Mapped[int | None] = mapped_column(BigInteger)
    record_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    receipt: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    __table_args__ = (
        UniqueConstraint("scope_id", "operation_id", name="uq_memory_operation_scope"),
        Index("ix_memory_operation_source", "scope_id", "source_identity"),
        Index("uq_memory_source_suppression", "scope_id", "source_identity", unique=True, postgresql_where=text("kind = 'SUPPRESS'")),
        CheckConstraint("state IN ('PREPARED','CANDIDATE','COMMITTED','CONFLICT','REJECTED','UNKNOWN','SUPPRESSED','SKIPPED_REVOKED')", name="ck_memory_operation_state"),
    )


class RuntimeMemoryBindingRow(PersistenceBase):
    """Planner 之前可保存；不依赖尚未建立的 execution root。"""
    __tablename__ = "runtime_memory_bindings"
    run_id: Mapped[str] = mapped_column(ForeignKey("runtime_run_control.run_id", ondelete="CASCADE"), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    access: Mapped[dict | None] = mapped_column(JSONB)
    scopes: Mapped[list] = mapped_column(JSONB, nullable=False)
    manifest: Mapped[list] = mapped_column(JSONB, nullable=False)
    manifest_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    earliest_expiry: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    seed_digest: Mapped[str | None] = mapped_column(CHAR(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'ACTIVE'"))
    invalid_code: Mapped[str | None] = mapped_column(String(64))
    admissions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    last_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    evidence_digest: Mapped[str | None] = mapped_column(CHAR(64))
    __table_args__ = (
        CheckConstraint("mode IN ('SCOPED','STATELESS') AND status IN ('ACTIVE','INVALID') AND row_version > 0", name="ck_runtime_memory_binding"),
    )


class MessageRow(PersistenceBase):
    """对话消息；``timestamp`` 由数据库 Authority 产生。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[object] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    metadata_json: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    memory_scope: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'direct'")
    )
    exchange_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # PostgreSQL-native 全文检索投影，替代 SQLite FTS5 external-content 表。
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', content)", persisted=True),
        nullable=True,
    )

    access_scope_id: Mapped[str | None] = mapped_column(String(255))
    record_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'REVOKED'"))
    body_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'PRESENT'"))
    body_digest: Mapped[str | None] = mapped_column(CHAR(64))
    source_identity: Mapped[str | None] = mapped_column(CHAR(64))
    source_dependencies: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    epistemic_kind: Mapped[str | None] = mapped_column(String(32))
    memory_policy_version: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        ForeignKeyConstraint(["access_scope_id"], ["memory_scopes.scope_id"], name="fk_messagerow_access_scope", use_alter=True),
        CheckConstraint("record_version > 0 AND lifecycle_status IN ('ACTIVE','EXPIRED','REVOKED','DELETED','SUPERSEDED','CANDIDATE') AND body_state IN ('PRESENT','REDACTED')", name="ck_messagerow_lifecycle"),

        CheckConstraint(
            "role IN ('user', 'assistant', 'system')",
            name="ck_messages_role",
        ),
        CheckConstraint(
            "memory_scope IN ('direct', 'orchestration')",
            name="ck_messages_memory_scope",
        ),
        # 一个 exchange 内 role 唯一（替代 SQLite 的 partial unique index）。
        Index(
            "uq_messages_exchange_role",
            "exchange_id",
            "role",
            unique=True,
            postgresql_where=text("exchange_id IS NOT NULL"),
        ),
        Index("ix_messages_agent_scope_time", "agent_id", "memory_scope", "timestamp", "id"),
        Index("ix_messages_agent_time", "agent_id", "timestamp", "id"),
        Index("ix_messages_scope_time", "memory_scope", "timestamp", "id"),
        Index("ix_messages_timestamp", "timestamp", "id"),
        Index(
            "ix_messages_search_vector",
            "search_vector",
            postgresql_using="gin",
        ),
    )


class ConversationSummaryRow(PersistenceBase):
    """每个 agent 的滚动对话摘要。"""

    __tablename__ = "conversation_summaries"

    summary_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    last_message_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    updated_at: Mapped[object] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    access_scope_id: Mapped[str | None] = mapped_column(String(255))
    record_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'REVOKED'"))
    body_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'PRESENT'"))
    body_digest: Mapped[str | None] = mapped_column(CHAR(64))
    source_identity: Mapped[str | None] = mapped_column(CHAR(64))
    source_dependencies: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    epistemic_kind: Mapped[str | None] = mapped_column(String(32))
    memory_policy_version: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        ForeignKeyConstraint(["access_scope_id"], ["memory_scopes.scope_id"], name="fk_conversationsummaryrow_access_scope", use_alter=True),
        CheckConstraint("record_version > 0 AND lifecycle_status IN ('ACTIVE','EXPIRED','REVOKED','DELETED','SUPERSEDED','CANDIDATE') AND body_state IN ('PRESENT','REDACTED')", name="ck_conversationsummaryrow_lifecycle"),
    )


class MessageExchangeRow(PersistenceBase):
    """原子 exchange 提交记录；是消息可见性的提交标记。"""

    __tablename__ = "message_exchanges"

    exchange_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    memory_scope: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'direct'")
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    user_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    assistant_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    access_scope_id: Mapped[str | None] = mapped_column(String(255))
    record_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'REVOKED'"))
    body_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'PRESENT'"))
    body_digest: Mapped[str | None] = mapped_column(CHAR(64))
    source_identity: Mapped[str | None] = mapped_column(CHAR(64))
    source_dependencies: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    epistemic_kind: Mapped[str | None] = mapped_column(String(32))
    memory_policy_version: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        ForeignKeyConstraint(["access_scope_id"], ["memory_scopes.scope_id"], name="fk_messageexchangerow_access_scope", use_alter=True),
        CheckConstraint("record_version > 0 AND lifecycle_status IN ('ACTIVE','EXPIRED','REVOKED','DELETED','SUPERSEDED','CANDIDATE') AND body_state IN ('PRESENT','REDACTED')", name="ck_messageexchangerow_lifecycle"),

        CheckConstraint(
            "state IN ('PENDING', 'COMMITTED')",
            name="ck_message_exchanges_state",
        ),
        CheckConstraint(
            "memory_scope IN ('direct', 'orchestration')",
            name="ck_message_exchanges_memory_scope",
        ),
        Index("ix_exchanges_state", "state"),
    )


class LongTermMemoryRow(PersistenceBase):
    """Long-term Memory（SEMANTIC / EPISODIC）。

    ``created_at`` / ``updated_at`` 保持既有 Domain-owned UTC ISO8601 TEXT
    contract：Advanced Memory Domain 以这些字符串作为身份与排序依据，换库
    不改写该 Contract。
    """

    __tablename__ = "long_term_memory"

    memory_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    memory_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_text: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    logical_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    origin_type: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    origin_exchange_id: Mapped[str] = mapped_column(String(255), nullable=False)
    origin_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    origin_memory_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    formation_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    superseded_by_memory_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    access_scope_id: Mapped[str | None] = mapped_column(String(255))
    record_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'REVOKED'"))
    body_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'PRESENT'"))
    body_digest: Mapped[str | None] = mapped_column(CHAR(64))
    source_identity: Mapped[str | None] = mapped_column(CHAR(64))
    source_dependencies: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    epistemic_kind: Mapped[str | None] = mapped_column(String(32))
    memory_policy_version: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        ForeignKeyConstraint(["access_scope_id"], ["memory_scopes.scope_id"], name="fk_longtermmemoryrow_access_scope", use_alter=True),
        CheckConstraint("record_version > 0 AND lifecycle_status IN ('ACTIVE','EXPIRED','REVOKED','DELETED','SUPERSEDED','CANDIDATE') AND body_state IN ('PRESENT','REDACTED')", name="ck_longtermmemoryrow_lifecycle"),

        Index("ix_long_term_memory_agent_scope", "agent_id", "memory_scope"),
        Index(
            "ix_long_term_memory_episodic_active_scope",
            "agent_id",
            "memory_scope",
            text("created_at DESC"),
            text("memory_id ASC"),
            postgresql_where=text(
                "memory_type = 'EPISODIC' AND status = 'ACTIVE'"
            ),
        ),
        # Episodic identity：RUN 级别与 STEP 级别各一条 partial unique index。
        # ``payload ->> 'episode_kind'`` 是 IMMUTABLE jsonb 取键，可安全用于
        # partial index predicate。
        Index(
            "uq_long_term_memory_episodic_run_identity",
            "memory_type",
            "origin_run_id",
            unique=True,
            postgresql_where=text(
                "memory_type = 'EPISODIC' "
                "AND COALESCE(payload ->> 'episode_kind', 'RUN') = 'RUN'"
            ),
        ),
        Index(
            "uq_long_term_memory_episodic_step_identity",
            "memory_type",
            "origin_run_id",
            "agent_id",
            text("(payload ->> 'origin_step_id')"),
            unique=True,
            postgresql_where=text(
                "memory_type = 'EPISODIC' "
                "AND payload ->> 'episode_kind' = 'STEP'"
            ),
        ),
    )


class ProjectSemanticMemoryRow(PersistenceBase):
    """Project-scoped semantic memory 分区；``promote`` 的来源/目标记录。"""

    __tablename__ = "project_semantic_memory"

    memory_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(255), nullable=False)
    logical_key: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_text: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    origin_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    origin_run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_by_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_memory_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_owner_agent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    promoted_by_agent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    promotion_run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    promotion_time: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_by_memory_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    access_scope_id: Mapped[str | None] = mapped_column(String(255))
    record_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'REVOKED'"))
    body_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'PRESENT'"))
    body_digest: Mapped[str | None] = mapped_column(CHAR(64))
    source_identity: Mapped[str | None] = mapped_column(CHAR(64))
    source_dependencies: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    epistemic_kind: Mapped[str | None] = mapped_column(String(32))
    memory_policy_version: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        ForeignKeyConstraint(["access_scope_id"], ["memory_scopes.scope_id"], name="fk_projectsemanticmemoryrow_access_scope", use_alter=True),
        CheckConstraint("record_version > 0 AND lifecycle_status IN ('ACTIVE','EXPIRED','REVOKED','DELETED','SUPERSEDED','CANDIDATE') AND body_state IN ('PRESENT','REDACTED')", name="ck_projectsemanticmemoryrow_lifecycle"),

        Index(
            "ix_project_semantic_active_scope",
            "project_id",
            "logical_key",
            text("created_at DESC"),
            text("memory_id ASC"),
            postgresql_where=text("visibility = 'PROJECT' AND status = 'ACTIVE'"),
        ),
        Index(
            "ix_project_semantic_partition",
            "project_id",
            "logical_key",
            text("created_at ASC"),
            text("memory_id ASC"),
        ),
    )


# Alembic 与 readiness 共用的 Canonical 表清单（不含 alembic_version）。
class RuntimeAgentEvaluationReceiptRow(PersistenceBase):
    """评测准入幂等事实；不授予 Runtime 恢复或执行租约。"""
    __tablename__ = "runtime_agent_evaluation_receipts"
    remote_run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    request_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluation_project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    evaluation_run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    correlation: Mapped[dict] = mapped_column(JSONB, nullable=False)
    execution_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    capture_profile: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evaluation_capture: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    received_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    sealed_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    sealed_digest: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    __table_args__ = (
        CheckConstraint("octet_length(correlation::text) <= 16384 AND octet_length(execution_manifest::text) <= 65536 AND octet_length(capture_profile::text) <= 16384 AND octet_length(evaluation_capture::text) <= 524288", name="ck_quality_receipt_metadata_limit"),
        CheckConstraint("(sealed_response IS NULL) = (sealed_digest IS NULL)", name="ck_quality_receipt_seal_pair"),
        CheckConstraint("sealed_response IS NULL OR octet_length(sealed_response) <= 2097152", name="ck_quality_receipt_response_limit"),
    )


CANONICAL_TABLES = (
    "feature_impact_tasks",
    "feature_impact_reports",
    "feature_impact_plans",
    "feature_impact_executions",
    "feature_impact_investigations",
    "nightly_ci_batches",
    "nightly_version_executions",
    "incident_groups",
    "incident_group_memberships",
    "user_notifications",
    "incident_actions",
    "incident_verifications",
    "ci_scenario_instances",
    "ci_executions",
    "incidents",
    "incident_investigations",
    "runtime_agent_evaluation_receipts",
    "stage8_feature_test_missions",
    "stage8_mission_run_refs",
    "stage8_business_reviews",
    "stage8_test_plans",
    "stage8_generated_case_artifacts",
    "stage8_external_execution_jobs",
    "stage8_ticket_continuations",
    "stage8_ci_runs",
    "stage8_ci_analysis",
    "tenants",
    "users",
    "roles",
    "user_roles",
    "object_ownership",
    "evaluation_jobs",
    "evaluation_results",
    "outbox_events",
    "runtime_event_journal",
    "client_delivery_events",
    "tool_resolution_snapshots",
    "runtime_run_control_commands",
    "runtime_run_control",
    "runtime_tool_approvals",
    "runtime_tool_execution_claims",
    "runtime_tool_invocations",
    "runtime_tool_manual_resolution_audit",
    "runtime_continuations",
    "runtime_run_executions",
    "runtime_step_executions",
    "runtime_model_invocations",
    "runtime_context_operations",
    "runtime_context_snapshots",
    "memory_operations",
    "memory_scopes",
    "runtime_memory_bindings",
    "runtime_loop_frames",
    "runtime_loop_iterations",
    "runtime_loop_actions",
    "runtime_agent_delegations",
    "runtime_snapshots",
    "event_consumption_checkpoint",
    "consumer_processed_events",
    "messages",
    "conversation_summaries",
    "message_exchanges",
    "long_term_memory",
    "project_semantic_memory",
)

# 后续 WP 的表仍不属于当前 Canonical schema。
TABLES_DEFERRED_TO_LATER_WORK_PACKAGES = ()

__all__ = [
    "CANONICAL_TABLES",
    "TABLES_DEFERRED_TO_LATER_WORK_PACKAGES",
    "ConversationSummaryRow",
    "ConsumerProcessedEventRow",
    "EvaluationJobRow",
    "EvaluationResultRow",
    "EventConsumptionCheckpointRow",
    "LongTermMemoryRow",
    "MessageExchangeRow",
    "MessageRow",
    "PersistenceBase",
    "RoleRow",
    "UserRoleRow",
    "ObjectOwnershipRow",
    "OutboxEventRow",
    "UserRow",
    "ProjectSemanticMemoryRow",
    "RuntimeEventJournalRow",
    "ToolResolutionSnapshotRow",
    "RunControlRow",
    "RunControlCommandRow",
    "DurableApprovalRow",
    "DurableToolExecutionClaimRow",
    "DurableToolInvocationRow",
    "ManualToolResolutionAuditRow",
    "DurableContinuationRow",
    "RuntimeSnapshotRow",
    "RuntimeLoopFrameRow",
    "RuntimeLoopIterationRow",
    "FeatureTestMissionRow",
    "MissionRunReferenceRow",
    "BusinessReviewRow",
    "Stage8TestPlanRow",
    "Stage8GeneratedCaseArtifactRow",
    "Stage8ExternalExecutionJobRow",
    "Stage8TicketContinuationRow",
    "Stage8CIRunRow",
    "Stage8CIAnalysisRow",
]
