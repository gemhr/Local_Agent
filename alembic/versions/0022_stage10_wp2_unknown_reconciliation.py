"""Stage10-WP2 durable UNKNOWN reconciliation scheduling metadata."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0022_stage10_wp2_unknown"
down_revision: Union[str, Sequence[str], None] = "0021_stage10_wp1_exec"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runtime_tool_invocations", sa.Column("reconcile_attempt_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")))
    op.add_column("runtime_tool_invocations", sa.Column("last_reconcile_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runtime_tool_invocations", sa.Column("last_safe_error_code", sa.String(128), nullable=True))
    op.add_column("runtime_tool_invocations", sa.Column("next_reconcile_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runtime_tool_invocations", sa.Column("manual_required", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.create_check_constraint("ck_runtime_tool_invocation_reconcile_attempts", "runtime_tool_invocations", "reconcile_attempt_count >= 0")
    op.create_index("ix_runtime_tool_invocations_reconcile", "runtime_tool_invocations", ["state", "next_reconcile_at", "manual_required"])


def downgrade() -> None:
    op.drop_index("ix_runtime_tool_invocations_reconcile", table_name="runtime_tool_invocations")
    op.drop_constraint("ck_runtime_tool_invocation_reconcile_attempts", "runtime_tool_invocations", type_="check")
    for name in ("manual_required", "next_reconcile_at", "last_safe_error_code", "last_reconcile_at", "reconcile_attempt_count"):
        op.drop_column("runtime_tool_invocations", name)
