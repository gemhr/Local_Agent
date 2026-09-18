"""Stage9-WP1 tenant binding and object authorization foundation."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0017_stage9_wp1_tenant_authz"
down_revision: Union[str, Sequence[str], None] = "0016_stage8_wp10_ticket"
branch_labels = None
depends_on = None

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenants"),
    )
    op.execute(sa.text(f"INSERT INTO tenants (tenant_id) VALUES ('{DEFAULT_TENANT_ID}')"))
    op.add_column("users", sa.Column("tenant_id", sa.String(64), nullable=True))
    op.execute(sa.text("UPDATE users SET tenant_id = :tenant WHERE tenant_id IS NULL").bindparams(tenant=DEFAULT_TENANT_ID))
    op.alter_column("users", "tenant_id", nullable=False, server_default=sa.text(f"'{DEFAULT_TENANT_ID}'"))
    op.create_foreign_key("fk_users_tenant", "users", "tenants", ["tenant_id"], ["tenant_id"], ondelete="RESTRICT")
    op.create_unique_constraint("uq_users_id_tenant", "users", ["id", "tenant_id"])
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.add_column("object_ownership", sa.Column("tenant_id", sa.String(64), nullable=True))
    op.execute(sa.text("UPDATE object_ownership o SET tenant_id = u.tenant_id FROM users u WHERE u.id = o.owner_user_id"))
    op.drop_constraint("ck_object_ownership_type", "object_ownership", type_="check")
    op.create_check_constraint(
        "ck_object_ownership_type",
        "object_ownership",
        "object_type IN ('RUN', 'CONVERSATION', 'MISSION', 'REVIEW', 'TEST_PLAN', 'ARTIFACT', 'GENERATED_CASE_ARTIFACT', 'EXTERNAL_EXECUTION_JOB', 'APPROVAL', 'TICKET_CONTINUATION', 'EVALUATION_JOB')",
    )
    op.execute(sa.text("""
        DO $$
        DECLARE
            missing_count BIGINT;
            human_count BIGINT;
            migration_owner UUID;
        BEGIN
            SELECT count(*) INTO missing_count
            FROM stage8_feature_test_missions m
            WHERE NOT EXISTS (
                SELECT 1 FROM object_ownership o
                WHERE o.object_type = 'MISSION' AND o.object_id = m.mission_id
            );
            IF missing_count > 0 THEN
                SELECT count(*) INTO human_count
                FROM users
                WHERE tenant_id = '00000000-0000-0000-0000-000000000001'
                  AND principal_kind = 'HUMAN';
                IF human_count <> 1 THEN
                    RAISE EXCEPTION 'ambiguous existing mission ownership backfill';
                END IF;
                SELECT id INTO migration_owner
                FROM users
                WHERE tenant_id = '00000000-0000-0000-0000-000000000001'
                  AND principal_kind = 'HUMAN'
                LIMIT 1;
                INSERT INTO object_ownership(object_type, object_id, owner_user_id, tenant_id)
                SELECT 'MISSION', m.mission_id, migration_owner,
                       '00000000-0000-0000-0000-000000000001'
                FROM stage8_feature_test_missions m
                WHERE NOT EXISTS (
                    SELECT 1 FROM object_ownership o
                    WHERE o.object_type = 'MISSION' AND o.object_id = m.mission_id
                );
            END IF;
        END $$
    """))
    op.execute(sa.text("""
        INSERT INTO object_ownership(object_type, object_id, owner_user_id, tenant_id)
        SELECT 'EVALUATION_JOB', j.id::text, j.owner_user_id, u.tenant_id
        FROM evaluation_jobs j
        JOIN users u ON u.id = j.owner_user_id
        ON CONFLICT (object_type, object_id) DO NOTHING
    """))
    op.execute(sa.text("DO $$ BEGIN IF EXISTS (SELECT 1 FROM object_ownership WHERE tenant_id IS NULL) THEN RAISE EXCEPTION 'ambiguous ownership tenant backfill'; END IF; END $$"))
    op.execute(sa.text("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM object_ownership o
                JOIN users u ON u.id = o.owner_user_id
                WHERE o.tenant_id IS DISTINCT FROM u.tenant_id
            ) THEN
                RAISE EXCEPTION 'object ownership tenant mismatch';
            END IF;
            IF EXISTS (
                SELECT 1 FROM evaluation_jobs j
                WHERE NOT EXISTS (
                    SELECT 1 FROM object_ownership o
                    WHERE o.object_type = 'EVALUATION_JOB'
                      AND o.object_id = j.id::text
                )
            ) THEN
                RAISE EXCEPTION 'evaluation job ownership backfill failed';
            END IF;
        END $$
    """))
    op.alter_column("object_ownership", "tenant_id", nullable=False, server_default=sa.text(f"'{DEFAULT_TENANT_ID}'"))
    op.create_foreign_key("fk_object_ownership_tenant", "object_ownership", "tenants", ["tenant_id"], ["tenant_id"], ondelete="RESTRICT")
    op.create_foreign_key(
        "fk_object_ownership_owner_tenant",
        "object_ownership",
        "users",
        ["owner_user_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_object_ownership_tenant_type", "object_ownership", ["tenant_id", "object_type"])


def downgrade() -> None:
    op.drop_index("ix_object_ownership_tenant_type", table_name="object_ownership")
    op.drop_constraint("fk_object_ownership_owner_tenant", "object_ownership", type_="foreignkey")
    op.drop_constraint("fk_object_ownership_tenant", "object_ownership", type_="foreignkey")
    op.execute(sa.text("DELETE FROM object_ownership WHERE object_type NOT IN ('RUN', 'CONVERSATION')"))
    op.drop_constraint("ck_object_ownership_type", "object_ownership", type_="check")
    op.create_check_constraint(
        "ck_object_ownership_type",
        "object_ownership",
        "object_type IN ('RUN', 'CONVERSATION')",
    )
    op.drop_column("object_ownership", "tenant_id")
    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_constraint("uq_users_id_tenant", "users", type_="unique")
    op.drop_constraint("fk_users_tenant", "users", type_="foreignkey")
    op.drop_column("users", "tenant_id")
    op.drop_table("tenants")
