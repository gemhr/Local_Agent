"""质量评测准入身份和不可变响应。"""
from alembic import op

revision = "0035_agent_quality_receipts"
down_revision = "0034_agent_batches_delegations"
branch_labels = None
depends_on = None


def upgrade():
    from core.persistence.models import RuntimeAgentEvaluationReceiptRow
    RuntimeAgentEvaluationReceiptRow.__table__.create(op.get_bind())
    op.execute("""
    CREATE OR REPLACE FUNCTION runtime_quality_receipt_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF ROW(OLD.remote_run_id,OLD.attempt_id,OLD.request_id,OLD.principal_id,OLD.tenant_id,
        OLD.evaluation_project_id,OLD.evaluation_run_id,OLD.request_fingerprint,OLD.correlation,
        OLD.execution_manifest,OLD.capture_profile,OLD.received_at)
        IS DISTINCT FROM ROW(NEW.remote_run_id,NEW.attempt_id,NEW.request_id,NEW.principal_id,NEW.tenant_id,
        NEW.evaluation_project_id,NEW.evaluation_run_id,NEW.request_fingerprint,NEW.correlation,
        NEW.execution_manifest,NEW.capture_profile,NEW.received_at)
        OR (OLD.sealed_response IS NOT NULL AND ROW(OLD.sealed_response,OLD.sealed_digest,OLD.evaluation_capture)
          IS DISTINCT FROM ROW(NEW.sealed_response,NEW.sealed_digest,NEW.evaluation_capture))
      THEN RAISE EXCEPTION 'QUALITY_RECEIPT_IMMUTABLE'; END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute("CREATE TRIGGER runtime_quality_receipt_guard BEFORE UPDATE ON runtime_agent_evaluation_receipts FOR EACH ROW EXECUTE FUNCTION runtime_quality_receipt_guard()")


def downgrade():
    raise RuntimeError("QUALITY_RECEIPT_HISTORY_MUST_BE_PRESERVED")
