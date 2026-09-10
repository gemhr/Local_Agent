"""Stage6-WP2 identity schema."""
from __future__ import annotations

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002_wp2_identity"
down_revision: Union[str, Sequence[str], None] = "19d1ccbe8526"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.UniqueConstraint("subject", name="uq_users_subject"),
    )
    op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.UniqueConstraint("code", name="uq_roles_code"),
        sa.CheckConstraint("code IN ('USER', 'OPERATOR', 'ADMIN')", name="ck_roles_code"),
    )
    op.create_table(
        "user_roles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    )
    roles = sa.table("roles", sa.column("id", postgresql.UUID()), sa.column("code", sa.String()))
    op.bulk_insert(roles, [
        {"id": "00000000-0000-0000-0000-000000000001", "code": "USER"},
        {"id": "00000000-0000-0000-0000-000000000002", "code": "OPERATOR"},
        {"id": "00000000-0000-0000-0000-000000000003", "code": "ADMIN"},
    ])


def downgrade() -> None:
    op.drop_table("user_roles")
    op.drop_table("roles")
    op.drop_table("users")
