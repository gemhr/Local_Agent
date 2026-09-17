"""Stage8-WP10 durable ticket continuation."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0016_stage8_wp10_ticket"
down_revision: Union[str, Sequence[str], None] = "0015_stage8_wp7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stage8_ticket_continuations",
        sa.Column("continuation_id", sa.String(255), nullable=False),
        sa.Column("mission_id", sa.String(255), nullable=False),
        sa.Column("execution_job_id", sa.String(255), nullable=False),
        sa.Column("triage_id", sa.String(255), nullable=False),
        sa.Column("ticket_draft_id", sa.String(255), nullable=False),
        sa.Column("approval_id", sa.String(255), nullable=False),
        sa.Column("tool_invocation_id", sa.String(255), nullable=False),
        sa.Column("invocation_binding_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("request_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(32), server_default=sa.text("'PENDING_APPROVAL'"), nullable=False),
        sa.Column("external_ticket_id", sa.String(255), nullable=True),
        sa.Column("external_ticket_url", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.ForeignKeyConstraint(["mission_id"], ["stage8_feature_test_missions.mission_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["execution_job_id"], ["stage8_external_execution_jobs.job_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("continuation_id"),
        sa.UniqueConstraint("approval_id"),
        sa.UniqueConstraint("tool_invocation_id"),
        sa.UniqueConstraint("execution_job_id", "ticket_draft_id", name="uq_stage8_ticket_continuation_draft"),
        sa.CheckConstraint("state IN ('PENDING_APPROVAL', 'REJECTED', 'READY', 'PROCESSING', 'SUCCEEDED', 'FAILED', 'UNKNOWN')", name="ck_stage8_ticket_continuation_state"),
    )
    op.create_index("ix_stage8_ticket_continuations_state", "stage8_ticket_continuations", ["state"])


def downgrade() -> None:
    op.drop_index("ix_stage8_ticket_continuations_state", table_name="stage8_ticket_continuations")
    op.drop_table("stage8_ticket_continuations")
