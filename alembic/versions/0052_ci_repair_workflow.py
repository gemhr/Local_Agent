"""固定 CI 修复 Workflow 的仓库基线及 REPLAY 位置。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0052_ci_repair_workflow"
down_revision = "0051_workspace_execution"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workflow_runs", sa.Column("repair_baseline", JSONB()))
    op.drop_constraint("ck_workflow_runs_current_step", "workflow_runs", type_="check")
    op.create_check_constraint("ck_workflow_runs_current_step", "workflow_runs",
        "current_step BETWEEN 0 AND CASE WHEN repair_baseline IS NULL THEN 1 ELSE 2 END")
    op.drop_constraint("ck_workflow_step_runs_position", "workflow_step_runs", type_="check")
    op.create_check_constraint("ck_workflow_step_runs_position", "workflow_step_runs",
        "attempt = 1 AND (position BETWEEN 0 AND 1 OR (position = 2 AND runtime_kind = 'REPLAY')) "
        "AND (runtime_kind <> 'REPLAY' OR position = 2)")


def downgrade():
    op.drop_constraint("ck_workflow_step_runs_position", "workflow_step_runs", type_="check")
    op.create_check_constraint("ck_workflow_step_runs_position", "workflow_step_runs",
        "attempt = 1 AND position BETWEEN 0 AND 1")
    op.drop_constraint("ck_workflow_runs_current_step", "workflow_runs", type_="check")
    op.create_check_constraint("ck_workflow_runs_current_step", "workflow_runs", "current_step BETWEEN 0 AND 1")
    op.drop_column("workflow_runs", "repair_baseline")
