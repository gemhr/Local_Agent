"""Stage7-WP5 durable Tool side-effect invocation aggregate."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0010_wp5_tool_idempotency"
down_revision: Union[str, Sequence[str], None] = "0009_wp3_service_principal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_tool_invocations",
        sa.Column("invocation_id", sa.String(255), nullable=False),
        sa.Column("run_id", sa.String(255), nullable=False),
        sa.Column("step_id", sa.String(255), nullable=False),
        sa.Column("tool_name", sa.String(255), nullable=False),
        sa.Column("invocation_binding_digest", sa.String(64), nullable=False),
        sa.Column("idempotency_key_digest", sa.String(64), nullable=False),
        sa.Column("resource_key_digest", sa.String(64), nullable=True),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("approval_id", sa.String(255), nullable=True),
        sa.Column("execution_claim_id", sa.String(255), nullable=True),
        sa.Column("state", sa.String(32), server_default=sa.text("'PREPARED'"), nullable=False),
        sa.Column("provider_operation_id", sa.String(255), nullable=True),
        sa.Column("uncertainty_reason", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unknown_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.PrimaryKeyConstraint("invocation_id"),
        sa.UniqueConstraint("run_id", "step_id", "invocation_id", name="uq_runtime_tool_invocation_identity"),
        sa.UniqueConstraint(
            "run_id",
            "tool_name",
            "idempotency_key_digest",
            name="uq_runtime_tool_invocation_idempotency",
        ),
        sa.CheckConstraint(
            "state IN ('PREPARED', 'STARTED', 'COMMITTED', 'UNKNOWN', 'NOT_COMMITTED')",
            name="ck_runtime_tool_invocation_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_runtime_tool_invocation_version"),
    )
    op.create_index(
        "ix_runtime_tool_invocations_run_state",
        "runtime_tool_invocations",
        ["run_id", "state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runtime_tool_invocations_run_state",
        table_name="runtime_tool_invocations",
    )
    op.drop_table("runtime_tool_invocations")
