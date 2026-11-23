"""持久化 Workflow 步骤后人工决定。"""

from alembic import op
import sqlalchemy as sa

revision = "0049_workflow_approval"
down_revision = "0048_workflow_artifact_handoff"
branch_labels = None
depends_on = None

_ORIGINAL_STATUSES = "'PENDING', 'RUNNING', 'PAUSED', 'CANCELLING', 'SUCCEEDED', 'FAILED', 'CANCELLED'"


def _set_workflow_statuses(values):
    op.drop_constraint("ck_workflow_runs_status", "workflow_runs", type_="check")
    op.create_check_constraint("ck_workflow_runs_status", "workflow_runs", f"status IN ({values})")


def upgrade():
    op.add_column("workflow_runs", sa.Column("approval_phase", sa.String(32), nullable=True))
    _set_workflow_statuses(_ORIGINAL_STATUSES + ", 'WAITING_APPROVAL', 'REJECTED'")
    op.create_table(
        "workflow_approvals",
        sa.Column("approval_id", sa.String(36), primary_key=True),
        sa.Column("workflow_run_id", sa.String(36), nullable=False),
        sa.Column("step_id", sa.String(64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("approval_phase", sa.String(32), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("binding_digest", sa.String(64), nullable=False),
        sa.Column("decided_by", sa.Uuid()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ("workflow_run_id", "step_id", "attempt"),
            ("workflow_step_runs.workflow_run_id", "workflow_step_runs.step_id", "workflow_step_runs.attempt"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workflow_run_id", name="uq_workflow_approvals_run"),
        sa.CheckConstraint("approval_phase = 'AFTER_STEP_OUTPUT'", name="ck_workflow_approvals_phase"),
        sa.CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED', 'INVALIDATED')", name="ck_workflow_approvals_status"),
        sa.CheckConstraint("consumed_at IS NULL OR status = 'APPROVED'", name="ck_workflow_approvals_consumed"),
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM workflow_runs WHERE approval_phase IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("存在审批 Workflow 记录，不能回退并移除其审批约束和审计")
    op.drop_table("workflow_approvals")
    _set_workflow_statuses(_ORIGINAL_STATUSES)
    op.drop_column("workflow_runs", "approval_phase")
