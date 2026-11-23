"""新增 Workspace 空间及团队成员。

Revision ID: 0050_workspace_spaces
Revises: 0049_workflow_approval
"""
import sqlalchemy as sa
from alembic import op

revision = "0050_workspace_spaces"
down_revision = "0049_workflow_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "spaces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_spaces_id_tenant"),
        sa.ForeignKeyConstraint(
            ("owner_user_id", "tenant_id"), ("users.id", "users.tenant_id"),
            name="fk_spaces_owner_tenant", ondelete="RESTRICT",
        ),
        sa.CheckConstraint("kind IN ('PERSONAL', 'TEAM')", name="ck_spaces_kind"),
    )
    op.create_index(
        "uq_spaces_personal_owner", "spaces", ["tenant_id", "owner_user_id"], unique=True,
        postgresql_where=sa.text("kind = 'PERSONAL'"),
    )
    op.create_index("ix_spaces_tenant_owner", "spaces", ["tenant_id", "owner_user_id"])
    op.create_table(
        "space_members",
        sa.Column("space_id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ("space_id", "tenant_id"), ("spaces.id", "spaces.tenant_id"),
            name="fk_space_members_space_tenant", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("user_id", "tenant_id"), ("users.id", "users.tenant_id"),
            name="fk_space_members_user_tenant", ondelete="CASCADE",
        ),
        sa.CheckConstraint("role IN ('ADMIN', 'MEMBER')", name="ck_space_members_role"),
    )
    op.create_index("ix_space_members_tenant_user", "space_members", ["tenant_id", "user_id"])


def downgrade() -> None:
    op.drop_table("space_members")
    op.drop_table("spaces")
