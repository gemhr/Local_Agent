"""Stage8-WP4 CI runs and durable guardian analysis."""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0014_stage8_wp4"
down_revision: Union[str, Sequence[str], None] = "0013_stage8_wp3"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("stage8_ci_runs",
        sa.Column("ci_run_id", sa.String(255), nullable=False), sa.Column("suite_id", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("branch", sa.String(255), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False), sa.PrimaryKeyConstraint("ci_run_id"))
    op.create_table("stage8_ci_analysis",
        sa.Column("analysis_id", sa.String(255), nullable=False), sa.Column("ci_run_id", sa.String(255), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False), sa.Column("digest", sa.CHAR(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["ci_run_id"], ["stage8_ci_runs.ci_run_id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("analysis_id"), sa.UniqueConstraint("ci_run_id"))

def downgrade() -> None:
    op.drop_table("stage8_ci_analysis")
    op.drop_table("stage8_ci_runs")
