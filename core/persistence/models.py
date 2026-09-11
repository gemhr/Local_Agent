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
    BigInteger,
    CHAR,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
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


class UserRow(PersistenceBase):
    __tablename__ = "users"
    id: Mapped[object] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    disabled_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))


class RoleRow(PersistenceBase):
    __tablename__ = "roles"
    id: Mapped[object] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)

    __table_args__ = (
        CheckConstraint(
            "code IN ('USER', 'OPERATOR', 'ADMIN')",
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
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "object_type IN ('RUN', 'CONVERSATION')",
            name="ck_object_ownership_type",
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

    __table_args__ = (
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

    agent_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    last_message_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    updated_at: Mapped[object] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
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

    __table_args__ = (
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

    __table_args__ = (
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

    __table_args__ = (
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
CANONICAL_TABLES = (
    "users",
    "roles",
    "user_roles",
    "object_ownership",
    "evaluation_jobs",
    "evaluation_results",
    "outbox_events",
    "runtime_event_journal",
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
    "RuntimeSnapshotRow",
]
