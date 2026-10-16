"""持久 Agent Loop 的增量表、轮次绑定和预算操作记录。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0031_durable_agent_loop"
down_revision = "0030_stage13_delivery"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runtime_run_executions", sa.Column("execution_policy", sa.String(64), nullable=False, server_default="fixed-plan.v1"))
    op.add_column("runtime_run_executions", sa.Column("budget_operations", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    from core.persistence.models import RuntimeLoopFrameRow, RuntimeLoopIterationRow
    RuntimeLoopFrameRow.__table__.create(op.get_bind())
    RuntimeLoopIterationRow.__table__.create(op.get_bind())
    op.add_column("runtime_model_invocations", sa.Column("loop_frame_id", sa.String(255)))
    op.add_column("runtime_model_invocations", sa.Column("loop_iteration_no", sa.Integer()))
    op.add_column("runtime_model_invocations", sa.Column("invocation_group", sa.String(255)))
    op.add_column("runtime_model_invocations", sa.Column("resume_generation", sa.BigInteger()))
    op.create_foreign_key("fk_runtime_model_loop_iteration", "runtime_model_invocations", "runtime_loop_iterations", ["loop_frame_id", "loop_iteration_no"], ["frame_id", "iteration_no"])
    op.create_check_constraint("ck_runtime_model_loop_binding", "runtime_model_invocations", "(loop_frame_id IS NULL AND loop_iteration_no IS NULL AND invocation_group IS NULL AND resume_generation IS NULL) OR (loop_frame_id IS NOT NULL AND loop_iteration_no > 0 AND invocation_group IS NOT NULL AND resume_generation >= 0)")
    op.execute("""
    CREATE OR REPLACE FUNCTION runtime_loop_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_TABLE_NAME = 'runtime_run_executions' THEN
        IF OLD.execution_policy IS DISTINCT FROM NEW.execution_policy THEN RAISE EXCEPTION 'LOOP_POLICY_IMMUTABLE'; END IF;
      ELSIF TG_TABLE_NAME = 'runtime_loop_frames' THEN
        IF OLD.context_seed IS DISTINCT FROM NEW.context_seed OR OLD.seed_digest IS DISTINCT FROM NEW.seed_digest
           OR OLD.pinned_config IS DISTINCT FROM NEW.pinned_config OR OLD.config_digest IS DISTINCT FROM NEW.config_digest
           OR OLD.loop_policy IS DISTINCT FROM NEW.loop_policy
           OR (OLD.final_delivery_receipt IS NOT NULL AND OLD.final_delivery_receipt IS DISTINCT FROM NEW.final_delivery_receipt)
           OR (OLD.sealed_evidence IS NOT NULL AND OLD.sealed_evidence IS DISTINCT FROM NEW.sealed_evidence)
        THEN RAISE EXCEPTION 'LOOP_FRAME_IMMUTABLE'; END IF;
      ELSE
        IF OLD.decision_id IS NOT NULL AND (OLD.decision_id IS DISTINCT FROM NEW.decision_id
          OR OLD.decision_digest IS DISTINCT FROM NEW.decision_digest OR OLD.decision_kind IS DISTINCT FROM NEW.decision_kind
          OR OLD.action_id IS DISTINCT FROM NEW.action_id OR OLD.action_recipe IS DISTINCT FROM NEW.action_recipe
          OR OLD.assistant_record IS DISTINCT FROM NEW.assistant_record OR OLD.accepted_generation IS DISTINCT FROM NEW.accepted_generation
          OR OLD.accepted_model_attempt IS DISTINCT FROM NEW.accepted_model_attempt
          OR OLD.accepted_model_attempt_number IS DISTINCT FROM NEW.accepted_model_attempt_number)
        THEN RAISE EXCEPTION 'LOOP_DECISION_IMMUTABLE'; END IF;
        IF OLD.observation IS NOT NULL AND (OLD.observation IS DISTINCT FROM NEW.observation
          OR OLD.observation_digest IS DISTINCT FROM NEW.observation_digest
          OR OLD.observation_source_digest IS DISTINCT FROM NEW.observation_source_digest
          OR OLD.observation_cursor IS DISTINCT FROM NEW.observation_cursor
          OR OLD.observed_generation IS DISTINCT FROM NEW.observed_generation)
        THEN RAISE EXCEPTION 'LOOP_OBSERVATION_IMMUTABLE'; END IF;
      END IF;
      RETURN NEW;
    END $$;
    """)
    for table in ("runtime_run_executions", "runtime_loop_frames", "runtime_loop_iterations"):
        op.execute(f"CREATE TRIGGER {table}_loop_guard BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION runtime_loop_immutable_guard()")


def downgrade():
    raise RuntimeError("DURABLE_LOOP_HISTORY_MUST_BE_PRESERVED")
