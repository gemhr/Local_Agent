"""夜间业务五表及原执行可空关联；冻结增量 DDL。"""
from alembic import op

revision = "0038_nightly_ci_operations"
down_revision = "0037_incident_resolution"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE TABLE nightly_ci_batches (\n\tbatch_id TEXT NOT NULL, \n\ttenant_id VARCHAR(64) NOT NULL, \n\tcreated_by_user_id UUID NOT NULL, \n\trequest_id TEXT NOT NULL, \n\tbusiness_date DATE NOT NULL, \n\tplan_key TEXT NOT NULL, \n\tplan_version TEXT NOT NULL, \n\tplan_digest TEXT NOT NULL, \n\tfrozen_plan JSONB NOT NULL, \n\tsimulated_now TIMESTAMP WITH TIME ZONE NOT NULL, \n\tstatus TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tsealed_at TIMESTAMP WITH TIME ZONE, \n\tsealed_summary JSONB, \n\tsealed_digest TEXT, \n\tPRIMARY KEY (batch_id), \n\tUNIQUE (tenant_id, created_by_user_id, request_id), \n\tUNIQUE (tenant_id, created_by_user_id, business_date, plan_key), \n\tCHECK (status IN ('PLANNED','RUNNING','COMPLETED')), \n\tCHECK ((status = 'COMPLETED' AND sealed_at IS NOT NULL AND sealed_summary IS NOT NULL AND sealed_digest IS NOT NULL) OR (status <> 'COMPLETED' AND sealed_at IS NULL AND sealed_summary IS NULL AND sealed_digest IS NULL)), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants (tenant_id), \n\tFOREIGN KEY(created_by_user_id) REFERENCES users (id)\n)")
    op.execute("CREATE TABLE nightly_version_executions (\n\tversion_execution_id TEXT NOT NULL, \n\tbatch_id TEXT NOT NULL, \n\tenvironment_id TEXT NOT NULL, \n\towner_user_id UUID NOT NULL, \n\tordinal INTEGER NOT NULL, \n\tsoftware_version TEXT NOT NULL, \n\tfrozen_cases JSONB NOT NULL, \n\tstatus TEXT NOT NULL, \n\tnext_case_index INTEGER NOT NULL, \n\tstarted_business_at TIMESTAMP WITH TIME ZONE, \n\tcompleted_business_at TIMESTAMP WITH TIME ZONE, \n\tstarted_by_user_id UUID, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (version_execution_id), \n\tUNIQUE (batch_id, environment_id, ordinal), \n\tCHECK (ordinal BETWEEN 1 AND 3), \n\tCHECK (jsonb_array_length(frozen_cases) = 2), \n\tCHECK (status IN ('PENDING','RUNNING','COMPLETED')), \n\tCHECK (next_case_index BETWEEN 0 AND 2), \n\tCHECK ((status = 'PENDING' AND next_case_index = 0 AND started_business_at IS NULL AND started_by_user_id IS NULL AND completed_business_at IS NULL) OR (status = 'RUNNING' AND next_case_index < 2 AND started_business_at IS NOT NULL AND started_by_user_id IS NOT NULL AND completed_business_at IS NULL) OR (status = 'COMPLETED' AND next_case_index = 2 AND started_business_at IS NOT NULL AND started_by_user_id IS NOT NULL AND completed_business_at IS NOT NULL)), \n\tFOREIGN KEY(batch_id) REFERENCES nightly_ci_batches (batch_id), \n\tFOREIGN KEY(owner_user_id) REFERENCES users (id), \n\tFOREIGN KEY(started_by_user_id) REFERENCES users (id)\n)")
    op.execute("CREATE UNIQUE INDEX uq_nightly_running_environment ON nightly_version_executions (batch_id, environment_id) WHERE status = 'RUNNING'")
    op.execute("CREATE TABLE incident_groups (\n\tgroup_id TEXT NOT NULL, \n\tbatch_id TEXT NOT NULL, \n\trule_version TEXT NOT NULL, \n\tsignature_digest TEXT NOT NULL, \n\tsignature_payload JSONB NOT NULL, \n\tkind TEXT NOT NULL, \n\trevision INTEGER NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\thistory JSONB NOT NULL, \n\tPRIMARY KEY (group_id), \n\tCHECK (kind IN ('AUTO','MANUAL_SPLIT')), \n\tCHECK (revision > 0), \n\tFOREIGN KEY(batch_id) REFERENCES nightly_ci_batches (batch_id)\n)")
    op.execute("CREATE UNIQUE INDEX uq_incident_auto_signature ON incident_groups (batch_id, rule_version, signature_digest) WHERE kind = 'AUTO'")
    op.execute("CREATE TABLE incident_group_memberships (\n\tgroup_id TEXT NOT NULL, \n\tincident_id TEXT NOT NULL, \n\texecution_id TEXT NOT NULL, \n\tevidence_refs JSONB NOT NULL, \n\tdecision TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tdecided_at TIMESTAMP WITH TIME ZONE, \n\tdecided_by_user_id UUID, \n\tPRIMARY KEY (group_id, incident_id), \n\tCHECK (decision IN ('CANDIDATE','CONFIRMED','EXCLUDED')), \n\tFOREIGN KEY(group_id) REFERENCES incident_groups (group_id), \n\tFOREIGN KEY(incident_id) REFERENCES incidents (incident_id), \n\tFOREIGN KEY(execution_id) REFERENCES ci_executions (execution_id), \n\tFOREIGN KEY(decided_by_user_id) REFERENCES users (id)\n)")
    op.execute("CREATE UNIQUE INDEX uq_incident_current_group ON incident_group_memberships (incident_id) WHERE decision <> 'EXCLUDED'")
    op.execute("CREATE TABLE user_notifications (\n\tnotification_id TEXT NOT NULL, \n\ttenant_id VARCHAR(64) NOT NULL, \n\trecipient_user_id UUID NOT NULL, \n\tbatch_id TEXT NOT NULL, \n\tkind TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tread_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (notification_id), \n\tUNIQUE (batch_id, recipient_user_id, kind), \n\tCHECK (kind IN ('INCREMENTAL_FAILURE','FINAL_SUMMARY')), \n\tFOREIGN KEY(tenant_id) REFERENCES tenants (tenant_id), \n\tFOREIGN KEY(recipient_user_id) REFERENCES users (id), \n\tFOREIGN KEY(batch_id) REFERENCES nightly_ci_batches (batch_id)\n)")
    op.execute('ALTER TABLE ci_executions ADD COLUMN nightly_version_execution_id TEXT REFERENCES nightly_version_executions(version_execution_id)')
    op.execute('ALTER TABLE ci_executions ADD COLUMN business_completed_at TIMESTAMP WITH TIME ZONE')
    op.execute('ALTER TABLE ci_executions ADD CONSTRAINT ck_ci_nightly_link CHECK ((nightly_version_execution_id IS NULL) = (business_completed_at IS NULL))')
    op.execute('CREATE UNIQUE INDEX uq_ci_nightly_case ON ci_executions (nightly_version_execution_id, case_id) WHERE nightly_version_execution_id IS NOT NULL')


def downgrade():
    op.execute("ALTER TABLE ci_executions DROP COLUMN business_completed_at CASCADE")
    op.execute("ALTER TABLE ci_executions DROP COLUMN nightly_version_execution_id CASCADE")
    op.execute("DROP TABLE user_notifications")
    op.execute("DROP TABLE incident_group_memberships")
    op.execute("DROP TABLE incident_groups")
    op.execute("DROP TABLE nightly_version_executions")
    op.execute("DROP TABLE nightly_ci_batches")
