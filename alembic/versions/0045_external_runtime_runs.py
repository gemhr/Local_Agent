"""持久化外部 Runtime 执行的身份和观察状态。"""

from alembic import op
import sqlalchemy as sa

revision = "0045_external_runtime_runs"
down_revision = "0044_oracle_reference_action"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "external_runtime_runs",
        sa.Column("run_id", sa.String(255), primary_key=True),
        sa.Column("trace_id", sa.String(255), nullable=False),
        sa.Column("instance_id", sa.String(64), nullable=False),
        sa.Column("target_digest", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(255), nullable=False),
        sa.Column("session_id", sa.String(255), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("agent_version", sa.String(64), nullable=False),
        sa.Column("external_session_id", sa.String(255)),
        sa.Column("user_message_id", sa.String(255)),
        sa.Column("assistant_message_id", sa.String(255)),
        sa.Column("observation_state", sa.String(16), nullable=False),
        sa.Column("terminal_status", sa.String(16)),
        sa.Column("stop_reason", sa.String(64)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("cancel_reason", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "observation_state IN ('CREATED', 'ACTIVE', 'UNKNOWN', 'TERMINAL')",
            name="ck_external_runtime_runs_observation_state",
        ),
        sa.CheckConstraint(
            "(observation_state = 'TERMINAL') = (terminal_status IS NOT NULL) "
            "AND (terminal_status IS NULL OR terminal_status IN ('SUCCEEDED', 'FAILED', 'CANCELLED'))",
            name="ck_external_runtime_runs_terminal",
        ),
    )


def downgrade():
    op.drop_table("external_runtime_runs")
