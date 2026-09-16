"""Stage8-WP0 mission and business review foundation."""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0011_stage8_mission"
down_revision: Union[str, Sequence[str], None] = "0010_wp5_tool_idempotency"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("stage8_feature_test_missions",
        sa.Column("mission_id", sa.String(255), nullable=False),
        sa.Column("feature_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("title", sa.String(255), nullable=True), sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("mission_id"), sa.CheckConstraint("version > 0", name="ck_stage8_mission_version"))
    op.create_table("stage8_mission_run_refs",
        sa.Column("reference_id", sa.String(255), nullable=False), sa.Column("mission_id", sa.String(255), nullable=False),
        sa.Column("run_id", sa.String(255), nullable=False), sa.Column("run_purpose", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["mission_id"], ["stage8_feature_test_missions.mission_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("reference_id"), sa.UniqueConstraint("mission_id", "run_id", "run_purpose", name="uq_stage8_mission_run_ref"))
    op.create_table("stage8_business_reviews",
        sa.Column("review_id", sa.String(255), nullable=False), sa.Column("mission_id", sa.String(255), nullable=False),
        sa.Column("review_type", sa.String(64), nullable=False), sa.Column("subject_version", sa.BigInteger(), nullable=True),
        sa.Column("subject_digest", sa.CHAR(64), nullable=True), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True), sa.Column("decided_by", sa.String(255), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["mission_id"], ["stage8_feature_test_missions.mission_id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("review_id"))

def downgrade() -> None:
    op.drop_table("stage8_business_reviews"); op.drop_table("stage8_mission_run_refs"); op.drop_table("stage8_feature_test_missions")
