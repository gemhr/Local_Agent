"""Stage8-WP3 external execution jobs and triage snapshots."""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0013_stage8_wp3"
down_revision: Union[str, Sequence[str], None] = "0012_stage8_wp1"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("stage8_business_reviews", sa.Column("subject_id", sa.String(255), nullable=True))
    op.create_table(
        "stage8_external_execution_jobs",
        sa.Column("job_id", sa.String(255), nullable=False),
        sa.Column("mission_id", sa.String(255), nullable=False),
        sa.Column("execution_id", sa.String(255), nullable=True),
        sa.Column("plan_id", sa.String(255), nullable=False),
        sa.Column("case_id", sa.String(255), nullable=False),
        sa.Column("environment_id", sa.String(255), nullable=False),
        sa.Column("executor_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("attempt_no", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("auto_repair_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("plan_payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("result_payload", postgresql.JSONB(), nullable=True),
        sa.Column("triage_payload", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["mission_id"], ["stage8_feature_test_missions.mission_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("execution_id", name="uq_stage8_external_execution_id"),
    )

def downgrade() -> None:
    op.drop_table("stage8_external_execution_jobs")
    op.drop_column("stage8_business_reviews", "subject_id")
