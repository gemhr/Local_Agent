"""新增不可变执行 envelope，不回填历史输入、deadline 或 receipt。"""

from alembic import op

revision = "0029_stage13_execution_policy"
down_revision = "0028_stage13_wp04c_evidence"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE stage13_triage_runs ADD COLUMN execution_policy JSONB")


def downgrade():
    raise RuntimeError("STAGE13_EXECUTION_HISTORY_DOWNGRADE_FORBIDDEN")
