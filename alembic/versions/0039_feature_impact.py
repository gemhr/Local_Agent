"""历史影响业务五表；只由显式迁移命令创建。"""
from alembic import op

revision = "0039_feature_impact"
down_revision = "0038_nightly_ci_operations"
branch_labels = None
depends_on = None

def upgrade():
    op.execute('\nCREATE TABLE feature_impact_tasks (\n\ttask_id TEXT NOT NULL, \n\ttenant_id VARCHAR(64) NOT NULL, \n\towner_user_id UUID NOT NULL, \n\trequest_id TEXT NOT NULL, \n\tsnapshot JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\texposed_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (task_id), \n\tUNIQUE (tenant_id, owner_user_id, request_id), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants (tenant_id), \n\tFOREIGN KEY(owner_user_id) REFERENCES users (id)\n)\n\n')
    op.execute('\nCREATE TABLE feature_impact_reports (\n\treport_id TEXT NOT NULL, \n\ttask_id TEXT NOT NULL, \n\trequest_id TEXT NOT NULL, \n\tintent_digest TEXT NOT NULL, \n\treport_version INTEGER NOT NULL, \n\tenvelope JSONB NOT NULL, \n\traw_output TEXT NOT NULL, \n\tpayload JSONB, \n\trejection TEXT, \n\tsource_record JSONB NOT NULL, \n\tPRIMARY KEY (report_id), \n\tUNIQUE (task_id, request_id), \n\tUNIQUE (task_id, report_version), \n\tCHECK ((payload IS NULL) <> (rejection IS NULL)), \n\tFOREIGN KEY(task_id) REFERENCES feature_impact_tasks (task_id)\n)\n\n')
    op.execute('\nCREATE TABLE feature_impact_plans (\n\tplan_id TEXT NOT NULL, \n\treport_id TEXT NOT NULL, \n\trequest_id TEXT NOT NULL, \n\tintent_digest TEXT NOT NULL, \n\tpayload JSONB NOT NULL, \n\tPRIMARY KEY (plan_id), \n\tUNIQUE (report_id), \n\tFOREIGN KEY(report_id) REFERENCES feature_impact_reports (report_id)\n)\n\n')
    op.execute('\nCREATE TABLE feature_impact_executions (\n\texecution_id TEXT NOT NULL, \n\tplan_id TEXT NOT NULL, \n\trequest_id TEXT NOT NULL, \n\tintent_digest TEXT NOT NULL, \n\tresult JSONB NOT NULL, \n\tinternal_trace JSONB NOT NULL, \n\tPRIMARY KEY (execution_id), \n\tUNIQUE (plan_id, request_id), \n\tFOREIGN KEY(plan_id) REFERENCES feature_impact_plans (plan_id)\n)\n\n')
    op.execute('\nCREATE TABLE feature_impact_investigations (\n\trun_id TEXT NOT NULL, \n\ttask_id TEXT NOT NULL, \n\tsession_id TEXT NOT NULL, \n\trequest_id TEXT NOT NULL, \n\tPRIMARY KEY (run_id), \n\tUNIQUE (task_id, request_id), \n\tFOREIGN KEY(task_id) REFERENCES feature_impact_tasks (task_id), \n\tUNIQUE (session_id)\n)\n\n')

    op.execute("""CREATE FUNCTION feature_impact_frozen_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_TABLE_NAME = 'feature_impact_tasks' AND TG_OP = 'UPDATE'
         AND (to_jsonb(OLD) - 'exposed_at') = (to_jsonb(NEW) - 'exposed_at')
         AND OLD.exposed_at IS NULL AND NEW.exposed_at IS NOT NULL THEN RETURN NEW; END IF;
      RAISE EXCEPTION 'FEATURE_IMPACT_IMMUTABLE';
    END $$""")
    for table in ('feature_impact_tasks', 'feature_impact_reports', 'feature_impact_plans', 'feature_impact_executions', 'feature_impact_investigations'):
        op.execute(f'CREATE TRIGGER feature_impact_frozen BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION feature_impact_frozen_guard()')

def downgrade():
    op.drop_table('feature_impact_investigations')
    op.drop_table('feature_impact_executions')
    op.drop_table('feature_impact_plans')
    op.drop_table('feature_impact_reports')
    op.drop_table('feature_impact_tasks')
    op.execute("DROP FUNCTION feature_impact_frozen_guard()")
