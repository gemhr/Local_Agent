"""Stage7-WP2 durable approval and pre-execution claim."""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0008_wp2_durable_hitl"
down_revision: Union[str, Sequence[str], None] = "0007_wp1_run_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_tool_approvals",
        sa.Column("approval_id", sa.String(255), primary_key=True),
        sa.Column("run_id", sa.String(255), nullable=False),
        sa.Column("step_id", sa.String(255), nullable=False),
        sa.Column("invocation_id", sa.String(255), nullable=False),
        sa.Column("tool_name", sa.String(255), nullable=False),
        sa.Column("invocation_identity_digest", sa.String(64), nullable=False),
        sa.Column("arguments_digest", sa.String(64), nullable=False),
        sa.Column("idempotency_key_digest", sa.String(64)),
        sa.Column("resource_key_digest", sa.String(64)),
        sa.Column("invocation_binding_digest", sa.String(64), nullable=False),
        sa.Column("risk_level", sa.String(64)),
        sa.Column("risk_facts", sa.Text, server_default=sa.text("''"), nullable=False),
        sa.Column("state", sa.String(32), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("decision", sa.String(16)),
        sa.Column("actor_id_digest", sa.String(64)),
        sa.Column("invalidated_reason", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("invalidated_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.BigInteger, server_default=sa.text("1"), nullable=False),
        sa.UniqueConstraint("run_id", "invocation_id", name="uq_runtime_tool_approval_invocation"),
        sa.CheckConstraint("state IN ('PENDING', 'APPROVED', 'REJECTED', 'INVALIDATED')", name="ck_runtime_tool_approval_state"),
        sa.CheckConstraint("version > 0", name="ck_runtime_tool_approval_version"),
    )
    op.create_index("ix_runtime_tool_approvals_run_state", "runtime_tool_approvals", ["run_id", "state"])
    op.create_table(
        "runtime_tool_execution_claims",
        sa.Column("claim_id", sa.String(255), primary_key=True),
        sa.Column("approval_id", sa.String(255), nullable=False, unique=True),
        sa.Column("run_id", sa.String(255), nullable=False),
        sa.Column("invocation_binding_digest", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("fencing_token", sa.BigInteger, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("runtime_tool_execution_claims")
    op.drop_index("ix_runtime_tool_approvals_run_state", table_name="runtime_tool_approvals")
    op.drop_table("runtime_tool_approvals")
