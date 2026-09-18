"""Stage9-WP2 durable client event feed."""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0018_stage9_wp2_client_feed"
down_revision: Union[str, Sequence[str], None] = "0017_stage9_wp1_tenant_authz"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "client_delivery_events",
        sa.Column("run_id", sa.String(255), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("event_id", sa.String(255), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.PrimaryKeyConstraint("run_id", "cursor", name="pk_client_delivery_events"),
        sa.CheckConstraint("cursor > 0", name="ck_client_delivery_events_cursor"),
    )
    op.create_index("ix_client_delivery_events_run_cursor", "client_delivery_events", ["run_id", "cursor"])

def downgrade() -> None:
    op.drop_index("ix_client_delivery_events_run_cursor", table_name="client_delivery_events")
    op.drop_table("client_delivery_events")
