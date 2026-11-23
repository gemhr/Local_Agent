"""持久化串行 Workflow 版本、运行和步骤身份。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0046_workflow_execution"
down_revision = "0045_external_runtime_runs"
branch_labels = None
depends_on = None

_OBJECT_TYPES = (
    "'RUN', 'CONVERSATION', 'MISSION', 'REVIEW', 'TEST_PLAN', 'ARTIFACT', "
    "'GENERATED_CASE_ARTIFACT', 'EXTERNAL_EXECUTION_JOB', 'APPROVAL', "
    "'TICKET_CONTINUATION', 'EVALUATION_JOB'"
)


def _ownership_types(types):
    op.drop_constraint("ck_object_ownership_type", "object_ownership", type_="check")
    op.create_check_constraint("ck_object_ownership_type", "object_ownership", f"object_type IN ({types})")


def upgrade():
    _ownership_types(_OBJECT_TYPES + ", 'WORKFLOW_RUN'")
    op.create_table(
        "workflow_versions",
        sa.Column("workflow_id", sa.String(64), primary_key=True),
        sa.Column("workflow_version", sa.String(64), primary_key=True),
        sa.Column("definition_digest", sa.String(64), nullable=False),
    )
    op.create_table(
        "workflow_runs",
        sa.Column("workflow_run_id", sa.String(36), primary_key=True),
        sa.Column("workflow_id", sa.String(64), nullable=False),
        sa.Column("workflow_version", sa.String(64), nullable=False),
        sa.Column("definition_digest", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("current_step", sa.Integer(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(("workflow_id", "workflow_version"), ("workflow_versions.workflow_id", "workflow_versions.workflow_version")),
        sa.UniqueConstraint("tenant_id", "owner_user_id", "request_id", name="uq_workflow_runs_request"),
        sa.CheckConstraint("status IN ('PENDING', 'RUNNING', 'PAUSED', 'CANCELLING', 'SUCCEEDED', 'FAILED', 'CANCELLED')", name="ck_workflow_runs_status"),
        sa.CheckConstraint("current_step BETWEEN 0 AND 1", name="ck_workflow_runs_current_step"),
    )
    op.create_table(
        "workflow_step_runs",
        sa.Column("workflow_run_id", sa.String(36), sa.ForeignKey("workflow_runs.workflow_run_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("step_id", sa.String(64), primary_key=True),
        sa.Column("attempt", sa.Integer(), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("platform_run_id", sa.String(255)),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("agent_version", sa.String(64), nullable=False),
        sa.Column("runtime_kind", sa.String(16), nullable=False),
        sa.Column("runtime_instance_id", sa.String(64)),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("dispatch_token", sa.String(36)),
        sa.Column("runtime_status", sa.String(16)),
        sa.Column("result_ref", JSONB()),
        sa.Column("reason", sa.String(64)),
        sa.UniqueConstraint("workflow_run_id", "position", name="uq_workflow_step_runs_position"),
        sa.UniqueConstraint("platform_run_id", name="uq_workflow_step_runs_platform_run"),
        sa.CheckConstraint("attempt = 1 AND position BETWEEN 0 AND 1", name="ck_workflow_step_runs_position"),
        sa.CheckConstraint("status IN ('PENDING', 'RESERVED', 'SUCCEEDED', 'FAILED', 'CANCELLED')", name="ck_workflow_step_runs_status"),
    )


def downgrade():
    op.drop_table("workflow_step_runs")
    op.drop_table("workflow_runs")
    op.drop_table("workflow_versions")
    op.execute("DELETE FROM object_ownership WHERE object_type = 'WORKFLOW_RUN'")
    _ownership_types(_OBJECT_TYPES)
