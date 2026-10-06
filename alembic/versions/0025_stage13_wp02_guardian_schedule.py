"""WP02 固定 Guardian/Cycle/Version/Due/Hold schema；只新增 migration。"""

from alembic import op

revision = "0025_stage13_wp02_guardian"
down_revision = "0024_stage13_wp01_provider"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "\nCREATE TABLE stage13_guardians (\n\tguardian_id VARCHAR(36) NOT NULL, \n\tguardian_key VARCHAR(64) NOT NULL, \n\tscope VARCHAR(128) NOT NULL, \n\tproject VARCHAR(128) NOT NULL, \n\tsuite VARCHAR(128) NOT NULL, \n\tenvironment VARCHAR(128) NOT NULL, \n\tchannel VARCHAR(128) NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tPRIMARY KEY (guardian_id), \n\tCONSTRAINT ck_s13_guardian_status CHECK (status IN ('ACTIVE','DISABLED')), \n\tUNIQUE (guardian_key)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE stage13_scheduler_state (\n\tscope VARCHAR(128) NOT NULL, \n\tlogical_now TIMESTAMP WITH TIME ZONE, \n\tsubmit_starts JSONB NOT NULL, \n\tread_starts JSONB NOT NULL, \n\trecovery_generation BIGINT NOT NULL, \n\trecovery_id VARCHAR(128), \n\tstale_writes_rejected BIGINT NOT NULL, \n\tPRIMARY KEY (scope)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE stage13_daily_cycles (\n\tcycle_id VARCHAR(36) NOT NULL, \n\tcycle_key VARCHAR(64) NOT NULL, \n\tguardian_id VARCHAR(36) NOT NULL, \n\tbusiness_date VARCHAR(10) NOT NULL, \n\tplan JSONB NOT NULL, \n\tplan_digest VARCHAR(64) NOT NULL, \n\tchannel VARCHAR(128) NOT NULL, \n\ttimezone VARCHAR(32) NOT NULL, \n\teligible_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tdeadline_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tdiscovered_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tstatus VARCHAR(32) NOT NULL, \n\treason VARCHAR(64), \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (cycle_id), \n\tCONSTRAINT uq_s13_cycle_day UNIQUE (guardian_id, business_date), \n\tCONSTRAINT ck_s13_cycle_status CHECK (status IN ('CREATED','READY','RUNNING','SUCCEEDED','COMPLETED_WITH_FAILURES','FAILED','UNRESOLVED','CANCELLED','SKIPPED_OVERLAP')), \n\tUNIQUE (cycle_key), \n\tFOREIGN KEY(guardian_id) REFERENCES stage13_guardians (guardian_id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE stage13_environment_occupancy (\n\tscope VARCHAR(128) NOT NULL, \n\tenvironment VARCHAR(128) NOT NULL, \n\tcycle_id VARCHAR(36), \n\tsafety_hold BOOLEAN NOT NULL, \n\thold_acquired_at TIMESTAMP WITH TIME ZONE, \n\tresolutions JSONB NOT NULL, \n\tPRIMARY KEY (scope, environment), \n\tFOREIGN KEY(cycle_id) REFERENCES stage13_daily_cycles (cycle_id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE stage13_version_executions (\n\tversion_execution_id VARCHAR(36) NOT NULL, \n\tversion_execution_key VARCHAR(64) NOT NULL, \n\tcycle_id VARCHAR(36) NOT NULL, \n\tordinal INTEGER NOT NULL, \n\tproduct_version VARCHAR(128) NOT NULL, \n\texpected_cases INTEGER NOT NULL, \n\trequest JSONB NOT NULL, \n\tbusiness_key VARCHAR(64) NOT NULL, \n\trequest_digest VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(32) NOT NULL, \n\treason VARCHAR(64), \n\tbinding_status VARCHAR(16) NOT NULL, \n\tknowledge_state VARCHAR(16) NOT NULL, \n\tremote_id VARCHAR(36), \n\treceipt JSONB, \n\tintent_at TIMESTAMP WITH TIME ZONE, \n\tbound_at TIMESTAMP WITH TIME ZONE, \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\tsubmit_attempts INTEGER NOT NULL, \n\treconciliation_requests INTEGER NOT NULL, \n\treconciliation_errors INTEGER NOT NULL, \n\tunknown_entries INTEGER NOT NULL, \n\tunknown_recoveries INTEGER NOT NULL, \n\tfirst_unknown_at TIMESTAMP WITH TIME ZONE, \n\tnext_poll_at TIMESTAMP WITH TIME ZONE, \n\tlast_poll_at TIMESTAMP WITH TIME ZONE, \n\tlast_successful_poll_at TIMESTAMP WITH TIME ZONE, \n\tlast_observed_state VARCHAR(32), \n\tlast_observed_digest VARCHAR(64), \n\tlast_result_revision INTEGER NOT NULL, \n\tlast_status_revision INTEGER NOT NULL, \n\tpoll_sequence INTEGER NOT NULL, \n\tpoll_requests INTEGER NOT NULL, \n\tsuccessful_polls INTEGER NOT NULL, \n\tconsecutive_poll_errors INTEGER NOT NULL, \n\tresult_retries INTEGER NOT NULL, \n\tcounts JSONB, \n\tterminal_summary JSONB, \n\tPRIMARY KEY (version_execution_id), \n\tCONSTRAINT uq_s13_cycle_ordinal UNIQUE (cycle_id, ordinal), \n\tCONSTRAINT ck_s13_version_plan CHECK (ordinal BETWEEN 1 AND 3 AND expected_cases > 0), \n\tCONSTRAINT ck_s13_attempt_budget CHECK (submit_attempts BETWEEN 0 AND 3 AND reconciliation_requests BETWEEN 0 AND 60 AND result_retries BETWEEN 0 AND 3), \n\tCONSTRAINT ck_s13_knowledge CHECK (binding_status IN ('UNBOUND','BOUND') AND knowledge_state IN ('KNOWN','UNKNOWN')), \n\tCONSTRAINT ck_s13_binding CHECK ((binding_status='UNBOUND' AND remote_id IS NULL AND receipt IS NULL) OR (binding_status='BOUND' AND remote_id IS NOT NULL AND receipt IS NOT NULL)), \n\tCONSTRAINT ck_s13_version_status CHECK (status IN ('PLANNED','DISPATCHING','ACTIVE','COMPLETED','INFRA_FAILED','DISPATCH_FAILED','UNRESOLVED','CANCELLED','SKIPPED')), \n\tUNIQUE (version_execution_key), \n\tFOREIGN KEY(cycle_id) REFERENCES stage13_daily_cycles (cycle_id), \n\tUNIQUE (business_key), \n\tUNIQUE (remote_id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE stage13_due_work (\n\twork_key VARCHAR(64) NOT NULL, \n\tscope VARCHAR(128) NOT NULL, \n\toperation VARCHAR(32) NOT NULL, \n\tversion_execution_id VARCHAR(36) NOT NULL, \n\tsequence INTEGER NOT NULL, \n\teligible_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\toriginal_due_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tnext_available_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tstate VARCHAR(16) NOT NULL, \n\tclaimed_at TIMESTAMP WITH TIME ZONE, \n\tclaim_token VARCHAR(36), \n\tclaim_epoch BIGINT NOT NULL, \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tstarted_at TIMESTAMP WITH TIME ZONE, \n\tbusiness_started_at TIMESTAMP WITH TIME ZONE, \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\trecovery_generation BIGINT NOT NULL, \n\ttakeovers INTEGER NOT NULL, \n\terror VARCHAR(64), \n\tPRIMARY KEY (work_key), \n\tCONSTRAINT uq_s13_due_operation UNIQUE (version_execution_id, operation, sequence), \n\tCONSTRAINT ck_s13_due_state CHECK (operation IN ('DISPATCH_VERSION','POLL_REMOTE','RECONCILE_REMOTE','FETCH_TERMINAL_RESULT') AND state IN ('READY','CLAIMED','COMPLETED')), \n\tFOREIGN KEY(version_execution_id) REFERENCES stage13_version_executions (version_execution_id)\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_s13_due_candidate ON stage13_due_work (scope, next_available_at, work_key) WHERE completed_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_s13_due_lease ON stage13_due_work (scope, lease_until) WHERE state='CLAIMED'"
    )
    op.execute(
        "\nCREATE TABLE stage13_observations (\n\twork_key VARCHAR(64) NOT NULL, \n\tversion_execution_id VARCHAR(36) NOT NULL, \n\tread_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tbusiness_read_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tsource_observed_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tsemantic_digest VARCHAR(64) NOT NULL, \n\tpacket_digest VARCHAR(64) NOT NULL, \n\tchanged BOOLEAN NOT NULL, \n\tevidence JSONB, \n\tPRIMARY KEY (work_key), \n\tFOREIGN KEY(work_key) REFERENCES stage13_due_work (work_key), \n\tFOREIGN KEY(version_execution_id) REFERENCES stage13_version_executions (version_execution_id)\n)\n\n"
    )
    op.execute("""
CREATE FUNCTION stage13_guardian_frozen_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'stage13_daily_cycles' THEN
        IF NEW.cycle_key IS DISTINCT FROM OLD.cycle_key OR NEW.guardian_id IS DISTINCT FROM OLD.guardian_id OR NEW.business_date IS DISTINCT FROM OLD.business_date OR NEW.plan IS DISTINCT FROM OLD.plan OR NEW.plan_digest IS DISTINCT FROM OLD.plan_digest OR NEW.channel IS DISTINCT FROM OLD.channel OR NEW.timezone IS DISTINCT FROM OLD.timezone OR NEW.eligible_at IS DISTINCT FROM OLD.eligible_at OR NEW.deadline_at IS DISTINCT FROM OLD.deadline_at THEN
            RAISE EXCEPTION 'STAGE13_CYCLE_PLAN_IMMUTABLE';
        END IF;
        IF OLD.status IN ('SUCCEEDED','COMPLETED_WITH_FAILURES','FAILED','UNRESOLVED','CANCELLED','SKIPPED_OVERLAP') AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'STAGE13_CYCLE_TERMINAL_IMMUTABLE';
        END IF;
    ELSIF TG_TABLE_NAME = 'stage13_version_executions' THEN
        IF NEW.version_execution_key IS DISTINCT FROM OLD.version_execution_key OR NEW.cycle_id IS DISTINCT FROM OLD.cycle_id OR NEW.ordinal IS DISTINCT FROM OLD.ordinal OR NEW.product_version IS DISTINCT FROM OLD.product_version OR NEW.expected_cases IS DISTINCT FROM OLD.expected_cases OR NEW.request IS DISTINCT FROM OLD.request OR NEW.business_key IS DISTINCT FROM OLD.business_key OR NEW.request_digest IS DISTINCT FROM OLD.request_digest OR (OLD.remote_id IS NOT NULL AND (NEW.remote_id IS DISTINCT FROM OLD.remote_id OR NEW.receipt IS DISTINCT FROM OLD.receipt)) THEN
            RAISE EXCEPTION 'STAGE13_VERSION_BINDING_IMMUTABLE';
        END IF;
        IF OLD.status IN ('COMPLETED','INFRA_FAILED','DISPATCH_FAILED','UNRESOLVED','CANCELLED','SKIPPED') AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'STAGE13_VERSION_TERMINAL_IMMUTABLE';
        END IF;
    END IF;
    RETURN NEW;
END $$
""")
    op.execute(
        "CREATE TRIGGER stage13_cycle_frozen BEFORE UPDATE ON stage13_daily_cycles FOR EACH ROW EXECUTE FUNCTION stage13_guardian_frozen_guard()"
    )
    op.execute(
        "CREATE TRIGGER stage13_version_frozen BEFORE UPDATE ON stage13_version_executions FOR EACH ROW EXECUTE FUNCTION stage13_guardian_frozen_guard()"
    )


def downgrade():
    op.drop_table("stage13_observations")
    op.drop_table("stage13_due_work")
    op.drop_table("stage13_version_executions")
    op.drop_table("stage13_environment_occupancy")
    op.drop_table("stage13_daily_cycles")
    op.drop_table("stage13_scheduler_state")
    op.drop_table("stage13_guardians")
    op.execute("DROP FUNCTION stage13_guardian_frozen_guard()")
