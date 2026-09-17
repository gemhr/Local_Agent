"""Stage8-WP7 durable generated Case artifacts."""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0015_stage8_wp7"
down_revision: Union[str, Sequence[str], None] = "0014_stage8_wp4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stage8_generated_case_artifacts",
        sa.Column("artifact_id", sa.String(255), nullable=False),
        sa.Column("mission_id", sa.String(255), nullable=False),
        sa.Column("test_plan_subject_id", sa.String(255), nullable=False),
        sa.Column("test_plan_version", sa.BigInteger(), nullable=False),
        sa.Column("test_plan_digest", sa.CHAR(64), nullable=False),
        sa.Column("scenario_id", sa.String(255), nullable=False),
        sa.Column("provider_case_id", sa.String(255), nullable=False),
        sa.Column("case_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), server_default=sa.text("'GENERATED'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["mission_id"], ["stage8_feature_test_missions.mission_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("artifact_id"),
        sa.UniqueConstraint("mission_id", "test_plan_subject_id", "test_plan_version", "test_plan_digest", "scenario_id", name="uq_stage8_generated_case_binding"),
    )


def downgrade() -> None:
    op.drop_table("stage8_generated_case_artifacts")
