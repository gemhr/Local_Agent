"""Stage8-WP1 durable TestPlan subject."""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0012_stage8_wp1"
down_revision: Union[str, Sequence[str], None] = "0011_stage8_mission"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("stage8_test_plans",
        sa.Column("subject_id", sa.String(255), nullable=False),
        sa.Column("mission_id", sa.String(255), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("subject_digest", sa.CHAR(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["mission_id"], ["stage8_feature_test_missions.mission_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("subject_id"),
        sa.UniqueConstraint("mission_id", "subject_id", "version", name="uq_stage8_test_plan_version"))

def downgrade() -> None:
    op.drop_table("stage8_test_plans")
