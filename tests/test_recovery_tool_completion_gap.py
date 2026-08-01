from __future__ import annotations

from dataclasses import replace

from core.runtime import (
    InMemoryRunEventJournal,
    JournalRecord,
    RecoveryReason,
    RecoveryStatus,
    RecoveryValidator,
    RuntimeEventType,
    ToolCompletedPayload,
    ToolCompletionGapFixture,
    ToolRecoveryDecisionStatus,
    ToolStartedPayload,
    safe_key_digest,
)
from tests._recovery_fixtures import (
    recovery_plan,
    recovery_snapshot,
    runtime_event,
)


def started_payload() -> ToolStartedPayload:
    return ToolStartedPayload(
        tool_name="writer",
        retry_index=0,
        tool_evidence_schema_version=1,
        invocation_identity_digest=safe_key_digest("invocation"),
        attempt_identity_digest=safe_key_digest("attempt"),
        side_effect_kind="EXTERNAL_STATE_MUTATION",
        idempotency_kind="NON_IDEMPOTENT",
        replay_supported=False,
        side_effect_state="NOT_STARTED",
        compensation_state="NOT_ATTEMPTED",
        retry_disposition="PENDING",
        outcome_classification="PENDING",
        execution_detached=False,
        worker_terminated=False,
        provider_started=False,
    )


def completed_payload() -> ToolCompletedPayload:
    return ToolCompletedPayload(
        tool_name="writer",
        succeeded=True,
        retry_index=0,
        side_effect_state="COMMITTED",
        retry_disposition="UNSAFE",
        worker_terminated=True,
        execution_detached=False,
        duration_ms=1,
        status="SUCCEEDED",
        tool_evidence_schema_version=1,
        invocation_identity_digest=safe_key_digest("invocation"),
        attempt_identity_digest=safe_key_digest("attempt"),
        side_effect_kind="EXTERNAL_STATE_MUTATION",
        idempotency_kind="NON_IDEMPOTENT",
        replay_supported=False,
        compensation_state="NOT_ATTEMPTED",
        outcome_classification="SUCCEEDED",
        provider_started=True,
    )


def journal_with_started(*, completed=False) -> InMemoryRunEventJournal:
    journal = InMemoryRunEventJournal()
    journal.append(
        runtime_event(
            1,
            RuntimeEventType.TOOL_STARTED,
            started_payload(),
            step_id="step",
            step_sequence=1,
        )
    )
    if completed:
        journal.append(
            runtime_event(
                2,
                RuntimeEventType.TOOL_COMPLETED,
                completed_payload(),
                step_id="step",
                step_sequence=2,
            )
        )
    return journal


def assess(journal):
    return RecoveryValidator(journal=journal).assess_snapshot(
        snapshot=recovery_snapshot(), current_plan=recovery_plan()
    )


def assert_no_automatic_action(result):
    assert not result.automatic_resume_supported
    assert not result.model_replay_allowed
    assert not result.tool_replay_allowed
    assert not result.retrieval_replay_allowed
    assert all(not item.automatic_action_allowed for item in result.tool_decisions)


def test_started_without_completed_requires_reconciliation_from_durable_tail():
    result = assess(journal_with_started())

    assert result.status is RecoveryStatus.REQUIRES_RECONCILIATION
    assert len(result.tool_evidence) == 1
    assert result.tool_evidence[0].event_kind == "STARTED"
    assert result.tool_decisions[0].status in {
        ToolRecoveryDecisionStatus.MANUAL_RECONCILIATION,
        ToolRecoveryDecisionStatus.INSUFFICIENT_EVIDENCE,
    }
    assert any(
        reason
        in {
            RecoveryReason.TOOL_OUTCOME_UNKNOWN,
            RecoveryReason.TOOL_SIDE_EFFECT_EVIDENCE,
            RecoveryReason.TOOL_EVIDENCE_INSUFFICIENT,
        }
        for reason in result.reasons
    )
    assert_no_automatic_action(result)


def test_committed_local_oracle_and_lost_local_evidence_have_same_assessment():
    expected_real_world_fact = ToolCompletionGapFixture(
        True, False, False, True, True, "COMMITTED", "UNSAFE", "SUCCEEDED"
    )
    lost_local_oracle = ToolCompletionGapFixture(
        True,
        False,
        False,
        False,
        None,
        "UNKNOWN",
        "OUTCOME_UNKNOWN",
        "EVIDENCE_LOST",
    )
    durable_recovery_input = journal_with_started()
    snapshot = recovery_snapshot()
    validator = RecoveryValidator(journal=durable_recovery_input)

    with_committed_oracle = validator.assess_snapshot(
        snapshot=snapshot, current_plan=recovery_plan()
    )
    without_local_oracle = validator.assess_snapshot(
        snapshot=snapshot, current_plan=recovery_plan()
    )

    assert expected_real_world_fact.side_effect_state == "COMMITTED"
    assert lost_local_oracle.local_completion_evidence_present is False
    assert with_committed_oracle == without_local_oracle
    assert RecoveryStatus(with_committed_oracle.status) is RecoveryStatus.REQUIRES_RECONCILIATION
    assert_no_automatic_action(with_committed_oracle)


class CorruptedStartedJournal:
    def __init__(self):
        event = runtime_event(
            1,
            RuntimeEventType.TOOL_STARTED,
            started_payload(),
            step_id="step",
            step_sequence=1,
        )
        self.record = replace(JournalRecord.from_event(event), event_digest="0" * 64)

    def last_sequence(self, run_id):
        return 1

    def read_after(self, run_id, sequence, limit):
        return (self.record,)


def test_corrupted_started_record_is_not_downgraded_to_missing_started():
    result = assess(CorruptedStartedJournal())
    assert result.status is RecoveryStatus.CORRUPTED
    assert result.reasons == (RecoveryReason.JOURNAL_RECORD_CORRUPTED,)
    assert result.tool_evidence == ()
    assert_no_automatic_action(result)


def test_persisted_completed_fact_comes_only_from_journal_evidence():
    result = assess(journal_with_started(completed=True))
    assert [item.event_kind for item in result.tool_evidence] == [
        "STARTED",
        "COMPLETED",
    ]
    assert result.tool_evidence[-1].side_effect_state == "COMMITTED"
    assert result.tool_decisions[0].status is not ToolRecoveryDecisionStatus.INSUFFICIENT_EVIDENCE
    assert_no_automatic_action(result)


def test_recovery_validation_invokes_no_model_tool_retrieval_or_compensation():
    counters = {
        "model": 0,
        "tool": 0,
        "retrieval": 0,
        "compensation": 0,
        "state_mutation": 0,
    }
    snapshot = recovery_snapshot()
    before_digest = snapshot.payload_digest
    result = RecoveryValidator(journal=journal_with_started()).assess_snapshot(
        snapshot=snapshot, current_plan=recovery_plan()
    )
    assert counters == {name: 0 for name in counters}
    assert snapshot.payload_digest == before_digest
    assert_no_automatic_action(result)
