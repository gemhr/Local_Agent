"""可信 Memory Scope、操作、Run 来源绑定及历史隔离。"""

from alembic import op
import sqlalchemy as sa

revision = "0033_scoped_memory_lifecycle"
down_revision = "0032_context_compaction"
branch_labels = None
depends_on = None


def upgrade():
    from core.persistence.models import (MemoryScopeRow, MemoryOperationRow, RuntimeMemoryBindingRow,
        LongTermMemoryRow, ProjectSemanticMemoryRow, MessageRow, MessageExchangeRow, ConversationSummaryRow)
    for model in (MemoryScopeRow, MemoryOperationRow, RuntimeMemoryBindingRow):
        model.__table__.create(op.get_bind())
    names = ("access_scope_id", "record_version", "expires_at", "lifecycle_status", "body_state", "body_digest",
        "source_identity", "source_dependencies", "epistemic_kind", "memory_policy_version")
    for model in (LongTermMemoryRow, ProjectSemanticMemoryRow, MessageRow, MessageExchangeRow, ConversationSummaryRow):
        table = model.__tablename__
        existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
        for name in names:
            if name not in existing:
                column = model.__table__.c[name]
                op.add_column(table, sa.Column(name, column.type, nullable=column.nullable, server_default=column.server_default))
        fks = {fk["name"] for fk in sa.inspect(op.get_bind()).get_foreign_keys(table)}
        checks = {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints(table)}
        for constraint in model.__table__.constraints:
            if isinstance(constraint, sa.ForeignKeyConstraint) and constraint.name and constraint.name.endswith("_access_scope") and constraint.name not in fks:
                op.execute(sa.schema.AddConstraint(constraint))
            if isinstance(constraint, sa.CheckConstraint) and constraint.name.endswith("_lifecycle") and constraint.name not in checks:
                op.create_check_constraint(constraint.name, table, str(constraint.sqltext))
    op.add_column("runtime_model_invocations", sa.Column("memory_operation_id", sa.String(255)))
    op.create_check_constraint("ck_model_memory_purpose", "runtime_model_invocations", "(purpose = 'FORMATION') = (memory_operation_id IS NOT NULL)")
    from core.persistence.models import RuntimeModelInvocationRow
    for name in ("ck_runtime_model_loop_binding", "ck_model_context_purpose"):
        op.drop_constraint(name, "runtime_model_invocations", type_="check")
        constraint = next(c for c in RuntimeModelInvocationRow.__table__.constraints if c.name == name)
        op.create_check_constraint(name, "runtime_model_invocations", str(constraint.sqltext))
    op.create_foreign_key("fk_model_memory_operation", "runtime_model_invocations", "memory_operations", ["memory_operation_id"], ["operation_id"])
    # 不把当前用户套到任何旧记录上。旧 scope NULL 的历史由新 SQL 排除。
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("conversation_summaries")}
    if "summary_id" not in columns:
        op.add_column("conversation_summaries", sa.Column("summary_id", sa.String(255)))
        op.execute("UPDATE conversation_summaries SET summary_id = 'legacy:' || agent_id")
        old = sa.inspect(op.get_bind()).get_pk_constraint("conversation_summaries")
        op.drop_constraint(old["name"], "conversation_summaries", type_="primary")
        op.alter_column("conversation_summaries", "summary_id", nullable=False)
        op.create_primary_key("conversation_summaries_pkey", "conversation_summaries", ["summary_id"])
    op.execute("CREATE UNIQUE INDEX uq_summary_access_scope ON conversation_summaries(access_scope_id) WHERE access_scope_id IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX uq_memory_active_key ON long_term_memory(access_scope_id,memory_type,logical_key) WHERE access_scope_id IS NOT NULL AND logical_key IS NOT NULL AND lifecycle_status = 'ACTIVE'")
    op.execute("CREATE UNIQUE INDEX uq_project_memory_active_key ON project_semantic_memory(access_scope_id,logical_key) WHERE access_scope_id IS NOT NULL AND lifecycle_status = 'ACTIVE'")
    for name in ("uq_long_term_memory_episodic_run_identity", "uq_long_term_memory_episodic_step_identity"):
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute("CREATE UNIQUE INDEX uq_scoped_episode_identity ON long_term_memory(access_scope_id,origin_run_id,COALESCE(payload->>'origin_step_id','')) WHERE access_scope_id IS NOT NULL AND memory_type = 'EPISODIC'")
    op.execute("""
    CREATE OR REPLACE FUNCTION memory_dependencies_valid(deps jsonb) RETURNS boolean LANGUAGE sql VOLATILE AS $$
      SELECT CASE WHEN jsonb_typeof(deps) <> 'array' OR jsonb_array_length(deps) > 128 THEN false ELSE NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(deps) d WHERE
          NOT EXISTS (SELECT 1 FROM memory_scopes s WHERE s.scope_id=d->>'scope_id' AND s.status='ACTIVE'
            AND s.authz_epoch::text=d->>'epoch' AND (s.owner_user_id IS NULL OR EXISTS (
              SELECT 1 FROM users u WHERE u.id=s.owner_user_id AND u.tenant_id=s.tenant_id AND u.disabled_at IS NULL)))
          OR (d->>'kind'='RECORD' AND NOT EXISTS (
            SELECT 1 FROM (
              SELECT memory_id,access_scope_id,record_version,body_digest,lifecycle_status,body_state,expires_at,source_identity FROM long_term_memory
              UNION ALL
              SELECT memory_id,access_scope_id,record_version,body_digest,lifecycle_status,body_state,expires_at,source_identity FROM project_semantic_memory
            ) r WHERE r.memory_id=d->>'id' AND r.access_scope_id=d->>'scope_id' AND r.record_version::text=d->>'version'
              AND r.body_digest=d->>'digest' AND r.lifecycle_status='ACTIVE' AND r.body_state='PRESENT'
              AND (r.expires_at IS NULL OR r.expires_at>clock_timestamp()) AND NOT EXISTS (
                SELECT 1 FROM memory_operations o WHERE o.scope_id=r.access_scope_id AND o.kind='SUPPRESS' AND o.source_identity=r.source_identity)))
          OR (d->>'kind'='EXCHANGE' AND NOT EXISTS (
            SELECT 1 FROM message_exchanges e WHERE e.exchange_id=d->>'id' AND e.access_scope_id=d->>'scope_id'
              AND e.record_version::text=d->>'version' AND e.body_digest=d->>'digest' AND e.lifecycle_status='ACTIVE'
              AND e.body_state='PRESENT' AND e.state='COMMITTED' AND (e.expires_at IS NULL OR e.expires_at>clock_timestamp())
              AND NOT EXISTS (SELECT 1 FROM memory_operations o WHERE o.scope_id=e.access_scope_id AND o.kind='SUPPRESS' AND o.source_identity=e.source_identity)))
          OR (d->>'kind'='USER_MESSAGE' AND NOT EXISTS (
            SELECT 1 FROM messages m JOIN message_exchanges e ON e.exchange_id=m.exchange_id AND e.access_scope_id=m.access_scope_id
            WHERE m.id::text=d->>'id' AND m.access_scope_id=d->>'scope_id' AND m.record_version::text=d->>'version'
              AND m.body_digest=d->>'digest' AND m.role='user' AND m.sequence=0 AND e.user_message_id=m.id
              AND m.lifecycle_status='ACTIVE' AND m.body_state='PRESENT' AND jsonb_array_length(m.source_dependencies)=0
              AND e.state='COMMITTED' AND e.lifecycle_status='ACTIVE' AND (m.expires_at IS NULL OR m.expires_at>clock_timestamp())
              AND NOT EXISTS (SELECT 1 FROM memory_operations o WHERE o.scope_id=m.access_scope_id AND o.kind='SUPPRESS' AND o.source_identity=m.source_identity)))
          OR COALESCE(d->>'kind','') NOT IN ('RECORD','EXCHANGE','USER_MESSAGE','SCOPE')
      ) END
    $$;
    """)
    op.execute("""
    CREATE OR REPLACE FUNCTION memory_scope_immutable_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.scope_id IS DISTINCT FROM NEW.scope_id OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
        OR OLD.owner_user_id IS DISTINCT FROM NEW.owner_user_id OR OLD.scope_kind IS DISTINCT FROM NEW.scope_kind
        OR OLD.agent_id IS DISTINCT FROM NEW.agent_id OR OLD.session_id IS DISTINCT FROM NEW.session_id OR OLD.project_id IS DISTINCT FROM NEW.project_id
      THEN RAISE EXCEPTION 'MEMORY_SCOPE_IMMUTABLE'; END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute("""
    CREATE TRIGGER memory_scope_guard BEFORE UPDATE ON memory_scopes FOR EACH ROW EXECUTE FUNCTION memory_scope_immutable_guard();
    """)


def downgrade():
    raise RuntimeError("MEMORY_SUPPRESSION_HISTORY_MUST_BE_PRESERVED")
