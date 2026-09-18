"""Stage9-WP4 generic durable continuation scheduling and lease."""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0020_stage9_wp4_cont"
down_revision: Union[str, Sequence[str], None] = "0019_stage9_wp3_tool_snapshot"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "runtime_continuations",
        sa.Column("continuation_id", sa.String(255), primary_key=True),
        sa.Column("run_id", sa.String(255), nullable=False),
        sa.Column("continuation_kind", sa.String(64), nullable=False),
        sa.Column("subject_type", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default=sa.text("'WAITING'")),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("payload_digest", sa.CHAR(64), nullable=False),
        sa.Column("claim_token", sa.String(255)),
        sa.Column("claimed_by", sa.String(255)),
        sa.Column("claim_deadline_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error_code", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["run_id"], ["runtime_run_control.run_id"], name="fk_runtime_continuations_run", ondelete="RESTRICT"),
        sa.CheckConstraint("state IN ('WAITING', 'READY', 'PROCESSING', 'SUCCEEDED', 'FAILED', 'CANCELLED')", name="ck_runtime_continuation_state"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_runtime_continuation_attempt_count"),
    )
    op.create_index("ix_runtime_continuations_ready", "runtime_continuations", ["state", "created_at"])
    op.create_index("ix_runtime_continuations_expired", "runtime_continuations", ["state", "claim_deadline_at"])
    op.create_index("ix_runtime_continuations_run", "runtime_continuations", ["run_id", "created_at"])

def downgrade() -> None:
    op.drop_table("runtime_continuations")
