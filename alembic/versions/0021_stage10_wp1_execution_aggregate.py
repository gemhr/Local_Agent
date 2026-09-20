"""Stage10-WP1 canonical durable execution aggregate."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0021_stage10_wp1_exec"
down_revision: Union[str, Sequence[str], None] = "0020_stage9_wp4_cont"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Recovery-only identity/result columns on the existing Tool aggregate.
    # They are nullable so this migration remains valid for pre-Stage10 rows;
    # Tool Runtime owns populating them on subsequent mutations.
    op.add_column("runtime_tool_invocations", sa.Column("arguments_digest", sa.CHAR(64), nullable=True))
    op.add_column("runtime_tool_invocations", sa.Column("committed_result", postgresql.JSONB(), nullable=True))
    op.add_column("runtime_tool_invocations", sa.Column("committed_result_digest", sa.CHAR(64), nullable=True))
    op.create_index(
        "ix_runtime_tool_invocations_recovery_identity",
        "runtime_tool_invocations",
        ["run_id", "step_id", "tool_name", "arguments_digest"],
    )
    op.create_table(
        "runtime_run_executions",
        sa.Column("run_id", sa.String(255), sa.ForeignKey("runtime_run_control.run_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("execution_version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("resume_input", postgresql.JSONB(), nullable=False),
        sa.Column("plan_payload", postgresql.JSONB(), nullable=False),
        sa.Column("plan_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'ACTIVE'")),
        sa.Column("stop_reason", sa.String(64)),
        sa.Column("final_result_binding", postgresql.JSONB()),
        sa.Column("absolute_deadline", sa.DateTime(timezone=True)),
        sa.Column("budget_totals", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("budget_reserved", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("budget_consumed", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("recovery_supported", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("recovery_attempt_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_recovery_at", sa.DateTime(timezone=True)),
        sa.Column("last_recovery_error", sa.String(128)),
        sa.Column("next_recovery_at", sa.DateTime(timezone=True)),
        sa.Column("manual_required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_committed_fencing_token", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("schema_version > 0", name="ck_runtime_run_execution_schema_version"),
        sa.CheckConstraint("execution_version > 0", name="ck_runtime_run_execution_version"),
        sa.CheckConstraint("plan_version > 0", name="ck_runtime_run_execution_plan_version"),
        sa.CheckConstraint("status IN ('ACTIVE', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'BLOCKED')", name="ck_runtime_run_execution_status"),
        sa.CheckConstraint("recovery_attempt_count >= 0", name="ck_runtime_run_execution_recovery_attempts"),
        sa.CheckConstraint("last_committed_fencing_token >= 0", name="ck_runtime_run_execution_fencing_token"),
    )
    op.create_index("ix_runtime_run_executions_recovery", "runtime_run_executions", ["status", "recovery_supported", "next_recovery_at"])

    op.create_table(
        "runtime_step_executions",
        sa.Column("run_id", sa.String(255), primary_key=True),
        sa.Column("step_id", sa.String(255), primary_key=True),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column("current_attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("execution_kind", sa.String(32), nullable=False),
        sa.Column("risk_classification", sa.String(32), nullable=False),
        sa.Column("typed_result_payload", postgresql.JSONB()),
        sa.Column("result_digest", sa.CHAR(64)),
        sa.Column("safe_error", sa.String(128)),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_committed_fencing_token", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["run_id"], ["runtime_run_executions.run_id"], name="fk_runtime_step_execution_run", ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "step_id", name="uq_runtime_step_execution_identity"),
        sa.CheckConstraint("current_attempt >= 0", name="ck_runtime_step_execution_attempt"),
        sa.CheckConstraint("plan_version > 0", name="ck_runtime_step_execution_plan_version"),
        sa.CheckConstraint("version > 0", name="ck_runtime_step_execution_version"),
        sa.CheckConstraint("status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'BLOCKED', 'SKIPPED')", name="ck_runtime_step_execution_status"),
        sa.CheckConstraint("last_committed_fencing_token >= 0", name="ck_runtime_step_execution_fencing_token"),
    )
    op.create_index("ix_runtime_step_executions_run_status", "runtime_step_executions", ["run_id", "status"])

    op.create_table(
        "runtime_model_invocations",
        sa.Column("run_id", sa.String(255), primary_key=True),
        sa.Column("step_id", sa.String(255), primary_key=True),
        sa.Column("attempt_number", sa.Integer(), primary_key=True),
        sa.Column("model_attempt_number", sa.Integer(), primary_key=True),
        sa.Column("request_digest", sa.CHAR(64), nullable=False),
        sa.Column("provider_kind", sa.String(64), nullable=False),
        sa.Column("profile_identity", sa.String(255), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default=sa.text("'NOT_STARTED'")),
        sa.Column("result_binding", postgresql.JSONB()),
        sa.Column("result_digest", sa.CHAR(64)),
        sa.Column("usage_evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("cost_evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("safe_error", sa.String(128)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_committed_fencing_token", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["run_id"], ["runtime_run_executions.run_id"], name="fk_runtime_model_invocation_run", ondelete="CASCADE"),
        sa.CheckConstraint("attempt_number > 0", name="ck_runtime_model_attempt_number"),
        sa.CheckConstraint("model_attempt_number > 0", name="ck_runtime_model_model_attempt_number"),
        sa.CheckConstraint("state IN ('NOT_STARTED', 'STARTED', 'COMPLETED', 'UNKNOWN')", name="ck_runtime_model_state"),
        sa.CheckConstraint("last_committed_fencing_token >= 0", name="ck_runtime_model_fencing_token"),
    )
    op.create_index("ix_runtime_model_invocations_run_state", "runtime_model_invocations", ["run_id", "state"])


def downgrade() -> None:
    op.drop_index("ix_runtime_tool_invocations_recovery_identity", table_name="runtime_tool_invocations")
    op.drop_column("runtime_tool_invocations", "committed_result_digest")
    op.drop_column("runtime_tool_invocations", "committed_result")
    op.drop_column("runtime_tool_invocations", "arguments_digest")
    op.drop_table("runtime_model_invocations")
    op.drop_table("runtime_step_executions")
    op.drop_table("runtime_run_executions")
