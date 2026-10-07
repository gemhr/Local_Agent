"""WP04 真实主体与执行记录；不变更 0026 历史 guard。"""

from alembic import op

revision = "0027_stage13_wp04_triage"
down_revision = "0026_stage13_wp03_incident"
branch_labels = None
depends_on = None
DDL = [
    "\nCREATE TABLE stage13_triage_subjects (\n\tsubject_digest VARCHAR(64) NOT NULL, \n\tsubject_id VARCHAR(128) NOT NULL, \n\tsubject_version VARCHAR(64) NOT NULL, \n\tmanifest JSONB NOT NULL, \n\tPRIMARY KEY (subject_digest), \n\tUNIQUE (subject_id, subject_version)\n)\n\n",
    "\nCREATE TABLE stage13_analysis_executions (\n\tjob_id VARCHAR(36) NOT NULL, \n\towner VARCHAR(128), \n\ttoken VARCHAR(36), \n\tepoch INTEGER NOT NULL, \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tinitial_run_id VARCHAR(36) NOT NULL, \n\trepair_run_id VARCHAR(36), \n\tselected_final_run_id VARCHAR(36), \n\traw_answer_digest VARCHAR(64), \n\tstructured_output_digest VARCHAR(64), \n\tvalidation JSONB, \n\tactual_subject_receipt_digest VARCHAR(64), \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (job_id), \n\tCONSTRAINT ck_s13_execution_epoch CHECK (epoch >= 0), \n\tFOREIGN KEY(job_id) REFERENCES stage13_incident_analysis_jobs (job_id), \n\tUNIQUE (initial_run_id), \n\tUNIQUE (repair_run_id)\n)\n\n",
    "\nCREATE TABLE stage13_triage_runs (\n\trun_id VARCHAR(36) NOT NULL, \n\tanchor_run_id VARCHAR(36) NOT NULL, \n\tanalysis_job_id VARCHAR(36), \n\tevaluation_attempt_id VARCHAR(36), \n\tscope VARCHAR(128) NOT NULL, \n\tlane VARCHAR(32) NOT NULL, \n\trole VARCHAR(32) NOT NULL, \n\tsubject_digest VARCHAR(64) NOT NULL, \n\tquery VARCHAR NOT NULL, \n\tinput_digest VARCHAR(64) NOT NULL, \n\tdeadline_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tmodel_call JSONB, \n\traw_answer VARCHAR, \n\treceipt JSONB, \n\truntime_status VARCHAR(32), \n\tstop_reason VARCHAR(64), \n\tvalidation JSONB, \n\tPRIMARY KEY (run_id), \n\tCONSTRAINT uq_s13_run_role UNIQUE (anchor_run_id, role), \n\tCONSTRAINT ck_s13_triage_run CHECK (role IN ('INITIAL','SCHEMA_REPAIR') AND lane IN ('NIGHTLY','CONTRACT_TEST','OFFLINE')), \n\tFOREIGN KEY(analysis_job_id) REFERENCES stage13_incident_analysis_jobs (job_id), \n\tFOREIGN KEY(subject_digest) REFERENCES stage13_triage_subjects (subject_digest)\n)\n\n",
    "\nCREATE TABLE stage13_triage_evidence_reads (\n\toperation_id VARCHAR(64) NOT NULL, \n\trun_id VARCHAR(36) NOT NULL, \n\tevidence JSONB NOT NULL, \n\tbyte_count INTEGER NOT NULL, \n\tsuccessful BOOLEAN NOT NULL, \n\tPRIMARY KEY (operation_id), \n\tFOREIGN KEY(run_id) REFERENCES stage13_triage_runs (run_id)\n)\n\n",
    "CREATE INDEX ix_stage13_triage_evidence_reads_run_id ON stage13_triage_evidence_reads (run_id)",
]


def upgrade():
    for statement in DDL:
        op.execute(statement)
    op.execute("""
    CREATE OR REPLACE FUNCTION stage13_triage_frozen_guard() RETURNS trigger AS $$
    BEGIN
      IF TG_OP='DELETE' OR TG_TABLE_NAME IN ('stage13_triage_subjects','stage13_triage_evidence_reads') THEN
        RAISE EXCEPTION 'STAGE13_TRIAGE_IMMUTABLE';
      END IF;
      IF TG_TABLE_NAME='stage13_triage_runs' THEN
        IF (to_jsonb(NEW)-ARRAY['model_call','raw_answer','receipt','runtime_status','stop_reason','validation'])
           IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['model_call','raw_answer','receipt','runtime_status','stop_reason','validation'])
           OR (OLD.receipt IS NOT NULL AND to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD))
           OR (OLD.model_call IS NOT NULL AND NEW.model_call IS DISTINCT FROM OLD.model_call AND
               ((NEW.model_call-'state'-'dispatch_certainty'-'verification_status'-'reported_provider'-'reported_model'-'reported_revision'-'actual_revision'-'input_tokens'-'output_tokens'-'cost')
                IS DISTINCT FROM
                (OLD.model_call-'state'-'dispatch_certainty'-'verification_status'-'reported_provider'-'reported_model'-'reported_revision'-'actual_revision'-'input_tokens'-'output_tokens'-'cost')
                OR OLD.model_call->>'state' <> 'STARTED')) THEN
          RAISE EXCEPTION 'STAGE13_TRIAGE_IMMUTABLE';
        END IF;
      ELSE
        IF NEW.initial_run_id <> OLD.initial_run_id
           OR (OLD.repair_run_id IS NOT NULL AND NEW.repair_run_id IS DISTINCT FROM OLD.repair_run_id)
           OR (OLD.selected_final_run_id IS NOT NULL AND to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD)) THEN
          RAISE EXCEPTION 'STAGE13_TRIAGE_IMMUTABLE';
        END IF;
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql
    """)
    for table in (
        "stage13_triage_subjects",
        "stage13_triage_evidence_reads",
        "stage13_triage_runs",
        "stage13_analysis_executions",
    ):
        op.execute(
            f"CREATE TRIGGER stage13_triage_frozen BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION stage13_triage_frozen_guard()"
        )


def downgrade():
    op.execute("DROP TABLE stage13_triage_evidence_reads CASCADE")
    op.execute("DROP TABLE stage13_triage_runs CASCADE")
    op.execute("DROP TABLE stage13_analysis_executions CASCADE")
    op.execute("DROP TABLE stage13_triage_subjects CASCADE")
    op.execute("DROP FUNCTION IF EXISTS stage13_triage_frozen_guard()")
