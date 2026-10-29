"""合成 CI 评测的原子准备绑定，不新增执行状态。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
revision = '0041_ci_synthetic_evaluation'
down_revision = '0040_incident_feedback'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('ci_synthetic_evaluation_bindings',
        sa.Column('run_id',sa.Text,sa.ForeignKey('runtime_agent_evaluation_receipts.remote_run_id'),primary_key=True),
        sa.Column('tenant_id',sa.String(64),sa.ForeignKey('tenants.tenant_id'),nullable=False),
        sa.Column('principal_id',sa.Uuid,sa.ForeignKey('users.id'),nullable=False),
        sa.Column('project_id',sa.Text,nullable=False),sa.Column('fingerprint',sa.Text,nullable=False),
        sa.Column('payload',JSONB,nullable=False),
        sa.Column('investigation_id',sa.Text,sa.ForeignKey('incident_investigations.investigation_id'),unique=True,nullable=False),
        sa.Column('instance_id',sa.Text,sa.ForeignKey('ci_scenario_instances.instance_id'),unique=True,nullable=False),
        sa.Column('execution_id',sa.Text,sa.ForeignKey('ci_executions.execution_id'),unique=True,nullable=False),
        sa.Column('incident_id',sa.Text,sa.ForeignKey('incidents.incident_id'),unique=True,nullable=False),
        sa.Column('sidecar',sa.Text))
    op.execute("""CREATE FUNCTION ci_synthetic_binding_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'CI_BINDING_IMMUTABLE'; END IF;
      IF (to_jsonb(NEW)-'sidecar') IS DISTINCT FROM (to_jsonb(OLD)-'sidecar')
         OR OLD.sidecar IS NOT NULL OR NEW.sidecar IS NULL THEN RAISE EXCEPTION 'CI_BINDING_IMMUTABLE'; END IF;
      RETURN NEW;
    END; $$;""")
    op.execute("""CREATE TRIGGER ci_synthetic_binding_frozen BEFORE UPDATE OR DELETE ON ci_synthetic_evaluation_bindings
    FOR EACH ROW EXECUTE FUNCTION ci_synthetic_binding_guard();""")


def downgrade():
    op.drop_table('ci_synthetic_evaluation_bindings')
    op.execute('DROP FUNCTION ci_synthetic_binding_guard()')
