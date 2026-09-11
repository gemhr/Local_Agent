"""Stage6-WP5 Kafka worker claims and PostgreSQL consumer dedup."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005_wp5_kafka"
down_revision: Union[str, Sequence[str], None] = "0004_wp4_evaluation_job_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("evaluation_jobs", sa.Column("worker_claim_owner", sa.String(128), nullable=True))
    op.add_column("evaluation_jobs", sa.Column("worker_claim_token", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("evaluation_jobs", sa.Column("worker_claim_deadline", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_evaluation_jobs_worker_claim_tuple",
        "evaluation_jobs",
        "(worker_claim_owner IS NULL AND worker_claim_token IS NULL AND worker_claim_deadline IS NULL) "
        "OR (worker_claim_owner IS NOT NULL AND worker_claim_token IS NOT NULL AND worker_claim_deadline IS NOT NULL)",
    )
    op.create_index(
        "ix_evaluation_jobs_worker_claim_deadline",
        "evaluation_jobs",
        ["worker_claim_deadline"],
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.create_table(
        "consumer_processed_events",
        sa.Column("consumer_name", sa.String(128), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic", sa.String(255), nullable=False),
        sa.Column("partition", sa.Integer(), nullable=False),
        sa.Column("offset", sa.BigInteger(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('SUCCEEDED', 'FAILED', 'CANCELLED_NOOP', 'TERMINAL_NOOP', 'DUPLICATE')",
            name="ck_consumer_processed_events_outcome",
        ),
        sa.PrimaryKeyConstraint("consumer_name", "event_id", name="pk_consumer_processed_events"),
    )
    op.create_index("ix_consumer_processed_events_event", "consumer_processed_events", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_consumer_processed_events_event", table_name="consumer_processed_events")
    op.drop_table("consumer_processed_events")
    op.drop_index("ix_evaluation_jobs_worker_claim_deadline", table_name="evaluation_jobs")
    op.drop_constraint("ck_evaluation_jobs_worker_claim_tuple", "evaluation_jobs", type_="check")
    op.drop_column("evaluation_jobs", "worker_claim_deadline")
    op.drop_column("evaluation_jobs", "worker_claim_token")
    op.drop_column("evaluation_jobs", "worker_claim_owner")
