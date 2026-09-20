"""Stage10-WP2 durable manual Tool resolution audit evidence."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0023_stage10_wp2_manual_audit"
down_revision: Union[str, Sequence[str], None] = "0022_stage10_wp2_unknown"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_tool_manual_resolution_audit",
        sa.Column("audit_id", sa.String(255), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("run_id", sa.String(255), nullable=False),
        sa.Column("step_id", sa.String(255), nullable=False),
        sa.Column("invocation_id", sa.String(255), nullable=False),
        sa.Column("tool_name", sa.String(255), nullable=False),
        sa.Column("resolution", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("audit_id"),
        sa.CheckConstraint(
            "resolution IN ('COMMITTED', 'NOT_COMMITTED')",
            name="ck_runtime_tool_manual_resolution_audit_resolution",
        ),
    )
    op.create_index(
        "ix_runtime_tool_manual_resolution_audit_invocation",
        "runtime_tool_manual_resolution_audit",
        ["invocation_id", "occurred_at"],
    )
    op.create_index(
        "ix_runtime_tool_manual_resolution_audit_run",
        "runtime_tool_manual_resolution_audit",
        ["run_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runtime_tool_manual_resolution_audit_run",
        table_name="runtime_tool_manual_resolution_audit",
    )
    op.drop_index(
        "ix_runtime_tool_manual_resolution_audit_invocation",
        table_name="runtime_tool_manual_resolution_audit",
    )
    op.drop_table("runtime_tool_manual_resolution_audit")
