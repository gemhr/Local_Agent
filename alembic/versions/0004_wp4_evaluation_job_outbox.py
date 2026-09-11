"""Stage6-WP4 durable evaluation jobs and transactional outbox."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0004_wp4_evaluation_job_outbox"
down_revision: Union[str, Sequence[str], None] = "0003_wp2_object_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluation_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("evaluator_kind", sa.String(length=64), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'QUEUED'"), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_message", sa.String(length=512), nullable=True),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_evaluation_jobs_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_evaluation_jobs_attempt"),
        sa.CheckConstraint("version >= 1", name="ck_evaluation_jobs_version"),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_jobs"),
    )
    op.create_index(
        "ix_evaluation_jobs_active",
        "evaluation_jobs",
        ["status"],
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )
    op.create_index(
        "ix_evaluation_jobs_owner_created",
        "evaluation_jobs",
        ["owner_user_id", sa.literal_column("created_at DESC")],
    )

    op.create_table(
        "evaluation_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evaluation_jobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_results"),
        sa.UniqueConstraint("job_id", name="uq_evaluation_results_job_id"),
    )

    op.create_table(
        "outbox_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("claim_owner", sa.String(length=128), nullable=True),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claim_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint("schema_version > 0", name="ck_outbox_events_schema_version"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_outbox_events_attempt_count"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PUBLISHED')", name="ck_outbox_events_status"
        ),
        sa.CheckConstraint(
            "(claim_owner IS NULL AND claim_token IS NULL AND claim_deadline IS NULL) "
            "OR (claim_owner IS NOT NULL AND claim_token IS NOT NULL AND claim_deadline IS NOT NULL)",
            name="ck_outbox_events_claim_tuple",
        ),
        sa.CheckConstraint(
            "(status = 'PENDING' AND published_at IS NULL) "
            "OR (status = 'PUBLISHED' AND published_at IS NOT NULL)",
            name="ck_outbox_events_publication_state",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_outbox_events"),
    )
    op.create_index(
        "ix_outbox_events_pending_available",
        "outbox_events",
        ["available_at", "created_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outbox_events_pending_available", table_name="outbox_events"
    )
    op.drop_table("outbox_events")
    op.drop_table("evaluation_results")
    op.drop_index(
        "ix_evaluation_jobs_owner_created", table_name="evaluation_jobs"
    )
    op.drop_index("ix_evaluation_jobs_active", table_name="evaluation_jobs")
    op.drop_table("evaluation_jobs")
