"""有限批次及同 Run/Step 的子 Agent 持久身份。"""
from alembic import op
import sqlalchemy as sa

revision = "0034_agent_batches_delegations"
down_revision = "0033_scoped_memory_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    from core.persistence.models import (RuntimeLoopFrameRow, RuntimeLoopIterationRow,
        RuntimeLoopActionRow, RuntimeAgentDelegationRow)
    for model, names in ((RuntimeLoopFrameRow, ("frame_role", "execution_agent_id", "parent_frame_id", "delegation_id")),
                         (RuntimeLoopIterationRow, ("schema_version",))):
        table = model.__tablename__
        existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
        for name in names:
            c = model.__table__.c[name]
            if name not in existing:
                op.add_column(table, sa.Column(name, c.type, nullable=c.nullable, server_default=c.server_default))
        constraints = {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints(table)}
        for c in model.__table__.constraints:
            if isinstance(c, sa.CheckConstraint):
                if c.name in constraints:
                    op.drop_constraint(c.name, table, type_="check")
                op.create_check_constraint(c.name, table, str(c.sqltext))
    unique = {c["name"] for c in sa.inspect(op.get_bind()).get_unique_constraints("runtime_loop_frames")}
    if "uq_runtime_loop_frame_step" in unique:
        op.drop_constraint("uq_runtime_loop_frame_step", "runtime_loop_frames", type_="unique")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_runtime_loop_root_step ON runtime_loop_frames(run_id,step_id) WHERE frame_role = 'ROOT'")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_loop_frame_delegation ON runtime_loop_frames(delegation_id) WHERE delegation_id IS NOT NULL")
    RuntimeLoopActionRow.__table__.create(op.get_bind())
    RuntimeAgentDelegationRow.__table__.create(op.get_bind())
    op.create_foreign_key("fk_loop_frame_parent", "runtime_loop_frames", "runtime_loop_frames",
        ["parent_frame_id", "run_id", "step_id"], ["frame_id", "run_id", "step_id"])
    op.create_foreign_key("fk_loop_frame_delegation", "runtime_loop_frames", "runtime_agent_delegations",
        ["delegation_id"], ["delegation_id"], deferrable=True, initially="DEFERRED")
    op.execute("""
    CREATE OR REPLACE FUNCTION runtime_agent_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_TABLE_NAME = 'runtime_loop_actions' THEN
        IF ROW(OLD.action_id,OLD.frame_id,OLD.run_id,OLD.step_id,OLD.iteration_no,OLD.ordinal,OLD.protocol_call_id,OLD.recipe,OLD.recipe_digest)
          IS DISTINCT FROM ROW(NEW.action_id,NEW.frame_id,NEW.run_id,NEW.step_id,NEW.iteration_no,NEW.ordinal,NEW.protocol_call_id,NEW.recipe,NEW.recipe_digest)
          OR (OLD.invocation_id IS NOT NULL AND OLD.invocation_id IS DISTINCT FROM NEW.invocation_id)
          OR (OLD.result_projection IS NOT NULL AND ROW(OLD.result_projection,OLD.result_digest,OLD.source_digest) IS DISTINCT FROM ROW(NEW.result_projection,NEW.result_digest,NEW.source_digest))
          OR (OLD.not_executed IS NOT NULL AND OLD.not_executed IS DISTINCT FROM NEW.not_executed)
        THEN RAISE EXCEPTION 'LOOP_ACTION_IMMUTABLE'; END IF;
      ELSIF TG_TABLE_NAME = 'runtime_agent_delegations' THEN
        IF ROW(OLD.delegation_id,OLD.run_id,OLD.step_id,OLD.parent_frame_id,OLD.parent_iteration,OLD.child_frame_id,OLD.frozen,OLD.frozen_digest,OLD.absolute_deadline,OLD.envelope_id)
          IS DISTINCT FROM ROW(NEW.delegation_id,NEW.run_id,NEW.step_id,NEW.parent_frame_id,NEW.parent_iteration,NEW.child_frame_id,NEW.frozen,NEW.frozen_digest,NEW.absolute_deadline,NEW.envelope_id)
          OR (OLD.result IS NOT NULL AND ROW(OLD.result,OLD.result_digest) IS DISTINCT FROM ROW(NEW.result,NEW.result_digest))
          OR (OLD.observed AND NOT NEW.observed)
        THEN RAISE EXCEPTION 'DELEGATION_IMMUTABLE'; END IF;
      ELSE
        IF ROW(OLD.schema_version,OLD.decision_schema_version,OLD.context_seed_version,OLD.execution_policy,OLD.frame_role,OLD.execution_agent_id,OLD.parent_frame_id,OLD.delegation_id)
          IS DISTINCT FROM ROW(NEW.schema_version,NEW.decision_schema_version,NEW.context_seed_version,NEW.execution_policy,NEW.frame_role,NEW.execution_agent_id,NEW.parent_frame_id,NEW.delegation_id)
        THEN RAISE EXCEPTION 'LOOP_VERSION_IMMUTABLE'; END IF;
      END IF;
      RETURN NEW;
    END $$;
    """)
    for table in ("runtime_loop_actions", "runtime_agent_delegations", "runtime_loop_frames"):
        op.execute(f"CREATE TRIGGER {table}_identity_guard BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION runtime_agent_identity_guard()")
    op.execute("""
    CREATE OR REPLACE FUNCTION runtime_agent_tree_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE f runtime_loop_frames; p runtime_loop_frames; d runtime_agent_delegations;
    BEGIN
      IF TG_TABLE_NAME = 'runtime_loop_iterations' THEN
        SELECT * INTO f FROM runtime_loop_frames WHERE frame_id=NEW.frame_id;
        IF f.schema_version <> NEW.schema_version OR (TG_OP='UPDATE' AND OLD.schema_version <> NEW.schema_version)
        THEN RAISE EXCEPTION 'LOOP_VERSION_UNSUPPORTED'; END IF;
      ELSE
        SELECT * INTO f FROM runtime_loop_frames WHERE frame_id=NEW.frame_id;
        IF f.frame_role='CHILD' THEN
          SELECT * INTO p FROM runtime_loop_frames WHERE frame_id=f.parent_frame_id;
          SELECT * INTO d FROM runtime_agent_delegations WHERE delegation_id=f.delegation_id;
          IF p.frame_role <> 'ROOT' OR p.schema_version <> 2 OR d.child_frame_id IS DISTINCT FROM f.frame_id
             OR d.parent_frame_id IS DISTINCT FROM p.frame_id OR d.run_id IS DISTINCT FROM f.run_id
             OR d.step_id IS DISTINCT FROM f.step_id THEN RAISE EXCEPTION 'DELEGATION_IDENTITY_INVALID'; END IF;
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute("CREATE TRIGGER runtime_loop_iteration_version BEFORE INSERT OR UPDATE ON runtime_loop_iterations FOR EACH ROW EXECUTE FUNCTION runtime_agent_tree_guard()")
    op.execute("CREATE CONSTRAINT TRIGGER runtime_loop_child_tree AFTER INSERT OR UPDATE ON runtime_loop_frames DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION runtime_agent_tree_guard()")


def downgrade():
    raise RuntimeError("AGENT_EXECUTION_HISTORY_MUST_BE_PRESERVED")
