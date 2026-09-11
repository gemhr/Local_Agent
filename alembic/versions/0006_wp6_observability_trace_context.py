"""Stage6-WP6 durable W3C trace context for transactional outbox."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006_wp6_observability"
down_revision: Union[str, Sequence[str], None] = "0005_wp5_kafka"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox_events", sa.Column("traceparent", sa.String(55), nullable=True))
    op.add_column("outbox_events", sa.Column("tracestate", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("outbox_events", "tracestate")
    op.drop_column("outbox_events", "traceparent")
