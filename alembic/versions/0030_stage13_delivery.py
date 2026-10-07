"""Stage13 授权交付局部意图及不可变受控 sink。"""

from alembic import op

revision = "0030_stage13_delivery"
down_revision = "0029_stage13_execution_policy"
branch_labels = None
depends_on = None


def upgrade():
    from core.stage13.delivery_models import DeliveryRow, ControlledSinkRow

    DeliveryRow.__table__.create(op.get_bind())
    ControlledSinkRow.__table__.create(op.get_bind())
    op.execute("""
    CREATE FUNCTION stage13_delivery_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'STAGE13_DELIVERY_IMMUTABLE'; END IF;
      IF TG_TABLE_NAME='stage13_controlled_delivery_sink' THEN
        RAISE EXCEPTION 'STAGE13_SINK_IMMUTABLE';
      END IF;
      IF OLD.delivery_id<>NEW.delivery_id OR OLD.request_digest<>NEW.request_digest
         OR OLD.authorization<>NEW.authorization OR OLD.started_at<>NEW.started_at
         OR (OLD.receipt IS NOT NULL AND OLD.receipt IS DISTINCT FROM NEW.receipt)
         OR NEW.epoch<OLD.epoch THEN RAISE EXCEPTION 'STAGE13_DELIVERY_IMMUTABLE'; END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute(
        "CREATE TRIGGER stage13_sink_guard BEFORE UPDATE OR DELETE ON stage13_controlled_delivery_sink FOR EACH ROW EXECUTE FUNCTION stage13_delivery_guard()"
    )
    op.execute(
        "CREATE TRIGGER stage13_delivery_guard BEFORE UPDATE OR DELETE ON stage13_deliveries FOR EACH ROW EXECUTE FUNCTION stage13_delivery_guard()"
    )


def downgrade():
    raise RuntimeError("STAGE13_DELIVERY_HISTORY_MUST_BE_PRESERVED")
