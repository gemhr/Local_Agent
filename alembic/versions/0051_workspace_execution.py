"""新增 Workspace 配置、发布快照及执行归属。

Revision ID: 0051_workspace_execution
Revises: 0050_workspace_spaces
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0051_workspace_execution"
down_revision = "0050_workspace_spaces"
branch_labels = None
depends_on = None


def _scope():
    return (
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(("space_id", "tenant_id"), ("spaces.id", "spaces.tenant_id")),
        sa.ForeignKeyConstraint(("owner_user_id", "tenant_id"), ("users.id", "users.tenant_id")),
    )


def upgrade():
    op.create_table(
        "workspace_agents", sa.Column("id", sa.Uuid(), primary_key=True), *_scope(),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("base_agent_id", sa.String(64), nullable=False),
        sa.Column("base_agent_version", sa.String(64), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
    op.create_index("ix_workspace_agents_space", "workspace_agents", ["space_id"])
    op.create_table(
        "workspace_workflows", sa.Column("id", sa.String(64), primary_key=True), *_scope(),
        sa.Column("template_id", sa.String(64), nullable=False),
        sa.Column("template_version", sa.String(64), nullable=False),
        sa.Column("draft", JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("published_revision", sa.Integer()),
        sa.Column("latest_version", sa.Integer(), nullable=False),
    )
    op.create_index("ix_workspace_workflows_space", "workspace_workflows", ["space_id"])
    op.create_table(
        "workspace_agent_submissions", sa.Column("run_id", sa.String(36), primary_key=True), *_scope(),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("configuration", JSONB(), nullable=False),
        sa.UniqueConstraint("tenant_id", "owner_user_id", "request_id", name="uq_workspace_agent_request"),
    )
    for table in ("object_ownership", "workflow_runs"):
        op.add_column(table, sa.Column("space_id", sa.Uuid()))
        op.create_foreign_key(f"fk_{table}_space_tenant", table, "spaces",
                              ["space_id", "tenant_id"], ["id", "tenant_id"])
    op.add_column("workflow_versions", sa.Column("definition_snapshot", JSONB()))


def downgrade():
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM object_ownership WHERE space_id IS NOT NULL "
        "UNION ALL SELECT 1 FROM workflow_versions WHERE definition_snapshot IS NOT NULL "
        "UNION ALL SELECT 1 FROM workspace_agents "
        "UNION ALL SELECT 1 FROM workspace_workflows LIMIT 1"
    )).first():
        raise RuntimeError("存在 Workspace 配置、运行归属或发布版本，不能移除")
    op.drop_column("workflow_versions", "definition_snapshot")
    for table in ("workflow_runs", "object_ownership"):
        op.drop_constraint(f"fk_{table}_space_tenant", table, type_="foreignkey")
        op.drop_column(table, "space_id")
    for table in ("workspace_agent_submissions", "workspace_workflows", "workspace_agents"):
        op.drop_table(table)
