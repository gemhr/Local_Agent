"""允许人工选择适用 Oracle 参考；不改变历史执行。"""
from alembic import op
import sqlalchemy as sa

revision = "0044_oracle_reference_action"
down_revision = "0043_baseband_input_action"
branch_labels = None
depends_on = None


def replace_check(values):
    checks = [c for c in sa.inspect(op.get_bind()).get_check_constraints("incident_actions") if "action_kind" in c["sqltext"]]
    if len(checks) != 1:
        raise RuntimeError("CI_ACTION_CONSTRAINT_NOT_FOUND")
    op.drop_constraint(checks[0]["name"], "incident_actions", type_="check")
    op.create_check_constraint("ck_incident_action_kind", "incident_actions", "action_kind IN (" + values + ")")


def upgrade():
    replace_check("'UPGRADE_PARSER_DECODER','SET_FEATURE_FLAG','REFRESH_CASE_INPUT','DELIVER_PRODUCT_FIX','ALIGN_BASEBAND_INPUT','SELECT_BASEBAND_REFERENCE'")


def downgrade():
    replace_check("'UPGRADE_PARSER_DECODER','SET_FEATURE_FLAG','REFRESH_CASE_INPUT','DELIVER_PRODUCT_FIX','ALIGN_BASEBAND_INPUT'")
