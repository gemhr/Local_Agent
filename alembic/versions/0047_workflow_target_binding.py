"""将外部实例执行配置摘要固定在子 Run 预留记录中。"""

from alembic import op
import sqlalchemy as sa

revision = "0047_workflow_target_binding"
down_revision = "0046_workflow_execution"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workflow_step_runs", sa.Column("runtime_target_digest", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("workflow_step_runs", "runtime_target_digest")
