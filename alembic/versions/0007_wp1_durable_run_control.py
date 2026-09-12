"""Stage7-WP1 durable Run control lease, fencing and cancel intent."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007_wp1_run_control"
down_revision: Union[str, Sequence[str], None] = "0006_wp6_observability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_run_control",
        sa.Column("run_id", sa.String(255), nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("state", sa.String(16), server_default=sa.text("'ACTIVE'"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("cancel_command_id", sa.String(255), nullable=True),
        sa.Column("cancel_reason", sa.String(64), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_sequence", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("state IN ('ACTIVE', 'CLOSED')", name="ck_runtime_run_control_state"),
        sa.CheckConstraint("fencing_token >= 0", name="ck_runtime_run_control_fencing_token"),
        sa.CheckConstraint("version > 0", name="ck_runtime_run_control_version"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_runtime_run_control_owner", "runtime_run_control", ["owner_id"])
    op.create_table(
        "runtime_run_control_commands",
        sa.Column("command_id", sa.String(255), nullable=False),
        sa.Column("run_id", sa.String(255), nullable=False),
        sa.Column("command_type", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("command_type = 'CANCEL'", name="ck_runtime_run_control_command_type"),
        sa.PrimaryKeyConstraint("command_id"),
        sa.UniqueConstraint("run_id", "command_type", name="uq_runtime_run_control_command_type"),
    )
    op.create_index("ix_runtime_run_control_commands_run", "runtime_run_control_commands", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_runtime_run_control_commands_run", table_name="runtime_run_control_commands")
    op.drop_table("runtime_run_control_commands")
    op.drop_index("ix_runtime_run_control_owner", table_name="runtime_run_control")
    op.drop_table("runtime_run_control")
