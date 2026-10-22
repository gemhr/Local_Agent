"""人工操作、同版本验证与主动关闭；不修改历史执行正文。"""
import re
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0037_incident_resolution"
down_revision = "0036_ci_incident_diagnosis"
branch_labels = None
depends_on = None


def upgrade():
    checks = sa.inspect(op.get_bind()).get_check_constraints("incidents")
    old = [c for c in checks if re.sub(r"[\s()]", "", c["sqltext"]).replace("::text", "") == "status='OPEN'"]
    if len(old) != 1:
        raise RuntimeError("CI_ORIGINAL_STATUS_CONSTRAINT_NOT_FOUND")
    op.drop_constraint(old[0]["name"], "incidents", type_="check")
    op.add_column("ci_executions", sa.Column("state_revision", sa.Integer(), nullable=False, server_default="1"))
    op.alter_column("ci_executions", "state_revision", server_default=None)
    op.create_check_constraint("ck_ci_execution_state_revision", "ci_executions", "state_revision > 0")
    op.add_column("incidents", sa.Column("closed_at", sa.DateTime(timezone=True)))
    op.add_column("incidents", sa.Column("closed_by_user_id", sa.Uuid()))
    op.create_foreign_key("fk_incident_closed_by", "incidents", "users", ["closed_by_user_id"], ["id"])
    op.add_column("incidents", sa.Column("closure_payload", JSONB()))
    op.create_check_constraint("ck_incident_status", "incidents", "status IN ('OPEN','PENDING_VERIFICATION','CLOSED')")
    op.create_check_constraint("ck_incident_closure", "incidents", "(status = 'CLOSED' AND closed_at IS NOT NULL AND closed_by_user_id IS NOT NULL AND closure_payload IS NOT NULL) OR (status <> 'CLOSED' AND closed_at IS NULL AND closed_by_user_id IS NULL AND closure_payload IS NULL)")
    from core.incidents.models import IncidentActionRow, IncidentVerificationRow
    for row in (IncidentActionRow, IncidentVerificationRow):
        row.__table__.create(op.get_bind())


def downgrade():
    op.drop_table("incident_verifications")
    op.drop_table("incident_actions")
    op.drop_constraint("ck_incident_closure", "incidents", type_="check")
    op.drop_constraint("ck_incident_status", "incidents", type_="check")
    op.drop_constraint("fk_incident_closed_by", "incidents", type_="foreignkey")
    for name in ("closure_payload", "closed_by_user_id", "closed_at"):
        op.drop_column("incidents", name)
    op.create_check_constraint("incidents_status_check", "incidents", "status = 'OPEN'")
    op.drop_constraint("ck_ci_execution_state_revision", "ci_executions", type_="check")
    op.drop_column("ci_executions", "state_revision")
