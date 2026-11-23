"""保存 Workflow 产物引用和正式下游输入。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0048_workflow_artifact_handoff"
down_revision = "0047_workflow_target_binding"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workflow_runs", sa.Column("handoff_input", JSONB(), nullable=True))
    op.add_column("workflow_step_runs", sa.Column("artifact_ref", JSONB(), nullable=True))
    op.add_column("workflow_step_runs", sa.Column("artifact_directory", JSONB(), nullable=True))
    op.add_column("workflow_step_runs", sa.Column("bound_input", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("workflow_step_runs", "bound_input")
    op.drop_column("workflow_step_runs", "artifact_ref")
    op.drop_column("workflow_step_runs", "artifact_directory")
    op.drop_column("workflow_runs", "handoff_input")
