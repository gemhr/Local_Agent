"""Stage9-WP3 durable ToolResolutionSnapshot."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0019_stage9_wp3_tool_snapshot"
down_revision: Union[str, Sequence[str], None] = "0018_stage9_wp2_client_feed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_resolution_snapshots",
        sa.Column("run_id", sa.String(255), nullable=False),
        sa.Column("snapshot_id", sa.String(255), nullable=False),
        sa.Column("snapshot_digest", sa.CHAR(64), nullable=False),
        sa.Column("registry_digest", sa.CHAR(64), nullable=False),
        sa.Column("selection_algorithm_version", sa.String(64), nullable=False),
        sa.Column("snapshot_schema_version", sa.Integer(), nullable=False),
        sa.Column("selection_query_digest", sa.CHAR(64), nullable=False),
        sa.Column("tool_items", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id", name="pk_tool_resolution_snapshots"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runtime_run_control.run_id"],
            name="fk_tool_resolution_snapshots_run", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("snapshot_id", name="uq_tool_resolution_snapshots_snapshot_id"),
        sa.CheckConstraint(
            "snapshot_schema_version > 0",
            name="ck_tool_resolution_snapshot_schema_version",
        ),
    )


def downgrade() -> None:
    op.drop_table("tool_resolution_snapshots")
