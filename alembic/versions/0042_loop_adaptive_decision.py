"""根调查 Frame 的可空终结限制；旧 Run 保持 NULL。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0042_loop_adaptive_decision"
down_revision = "0041_ci_synthetic_evaluation"
branch_labels = None
depends_on = None


def upgrade():
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("runtime_loop_frames")}
    if "adaptive_decision" not in existing:
        op.add_column("runtime_loop_frames", sa.Column("adaptive_decision", JSONB(), nullable=True))
    op.execute("""CREATE OR REPLACE FUNCTION runtime_loop_adaptive_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.adaptive_decision IS NOT NULL AND OLD.adaptive_decision IS DISTINCT FROM NEW.adaptive_decision
      THEN RAISE EXCEPTION 'LOOP_ADAPTIVE_DECISION_IMMUTABLE'; END IF;
      RETURN NEW;
    END $$;""")
    op.execute("""CREATE TRIGGER runtime_loop_adaptive_guard BEFORE UPDATE ON runtime_loop_frames
    FOR EACH ROW EXECUTE FUNCTION runtime_loop_adaptive_guard()""")


def downgrade():
    raise RuntimeError("DURABLE_LOOP_HISTORY_MUST_BE_PRESERVED")
