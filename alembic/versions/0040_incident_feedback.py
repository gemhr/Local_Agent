"""调查人工反馈增量表；不修改既有事实。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0040_incident_feedback"
down_revision = "0039_feature_impact"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('incident_investigation_feedback',
        sa.Column('feedback_id', sa.Text, primary_key=True),
        sa.Column('incident_id', sa.Text, sa.ForeignKey('incidents.incident_id'), nullable=False),
        sa.Column('investigation_id', sa.Text, sa.ForeignKey('incident_investigations.investigation_id'), nullable=False),
        sa.Column('tenant_id', sa.String(64), sa.ForeignKey('tenants.tenant_id'), nullable=False),
        sa.Column('created_by_user_id', sa.Uuid, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('request_id', sa.Text, nullable=False),
        sa.Column('request_payload', JSONB, nullable=False),
        sa.Column('binding', JSONB, nullable=False),
        sa.Column('supersedes', sa.Text, sa.ForeignKey('incident_investigation_feedback.feedback_id'), unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('tenant_id', 'created_by_user_id', 'request_id'))
    op.create_index('ix_incident_investigation_feedback_incident_id', 'incident_investigation_feedback', ['incident_id'])
    op.execute("""CREATE FUNCTION incident_feedback_frozen_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'CI_FEEDBACK_IMMUTABLE'; END; $$""")
    op.execute("""CREATE TRIGGER incident_feedback_frozen BEFORE UPDATE OR DELETE ON incident_investigation_feedback
    FOR EACH ROW EXECUTE FUNCTION incident_feedback_frozen_guard()""")


def downgrade():
    op.drop_table('incident_investigation_feedback')
    op.execute('DROP FUNCTION incident_feedback_frozen_guard()')
