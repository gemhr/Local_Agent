"""WP6 recovery matrix supplement: PREPARED interruption + no-replay invariants."""

from __future__ import annotations

from core.runtime.checkpoint_contract import CheckpointKind
from core.runtime.event_journal_store import InMemoryRunEventJournal
from core.runtime.recovery_contract import RecoveryReason, RecoveryStatus
from core.runtime.recovery_validation import RecoveryValidator
from tests._recovery_fixtures import activity, recovery_plan, recovery_snapshot
from tests.test_recovery_delivery_boundary import (
    assess,
    started_step_chain,
)


def test_store_prepared_interruption_fails_closed() -> None:
    """FP-REC-03: Store PREPARED 中断（Step RUNNING、无 result 证据）：
    Recovery 不恢复 Store/Bindings，fail closed。"""
    state = __import__(
        "core.runtime.state", fromlist=["AgentState"]
    ).AgentState("run")
    state.mark_running()
    state.add_step("step", "step")
    state.start_step("step")
    snapshot = recovery_snapshot(
        state=state,
        kind=CheckpointKind.NON_QUIESCENT_AUDIT,
        activity_snapshot=activity(
            running_step_count=1,
            step_workers_active=1,
        ),
        quiescent=False,
    )
    result = assess(snapshot, InMemoryRunEventJournal())

    assert result.status is RecoveryStatus.REQUIRES_RECONCILIATION
    assert RecoveryReason.RUNNING_STEP_PRESENT in result.reasons
    assert not result.resume_data_availability.result_rehydration_supported
    assert not result.automatic_resume_supported


def _assert_no_redelivery_no_recommit(result) -> None:
    """恢复判定永远不携带重发、重写或 Gate/Store 恢复能力。"""
    assert not result.output_reconstruction_supported
    assert not result.automatic_resume_supported
    assert not result.model_replay_allowed
    assert not result.tool_replay_allowed
    assert not result.retrieval_replay_allowed
    assert not result.resume_data_availability.result_rehydration_supported
    assert not result.resume_data_availability.output_reconstruction_supported


def test_delivery_unknown_never_resends_or_rewrites() -> None:
    journal = InMemoryRunEventJournal()
    for event in started_step_chain(
        delivery="OUTCOME_UNKNOWN", memory="NOT_ATTEMPTED"
    ):
        journal.append(event)
    result = assess(recovery_snapshot(), journal)
    assert (
        RecoveryReason.FINAL_OUTPUT_DELIVERY_UNKNOWN in result.reasons
    )
    _assert_no_redelivery_no_recommit(result)


def test_delivered_memory_unknown_never_rewrites() -> None:
    journal = InMemoryRunEventJournal()
    for event in started_step_chain(delivery="DELIVERED"):
        journal.append(event)
    result = assess(recovery_snapshot(), journal)
    assert (
        RecoveryReason.FINAL_OUTPUT_MEMORY_COMMIT_UNKNOWN in result.reasons
    )
    _assert_no_redelivery_no_recommit(result)


def test_memory_committed_without_terminal_never_rewrites_or_resends() -> None:
    journal = InMemoryRunEventJournal()
    for event in started_step_chain(
        delivery="DELIVERED", memory="SUCCEEDED"
    ):
        journal.append(event)
    result = assess(recovery_snapshot(), journal)
    assert RecoveryReason.MEMORY_COMMITTED_WITHOUT_TERMINAL in result.reasons
    _assert_no_redelivery_no_recommit(result)


def test_terminal_delivered_memory_failed_is_not_resumed() -> None:
    journal = InMemoryRunEventJournal()
    for event in started_step_chain(
        delivery="DELIVERED",
        memory="FAILED",
        terminal="FAILED",
    ):
        journal.append(event)
    result = assess(recovery_snapshot(), journal)
    assert result.status is RecoveryStatus.TERMINAL
    _assert_no_redelivery_no_recommit(result)


def test_recovery_validator_has_no_write_or_deliver_capability() -> None:
    """Recovery 是只读验证器：不存在任何重发/重写/恢复 Gate 的方法。"""
    validator = RecoveryValidator(
        snapshot_store=None,
        journal=InMemoryRunEventJournal(),
    )
    public = {
        name
        for name in dir(validator)
        if not name.startswith("_")
    }
    assert "assess_snapshot" in public
    assert "validate" in public
    for forbidden in (
        "resend",
        "redeliver",
        "rewrite",
        "commit_memory",
        "restore_gate",
        "restore_store",
        "restore_bindings",
    ):
        assert not any(forbidden in name.lower() for name in public)
    assert "write" not in {name.lower() for name in public}
