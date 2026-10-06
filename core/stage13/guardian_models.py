"""WP02 业务持久化；独立于 Provider 和 generic Runtime 状态机。"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class GuardianBase(DeclarativeBase):
    pass


class GuardianRow(GuardianBase):
    __tablename__ = "stage13_guardians"
    guardian_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    guardian_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    project: Mapped[str] = mapped_column(String(128), nullable=False)
    suite: Mapped[str] = mapped_column(String(128), nullable=False)
    environment: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE','DISABLED')", name="ck_s13_guardian_status"
        ),
    )


class CycleRow(GuardianBase):
    __tablename__ = "stage13_daily_cycles"
    cycle_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cycle_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    guardian_id: Mapped[str] = mapped_column(
        ForeignKey("stage13_guardians.guardian_id"), nullable=False
    )
    business_date: Mapped[str] = mapped_column(String(10), nullable=False)
    plan: Mapped[dict] = mapped_column(JSONB, nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(32), nullable=False)
    eligible_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("guardian_id", "business_date", name="uq_s13_cycle_day"),
        CheckConstraint(
            "status IN ('CREATED','READY','RUNNING','SUCCEEDED','COMPLETED_WITH_FAILURES','FAILED','UNRESOLVED','CANCELLED','SKIPPED_OVERLAP')",
            name="ck_s13_cycle_status",
        ),
    )


class VersionRow(GuardianBase):
    __tablename__ = "stage13_version_executions"
    version_execution_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_execution_key: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("stage13_daily_cycles.cycle_id"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    product_version: Mapped[str] = mapped_column(String(128), nullable=False)
    expected_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)
    business_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64))
    binding_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="UNBOUND"
    )
    knowledge_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="KNOWN"
    )
    remote_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    receipt: Mapped[dict | None] = mapped_column(JSONB)
    intent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submit_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reconciliation_requests: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    reconciliation_errors: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    unknown_entries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unknown_recoveries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_unknown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_poll_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_observed_state: Mapped[str | None] = mapped_column(String(32))
    last_observed_digest: Mapped[str | None] = mapped_column(String(64))
    last_result_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    last_status_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    poll_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    poll_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successful_polls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_poll_errors: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    result_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    counts: Mapped[dict | None] = mapped_column(JSONB)
    terminal_summary: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        UniqueConstraint("cycle_id", "ordinal", name="uq_s13_cycle_ordinal"),
        CheckConstraint(
            "ordinal BETWEEN 1 AND 3 AND expected_cases > 0", name="ck_s13_version_plan"
        ),
        CheckConstraint(
            "submit_attempts BETWEEN 0 AND 3 AND reconciliation_requests BETWEEN 0 AND 60 AND result_retries BETWEEN 0 AND 3",
            name="ck_s13_attempt_budget",
        ),
        CheckConstraint(
            "binding_status IN ('UNBOUND','BOUND') AND knowledge_state IN ('KNOWN','UNKNOWN')",
            name="ck_s13_knowledge",
        ),
        CheckConstraint(
            "(binding_status='UNBOUND' AND remote_id IS NULL AND receipt IS NULL) OR (binding_status='BOUND' AND remote_id IS NOT NULL AND receipt IS NOT NULL)",
            name="ck_s13_binding",
        ),
        CheckConstraint(
            "status IN ('PLANNED','DISPATCHING','ACTIVE','COMPLETED','INFRA_FAILED','DISPATCH_FAILED','UNRESOLVED','CANCELLED','SKIPPED')",
            name="ck_s13_version_status",
        ),
    )


class OccupancyRow(GuardianBase):
    __tablename__ = "stage13_environment_occupancy"
    scope: Mapped[str] = mapped_column(String(128), primary_key=True)
    environment: Mapped[str] = mapped_column(String(128), primary_key=True)
    cycle_id: Mapped[str | None] = mapped_column(
        ForeignKey("stage13_daily_cycles.cycle_id")
    )
    safety_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hold_acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolutions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)


class DueRow(GuardianBase):
    __tablename__ = "stage13_due_work"
    work_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    version_execution_id: Mapped[str] = mapped_column(
        ForeignKey("stage13_version_executions.version_execution_id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    eligible_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    original_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    next_available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="READY")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(36))
    claim_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    business_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovery_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    takeovers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint(
            "version_execution_id", "operation", "sequence", name="uq_s13_due_operation"
        ),
        CheckConstraint(
            "operation IN ('DISPATCH_VERSION','POLL_REMOTE','RECONCILE_REMOTE','FETCH_TERMINAL_RESULT') AND state IN ('READY','CLAIMED','COMPLETED')",
            name="ck_s13_due_state",
        ),
        Index(
            "ix_s13_due_candidate",
            "scope",
            "next_available_at",
            "work_key",
            postgresql_where=text("completed_at IS NULL"),
        ),
        Index(
            "ix_s13_due_lease",
            "scope",
            "lease_until",
            postgresql_where=text("state='CLAIMED'"),
        ),
    )


class ObservationRow(GuardianBase):
    __tablename__ = "stage13_observations"
    work_key: Mapped[str] = mapped_column(
        ForeignKey("stage13_due_work.work_key"), primary_key=True
    )
    version_execution_id: Mapped[str] = mapped_column(
        ForeignKey("stage13_version_executions.version_execution_id"), nullable=False
    )
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    business_read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    semantic_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    packet_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    changed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # unchanged read 只留 ledger，不重复存 evidence body。
    evidence: Mapped[dict | None] = mapped_column(JSONB)


class SchedulerRow(GuardianBase):
    __tablename__ = "stage13_scheduler_state"
    scope: Mapped[str] = mapped_column(String(128), primary_key=True)
    logical_now: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submit_starts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    read_starts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    recovery_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    recovery_id: Mapped[str | None] = mapped_column(String(128))
    stale_writes_rejected: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )


GUARDIAN_TABLES = tuple(GuardianBase.metadata.tables)
