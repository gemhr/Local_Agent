"""Stage6-WP2 durable object ownership binding."""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003_wp2_object_ownership"
down_revision: Union[str, Sequence[str], None] = "0002_wp2_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "object_ownership",
        sa.Column("object_type", sa.String(32), nullable=False),
        sa.Column("object_id", sa.String(255), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("object_type", "object_id", name="pk_object_ownership"),
        sa.CheckConstraint("object_type IN ('RUN', 'CONVERSATION')", name="ck_object_ownership_type"),
    )
    op.create_index("ix_object_ownership_owner_type", "object_ownership", ["owner_user_id", "object_type"])


def downgrade() -> None:
    op.drop_index("ix_object_ownership_owner_type", table_name="object_ownership")
    op.drop_table("object_ownership")
