"""冻结 CI 调查初版四表结构；后续增量由独立 revision 负责。"""
from alembic import op

revision = "0036_ci_incident_diagnosis"
down_revision = "0035_agent_quality_receipts"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE ci_scenario_instances (
	instance_id TEXT NOT NULL,
	tenant_id VARCHAR(64) NOT NULL,
	owner_user_id UUID NOT NULL,
	request_id TEXT NOT NULL,
	scenario_key TEXT NOT NULL,
	template_version TEXT NOT NULL,
	state_revision INTEGER NOT NULL,
	current_state JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (instance_id),
	UNIQUE (tenant_id, owner_user_id, request_id),
	CHECK (state_revision > 0),
	FOREIGN KEY(tenant_id) REFERENCES tenants (tenant_id),
	FOREIGN KEY(owner_user_id) REFERENCES users (id)
)
""")
    op.execute("""
CREATE INDEX ix_ci_scenario_instances_tenant_id ON ci_scenario_instances (tenant_id)
""")
    op.execute("""
CREATE TABLE ci_executions (
	execution_id TEXT NOT NULL,
	instance_id TEXT NOT NULL,
	request_id TEXT NOT NULL,
	environment_id TEXT NOT NULL,
	case_id TEXT NOT NULL,
	feature_id TEXT NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	outcome TEXT NOT NULL,
	execution_snapshot JSONB NOT NULL,
	summary JSONB NOT NULL,
	evidence_manifest JSONB NOT NULL,
	evidence_digest TEXT NOT NULL,
	PRIMARY KEY (execution_id),
	UNIQUE (instance_id, request_id),
	CHECK (outcome IN ('PASS','FAIL')),
	FOREIGN KEY(instance_id) REFERENCES ci_scenario_instances (instance_id)
)
""")
    op.execute("""
CREATE TABLE incidents (
	incident_id TEXT NOT NULL,
	execution_id TEXT NOT NULL,
	tenant_id VARCHAR(64) NOT NULL,
	owner_user_id UUID NOT NULL,
	title TEXT NOT NULL,
	status TEXT NOT NULL,
	session_id TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (incident_id),
	CHECK (status = 'OPEN'),
	UNIQUE (execution_id),
	FOREIGN KEY(execution_id) REFERENCES ci_executions (execution_id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (tenant_id),
	FOREIGN KEY(owner_user_id) REFERENCES users (id)
)
""")
    op.execute("""
CREATE INDEX ix_incidents_owner_user_id ON incidents (owner_user_id)
""")
    op.execute("""
CREATE INDEX ix_incidents_tenant_id ON incidents (tenant_id)
""")
    op.execute("""
CREATE TABLE incident_investigations (
	investigation_id TEXT NOT NULL,
	incident_id TEXT NOT NULL,
	request_id TEXT NOT NULL,
	report_version INTEGER NOT NULL,
	run_id TEXT NOT NULL,
	session_id TEXT NOT NULL,
	agent_id TEXT NOT NULL,
	agent_version TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	projected_at TIMESTAMP WITH TIME ZONE,
	projection_status TEXT NOT NULL,
	safe_error_code TEXT,
	runtime_result_digest TEXT,
	report_payload JSONB,
	PRIMARY KEY (investigation_id),
	UNIQUE (incident_id, request_id),
	UNIQUE (incident_id, report_version),
	CHECK (report_version > 0),
	CHECK (projection_status IN ('PENDING','READY','FAILED')),
	FOREIGN KEY(incident_id) REFERENCES incidents (incident_id),
	UNIQUE (run_id)
)
""")
    op.execute("""
CREATE INDEX ix_incident_investigations_incident_id ON incident_investigations (incident_id)
""")


def downgrade():
    for name in ("incident_investigations", "incidents", "ci_executions", "ci_scenario_instances"):
        op.drop_table(name)
