"""Stage7-WP3 dedicated service principal and evaluation scope storage."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0009_wp3_service_principal"
down_revision: Union[str, Sequence[str], None] = "0008_wp2_durable_hitl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("principal_kind", sa.String(16), nullable=False, server_default="HUMAN"))
    op.add_column("users", sa.Column("service_scopes", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.create_check_constraint("ck_users_principal_kind", "users", "principal_kind IN ('HUMAN', 'SERVICE')")
    op.drop_constraint("ck_roles_code", "roles", type_="check")
    op.create_check_constraint("ck_roles_code", "roles", "code IN ('USER', 'OPERATOR', 'ADMIN', 'SERVICE')")
    op.bulk_insert(
        sa.table("roles", sa.column("id", postgresql.UUID()), sa.column("code", sa.String())),
        [{"id": "00000000-0000-0000-0000-000000000004", "code": "SERVICE"}],
    )


def downgrade() -> None:
    op.execute("DELETE FROM user_roles WHERE role_id = '00000000-0000-0000-0000-000000000004'")
    op.execute("DELETE FROM roles WHERE id = '00000000-0000-0000-0000-000000000004'")
    op.drop_constraint("ck_roles_code", "roles", type_="check")
    op.create_check_constraint("ck_roles_code", "roles", "code IN ('USER', 'OPERATOR', 'ADMIN')")
    op.drop_constraint("ck_users_principal_kind", "users", type_="check")
    op.drop_column("users", "service_scopes")
    op.drop_column("users", "principal_kind")
