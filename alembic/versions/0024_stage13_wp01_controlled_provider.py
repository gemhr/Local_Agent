"""Stage13 WP01 独立 Controlled CI Provider；只新增。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0024_stage13_wp01_provider"
down_revision = "0023_stage10_wp2_manual_audit"
branch_labels = None
depends_on = None


def upgrade():
    # 明确固定本 revision 的 schema，不从未来变化的 ORM metadata 重建历史。
    op.create_table(
        "stage13_provider_namespaces",
        sa.Column("provider_namespace_id", sa.String(128), primary_key=True),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("logical_time", sa.BigInteger(), nullable=False),
        sa.Column("evidence_retention_until", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("logical_time >= 0", name="ck_stage13_provider_clock"),
    )
    op.create_table(
        "stage13_provider_keys",
        sa.Column(
            "provider_namespace_id",
            sa.String(128),
            sa.ForeignKey("stage13_provider_namespaces.provider_namespace_id"),
            primary_key=True,
        ),
        sa.Column("business_key", sa.String(64), primary_key=True),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("remote_execution_id", sa.String(36)),
        sa.Column("receipt", postgresql.JSONB()),
        sa.Column("seal_receipt", postgresql.JSONB()),
        sa.Column("request", postgresql.JSONB()),
        sa.Column("plan", postgresql.JSONB()),
        sa.Column("state", sa.String(32)),
        sa.Column("status_revision", sa.BigInteger(), nullable=False),
        sa.Column("accepted_logical_time", sa.BigInteger()),
        sa.Column("terminal_logical_time", sa.BigInteger()),
        sa.Column("result_delay_seconds", sa.BigInteger(), nullable=False),
        sa.Column("response_loss_remaining", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "remote_execution_id", name="uq_stage13_provider_remote_id"
        ),
        sa.CheckConstraint(
            "(remote_execution_id IS NULL AND seal_receipt IS NOT NULL AND receipt IS NULL AND state IS NULL AND request IS NULL AND plan IS NULL AND status_revision = 0) OR (remote_execution_id IS NOT NULL AND seal_receipt IS NULL AND receipt IS NOT NULL AND request IS NOT NULL AND plan IS NOT NULL AND state IS NOT NULL AND state IN ('QUEUED','RUNNING','COMPLETED','INFRA_FAILED','CANCELLED') AND status_revision >= 1)",
            name="ck_stage13_provider_key_kind",
        ),
        sa.CheckConstraint(
            "result_delay_seconds >= 0 AND response_loss_remaining >= 0",
            name="ck_stage13_provider_fault_bounds",
        ),
    )
    op.execute("""
        CREATE FUNCTION stage13_provider_key_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF (NEW.provider_namespace_id, NEW.business_key, NEW.request_digest,
              NEW.remote_execution_id, NEW.receipt, NEW.seal_receipt, NEW.request, NEW.plan,
              NEW.accepted_logical_time, NEW.result_delay_seconds)
             IS DISTINCT FROM
             (OLD.provider_namespace_id, OLD.business_key, OLD.request_digest,
              OLD.remote_execution_id, OLD.receipt, OLD.seal_receipt, OLD.request, OLD.plan,
              OLD.accepted_logical_time, OLD.result_delay_seconds) THEN
            RAISE EXCEPTION 'STAGE13_IMMUTABLE_BINDING';
          END IF;
          IF OLD.state IN ('COMPLETED','INFRA_FAILED','CANCELLED') AND
             (NEW.state, NEW.status_revision, NEW.terminal_logical_time) IS DISTINCT FROM
             (OLD.state, OLD.status_revision, OLD.terminal_logical_time) THEN
            RAISE EXCEPTION 'STAGE13_IMMUTABLE_TERMINAL';
          END IF;
          IF NEW.state IS DISTINCT FROM OLD.state THEN
            IF NOT ((OLD.state = 'QUEUED' AND NEW.state IN ('RUNNING','INFRA_FAILED','CANCELLED'))
                 OR (OLD.state = 'RUNNING' AND NEW.state IN ('COMPLETED','INFRA_FAILED','CANCELLED')))
               OR NEW.status_revision <> OLD.status_revision + 1 THEN
              RAISE EXCEPTION 'STAGE13_INVALID_TRANSITION';
            END IF;
          ELSIF NEW.status_revision <> OLD.status_revision THEN
            RAISE EXCEPTION 'STAGE13_INVALID_REVISION';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER stage13_provider_key_guard BEFORE UPDATE ON stage13_provider_keys
          FOR EACH ROW EXECUTE FUNCTION stage13_provider_key_guard()
    """)


def downgrade():
    op.execute("DROP TRIGGER stage13_provider_key_guard ON stage13_provider_keys")
    op.execute("DROP FUNCTION stage13_provider_key_guard()")
    op.drop_table("stage13_provider_keys")
    op.drop_table("stage13_provider_namespaces")
