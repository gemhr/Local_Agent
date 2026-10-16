"""不可变模型上下文、有限压缩操作与模型 purpose。"""

from alembic import op
import sqlalchemy as sa

revision = "0032_context_compaction"
down_revision = "0031_durable_agent_loop"
branch_labels = None
depends_on = None


def upgrade():
    from core.persistence.models import (RuntimeLoopFrameRow, RuntimeLoopIterationRow,
        RuntimeModelInvocationRow, RuntimeContextSnapshotRow, RuntimeContextOperationRow)
    additions = {
        RuntimeLoopFrameRow: ("context_policy", "current_context_snapshot_id", "current_context_snapshot_version", "current_context_snapshot_digest", "sealed_context_evidence"),
        RuntimeLoopIterationRow: ("context_snapshot_id", "context_snapshot_version", "context_snapshot_digest", "context_rebind_count"),
        RuntimeModelInvocationRow: ("purpose", "context_snapshot_id", "context_snapshot_version", "context_snapshot_digest", "compaction_operation_id", "request_assessment"),
    }
    # 0031 历史上以 metadata 建表；兼容空库顺序迁移时已存在的新 nullable 列。
    for model, names in additions.items():
        existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(model.__tablename__)}
        for name in names:
            if name not in existing:
                column = model.__table__.c[name]
                op.add_column(model.__tablename__, sa.Column(name, column.type, nullable=column.nullable, server_default=column.server_default))
    RuntimeContextSnapshotRow.__table__.create(op.get_bind())
    RuntimeContextOperationRow.__table__.create(op.get_bind())
    for model, names in ((RuntimeModelInvocationRow, ("ck_runtime_model_loop_binding", "ck_runtime_model_state", "ck_model_context_purpose", "ck_model_context_tuple")),
                         (RuntimeLoopIterationRow, ("ck_context_rebind_count",))):
        existing = {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints(model.__tablename__)}
        for constraint in model.__table__.constraints:
            if isinstance(constraint, sa.CheckConstraint) and constraint.name in names:
                if constraint.name in existing:
                    op.drop_constraint(constraint.name, model.__tablename__, type_="check")
                op.create_check_constraint(constraint.name, model.__tablename__, str(constraint.sqltext))
    for model in (RuntimeModelInvocationRow, RuntimeLoopIterationRow, RuntimeLoopFrameRow):
        for constraint in model.__table__.foreign_key_constraints:
            if constraint.name in {"fk_model_context_snapshot", "fk_model_context_operation", "fk_iteration_context_snapshot", "fk_frame_context_snapshot"}:
                op.execute(sa.schema.AddConstraint(constraint))
    op.execute("""
    CREATE OR REPLACE FUNCTION runtime_context_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_TABLE_NAME = 'runtime_context_snapshots' THEN
        RAISE EXCEPTION 'CONTEXT_SNAPSHOT_IMMUTABLE';
      ELSIF TG_TABLE_NAME = 'runtime_loop_frames' THEN
        IF OLD.context_policy IS DISTINCT FROM NEW.context_policy OR
           (OLD.sealed_context_evidence IS NOT NULL AND OLD.sealed_context_evidence IS DISTINCT FROM NEW.sealed_context_evidence)
        THEN RAISE EXCEPTION 'CONTEXT_POLICY_IMMUTABLE'; END IF;
      ELSIF TG_TABLE_NAME = 'runtime_loop_iterations' THEN
        IF OLD.decision_id IS NOT NULL AND (OLD.context_input_digest IS DISTINCT FROM NEW.context_input_digest OR
            OLD.context_snapshot_id IS DISTINCT FROM NEW.context_snapshot_id OR OLD.context_cursor IS DISTINCT FROM NEW.context_cursor)
        THEN RAISE EXCEPTION 'CONTEXT_BINDING_IMMUTABLE'; END IF;
      ELSIF TG_TABLE_NAME = 'runtime_context_operations' THEN
        IF OLD.prepared IS DISTINCT FROM NEW.prepared OR OLD.created_generation IS DISTINCT FROM NEW.created_generation OR
           OLD.parent_id IS DISTINCT FROM NEW.parent_id OR OLD.source_cursor IS DISTINCT FROM NEW.source_cursor OR
           (OLD.candidate IS NOT NULL AND OLD.candidate IS DISTINCT FROM NEW.candidate)
           OR (OLD.candidate_digest IS NOT NULL AND OLD.candidate_digest IS DISTINCT FROM NEW.candidate_digest)
           OR (OLD.result_digest IS NOT NULL AND OLD.result_digest IS DISTINCT FROM NEW.result_digest)
        THEN RAISE EXCEPTION 'CONTEXT_OPERATION_IMMUTABLE'; END IF;
      ELSE
        IF OLD.purpose IS DISTINCT FROM NEW.purpose OR OLD.context_snapshot_id IS DISTINCT FROM NEW.context_snapshot_id OR
           OLD.context_snapshot_version IS DISTINCT FROM NEW.context_snapshot_version OR OLD.context_snapshot_digest IS DISTINCT FROM NEW.context_snapshot_digest OR
           OLD.provider_kind IS DISTINCT FROM NEW.provider_kind OR OLD.profile_identity IS DISTINCT FROM NEW.profile_identity OR
           OLD.request_digest IS DISTINCT FROM NEW.request_digest OR OLD.request_assessment IS DISTINCT FROM NEW.request_assessment OR
           OLD.compaction_operation_id IS DISTINCT FROM NEW.compaction_operation_id
        THEN RAISE EXCEPTION 'CONTEXT_ATTEMPT_IMMUTABLE'; END IF;
      END IF;
      RETURN NEW;
    END $$;
    """)
    for table in ("runtime_context_snapshots", "runtime_context_operations", "runtime_loop_frames", "runtime_loop_iterations", "runtime_model_invocations"):
        op.execute(f"CREATE TRIGGER {table}_context_guard BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION runtime_context_immutable_guard()")


def downgrade():
    raise RuntimeError("CONTEXT_HISTORY_MUST_BE_PRESERVED")
