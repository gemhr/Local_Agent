"""WP03 deterministic aggregation / immutable revisions / hard admission budget."""

from alembic import op

revision = "0026_stage13_wp03_incident"
down_revision = "0025_stage13_wp02_guardian"
branch_labels = None
depends_on = None

DDL = [
    "CREATE TABLE stage13_analysis_admission_budget (\n\tbudget_key VARCHAR(64) NOT NULL, \n\tscope VARCHAR(128) NOT NULL, \n\tproject VARCHAR(128) NOT NULL, \n\tsuite VARCHAR(128) NOT NULL, \n\tbusiness_date VARCHAR(10) NOT NULL, \n\tsubject_digest VARCHAR(64) NOT NULL, \n\tlane VARCHAR(16) NOT NULL, \n\tadmitted_total INTEGER NOT NULL, \n\tcutoff JSONB, \n\tPRIMARY KEY (budget_key), \n\tCONSTRAINT uq_s13_budget_scope UNIQUE (scope, project, suite, business_date, subject_digest, lane), \n\tCONSTRAINT ck_s13_hard_budget CHECK (admitted_total BETWEEN 0 AND 60 AND lane='NIGHTLY')\n)",
    "CREATE TABLE stage13_global_incidents (\n\tincident_id VARCHAR(36) NOT NULL, \n\tincident_key VARCHAR(64) NOT NULL, \n\tscope VARCHAR(128) NOT NULL, \n\tproject VARCHAR(128) NOT NULL, \n\tsuite VARCHAR(128) NOT NULL, \n\tbusiness_date VARCHAR(10) NOT NULL, \n\tnormalizer_version VARCHAR(64) NOT NULL, \n\tsignature VARCHAR NOT NULL, \n\tcomponents JSONB NOT NULL, \n\tstate VARCHAR(16) NOT NULL, \n\tfirst_seen_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tlast_seen_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tchanges JSONB NOT NULL, \n\tmaterial_digest VARCHAR(64) NOT NULL, \n\tdraft JSONB NOT NULL, \n\tevidence_revision INTEGER NOT NULL, \n\tevidence_manifest_digest VARCHAR(64), \n\tPRIMARY KEY (incident_id), \n\tCONSTRAINT ck_s13_incident CHECK (state IN ('OPEN','SEALED') AND evidence_revision >= 0), \n\tUNIQUE (incident_key)\n)",
    "CREATE TABLE stage13_incident_analysis_jobs (\n\tjob_id VARCHAR(36) NOT NULL, \n\tadmission_key VARCHAR(64) NOT NULL, \n\tincident_id VARCHAR(36) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tsubject_digest VARCHAR(64) NOT NULL, \n\tsubject_manifest JSONB NOT NULL, \n\tbudget_key VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(32) NOT NULL, \n\tkind VARCHAR(16) NOT NULL, \n\tadmitted_at TIMESTAMP WITH TIME ZONE, \n\tinput JSONB NOT NULL, \n\tinput_digest VARCHAR(64) NOT NULL, \n\tPRIMARY KEY (job_id), \n\tCONSTRAINT ck_s13_job CHECK (status IN ('READY','RUNNING','COMPLETED','FAILED','UNRESOLVED','DEFERRED_BUDGET') AND kind IN ('INITIAL','REANALYSIS')), \n\tCONSTRAINT uq_s13_analysis_revision UNIQUE (incident_id, revision, subject_digest), \n\tUNIQUE (admission_key), \n\tFOREIGN KEY(incident_id) REFERENCES stage13_global_incidents (incident_id), \n\tFOREIGN KEY(budget_key) REFERENCES stage13_analysis_admission_budget (budget_key)\n)",
    "CREATE TABLE stage13_incident_evidence_revisions (\n\tincident_id VARCHAR(36) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tmaterial_digest VARCHAR(64) NOT NULL, \n\tmanifest_digest VARCHAR(64) NOT NULL, \n\tmanifest JSONB NOT NULL, \n\tfrozen_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (incident_id, revision), \n\tCONSTRAINT ck_s13_revision CHECK (revision > 0), \n\tFOREIGN KEY(incident_id) REFERENCES stage13_global_incidents (incident_id)\n)",
    "CREATE TABLE stage13_failure_collections (\n\tversion_id VARCHAR(36) NOT NULL, \n\tstate VARCHAR(16) NOT NULL, \n\ttoken VARCHAR(36), \n\tepoch INTEGER NOT NULL, \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tnext_available_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tattempts INTEGER NOT NULL, \n\terror VARCHAR(64), \n\tpages JSONB NOT NULL, \n\tread_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (version_id), \n\tCONSTRAINT ck_s13_collection CHECK (state IN ('PENDING','CLAIMED','COMPLETE','MISSING') AND attempts BETWEEN 0 AND 3), \n\tFOREIGN KEY(version_id) REFERENCES stage13_version_executions (version_execution_id)\n)",
    "CREATE TABLE stage13_local_failure_clusters (\n\tcluster_id VARCHAR(36) NOT NULL, \n\tcluster_key VARCHAR(64) NOT NULL, \n\tversion_id VARCHAR(36) NOT NULL, \n\tincident_id VARCHAR(36) NOT NULL, \n\tnormalizer_version VARCHAR(64) NOT NULL, \n\tsignature VARCHAR NOT NULL, \n\tcomponents JSONB NOT NULL, \n\tsummary JSONB NOT NULL, \n\tPRIMARY KEY (cluster_id), \n\tCONSTRAINT uq_s13_incident_cluster UNIQUE (incident_id, cluster_id), \n\tUNIQUE (cluster_key), \n\tFOREIGN KEY(version_id) REFERENCES stage13_version_executions (version_execution_id), \n\tFOREIGN KEY(incident_id) REFERENCES stage13_global_incidents (incident_id)\n)",
    "CREATE TABLE stage13_cluster_memberships (\n\tversion_id VARCHAR(36) NOT NULL, \n\tcase_id VARCHAR(128) NOT NULL, \n\tcluster_id VARCHAR(36) NOT NULL, \n\tincident_id VARCHAR(36) NOT NULL, \n\tenvironment VARCHAR(128) NOT NULL, \n\tchannel VARCHAR(128) NOT NULL, \n\tproduct_version VARCHAR(128) NOT NULL, \n\tordinal INTEGER NOT NULL, \n\toutcome VARCHAR(16) NOT NULL, \n\tavailability VARCHAR(16) NOT NULL, \n\tevidence JSONB NOT NULL, \n\tPRIMARY KEY (version_id, case_id), \n\tCONSTRAINT ck_s13_membership CHECK (outcome IN ('FAILED','ERROR') AND availability IN ('AVAILABLE','MISSING')), \n\tFOREIGN KEY(version_id) REFERENCES stage13_version_executions (version_execution_id), \n\tFOREIGN KEY(cluster_id) REFERENCES stage13_local_failure_clusters (cluster_id), \n\tFOREIGN KEY(incident_id) REFERENCES stage13_global_incidents (incident_id)\n)",
    "CREATE INDEX ix_s13_incident_day ON stage13_global_incidents (scope, project, suite, business_date, first_seen_at, incident_key)",
    "CREATE INDEX ix_stage13_incident_analysis_jobs_budget_key ON stage13_incident_analysis_jobs (budget_key)",
    "CREATE INDEX ix_s13_collection_due ON stage13_failure_collections (next_available_at) WHERE state IN ('PENDING','CLAIMED')",
    "CREATE INDEX ix_stage13_local_failure_clusters_incident_id ON stage13_local_failure_clusters (incident_id)",
    "CREATE INDEX ix_stage13_local_failure_clusters_version_id ON stage13_local_failure_clusters (version_id)",
    "CREATE INDEX ix_s13_representative ON stage13_cluster_memberships (incident_id, channel, environment, ordinal, case_id)",
    "CREATE INDEX ix_stage13_cluster_memberships_cluster_id ON stage13_cluster_memberships (cluster_id)",
    "CREATE INDEX ix_stage13_cluster_memberships_incident_id ON stage13_cluster_memberships (incident_id)",
    "CREATE INDEX ix_s13_failure_source ON stage13_version_executions (completed_at,version_execution_key) WHERE status='COMPLETED' AND ((counts->>'FAILED')::int+(counts->>'ERROR')::int)>0",
    "CREATE INDEX ix_s13_cycle_day ON stage13_daily_cycles (guardian_id,business_date,status)",
]

GUARD = """
CREATE FUNCTION stage13_incident_immutable_guard() RETURNS trigger AS $$
BEGIN
  IF TG_TABLE_NAME='stage13_incident_evidence_revisions' THEN
    RAISE EXCEPTION 'IMMUTABLE_INCIDENT_REVISION';
  ELSIF TG_TABLE_NAME='stage13_incident_analysis_jobs' THEN
    IF TG_OP='DELETE' OR (to_jsonb(NEW)-'status') IS DISTINCT FROM (to_jsonb(OLD)-'status') THEN
      RAISE EXCEPTION 'IMMUTABLE_ANALYSIS_IDENTITY';
    END IF;
  ELSIF TG_TABLE_NAME='stage13_analysis_admission_budget' THEN
    IF TG_OP='DELETE' OR NEW.admitted_total<OLD.admitted_total OR
       (to_jsonb(NEW)-'admitted_total'-'cutoff') IS DISTINCT FROM (to_jsonb(OLD)-'admitted_total'-'cutoff') THEN
      RAISE EXCEPTION 'ADMISSION_BUDGET_NO_REFUND';
    END IF;
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql
"""


def upgrade():
    for statement in DDL:
        op.execute(statement)
    op.execute(
        "ALTER TABLE stage13_incident_analysis_jobs ADD CONSTRAINT fk_s13_job_revision FOREIGN KEY (incident_id,revision) REFERENCES stage13_incident_evidence_revisions(incident_id,revision)"
    )
    op.execute(GUARD)
    for table in (
        "stage13_incident_evidence_revisions",
        "stage13_incident_analysis_jobs",
        "stage13_analysis_admission_budget",
    ):
        op.execute(
            f"CREATE TRIGGER s13_incident_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION stage13_incident_immutable_guard()"
        )


def downgrade():
    op.execute(
        "ALTER TABLE stage13_incident_analysis_jobs DROP CONSTRAINT fk_s13_job_revision"
    )
    op.execute("DROP INDEX ix_s13_failure_source")
    op.execute("DROP INDEX ix_s13_cycle_day")
    op.execute("DROP TABLE stage13_cluster_memberships")
    op.execute("DROP TABLE stage13_local_failure_clusters")
    op.execute("DROP TABLE stage13_failure_collections")
    op.execute("DROP TABLE stage13_incident_evidence_revisions")
    op.execute("DROP TABLE stage13_incident_analysis_jobs")
    op.execute("DROP TABLE stage13_global_incidents")
    op.execute("DROP TABLE stage13_analysis_admission_budget")
    op.execute("DROP FUNCTION stage13_incident_immutable_guard()")
