"""允许 STARTED 调用补写响应身份元数据，保留已冻结回执保护。"""

from alembic import op

revision = "0028_stage13_wp04c_evidence"
down_revision = "0027_stage13_wp04_triage"
branch_labels = None
depends_on = None


def _guard(extra):
    mutable = (
        "'state','dispatch_certainty','verification_status','reported_provider','reported_model','reported_revision','actual_revision','input_tokens','output_tokens','cost'"
        + extra
    )
    return f"""
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
               ((NEW.model_call-ARRAY[{mutable}])
                IS DISTINCT FROM (OLD.model_call-ARRAY[{mutable}])
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
    """


def upgrade():
    op.execute(
        _guard(
            ", 'system_fingerprint','reported_artifact_digest','reported_deployment_id'"
        )
    )


def downgrade():
    op.execute(_guard(""))
