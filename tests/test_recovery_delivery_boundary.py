from __future__ import annotations

from core.runtime.checkpoint_contract import CheckpointKind
from core.runtime.event_journal_store import InMemoryRunEventJournal
from core.runtime.events import (
    ErrorPayload,
    OutputDeltaPayload,
    RunCompletedPayload,
    RunStartedPayload,
    RuntimeEventType,
    StepCompletedPayload,
    StepStartedPayload,
)
from core.runtime.recovery_contract import RecoveryReason, RecoveryStatus
from core.runtime.recovery_validation import RecoveryValidator
from tests._recovery_fixtures import recovery_plan, recovery_snapshot, runtime_event


def assess(snapshot, journal):
    return RecoveryValidator(journal=journal).assess_snapshot(
        snapshot=snapshot,
        current_plan=recovery_plan(),
    )


def started_step_chain(*, delivery=None, memory=None, terminal=None):
    """构建 final step 已启动/完成的可选事件链。"""
    events = [
        runtime_event(
            1,
            RuntimeEventType.RUN_STARTED,
            RunStartedPayload("RUNNING"),
        ),
        runtime_event(
            2,
            RuntimeEventType.STEP_STARTED,
            StepStartedPayload(
                "RUNNING",
                agent_id="router",
                execution_kind="AGENT",
                output_policy="FINAL_PASSTHROUGH",
                dependency_count=0,
            ),
            step_id="step",
            step_sequence=1,
        ),
    ]
    sequence = 3
    if delivery == "journaled":
        events.append(
            runtime_event(
                sequence,
                RuntimeEventType.OUTPUT_DELTA,
                OutputDeltaPayload("final"),
            )
        )
        sequence += 1
    events.append(
        runtime_event(
            sequence,
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload(
                "SUCCEEDED",
                duration_ms=10,
                result_char_count=5,
                delivery_status=delivery,
                delivery_duration_ms=1,
            ),
            step_id="step",
            step_sequence=1,
        )
    )
    sequence += 1
    if memory is not None:
        events.append(
            runtime_event(
                sequence,
                RuntimeEventType.ERROR,
                ErrorPayload(
                    "FINAL_OUTPUT_MEMORY_COMMIT_FAILED"
                    if memory == "FAILED"
                    else "INTERNAL_ERROR",
                    "safe",
                    "step_completion",
                    True,
                    delivery_status=delivery,
                    final_step_status="SUCCEEDED",
                    memory_commit_status=memory,
                ),
            )
        )
        sequence += 1
    if terminal is not None:
        events.append(
            runtime_event(
                sequence,
                RuntimeEventType.RUN_COMPLETED,
                RunCompletedPayload(
                    terminal,
                    "COMPLETED" if terminal == "SUCCEEDED" else "UNHANDLED_ERROR",
                    duration_ms=50,
                    delivery_status=delivery,
                    final_step_status="SUCCEEDED",
                    memory_commit_status=memory,
                ),
            )
        )
    return events


def test_post_plan_checkpoint_is_not_resumable_without_bindings():
    journal = InMemoryRunEventJournal()
    snapshot = recovery_snapshot(
        kind=CheckpointKind.POST_PLAN_PRE_EXECUTION
    )
    result = assess(snapshot, journal)
    assert result.status is RecoveryStatus.UNSUPPORTED
    assert RecoveryReason.UNSUPPORTED_CHECKPOINT_KIND in result.reasons
    assert not result.resume_prerequisites_satisfied


def test_specialist_interruption_fails_closed_without_result_rehydration():
    from tests._recovery_fixtures import activity

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


def test_final_step_succeeded_without_output_journal_is_unsupported():
    journal = InMemoryRunEventJournal()
    for event in started_step_chain(delivery=None):
        journal.append(event)
    result = assess(recovery_snapshot(), journal)
    assert result.status is RecoveryStatus.UNSUPPORTED
    assert (
        RecoveryReason.FINAL_OUTPUT_JOURNAL_FACT_MISSING
        in result.reasons
    )


def test_output_journaled_without_terminal_is_delivery_unknown():
    journal = InMemoryRunEventJournal()
    for event in started_step_chain(delivery="journaled"):
        journal.append(event)
    result = assess(recovery_snapshot(), journal)
    assert result.status is RecoveryStatus.REQUIRES_RECONCILIATION
    assert (
        RecoveryReason.FINAL_OUTPUT_DELIVERY_UNKNOWN in result.reasons
    )
    assert not result.automatic_resume_supported
    assert not result.output_reconstruction_supported


def test_delivery_unknown_never_resumes_or_resends():
    journal = InMemoryRunEventJournal()
    for event in started_step_chain(
        delivery="OUTCOME_UNKNOWN", memory="NOT_ATTEMPTED"
    ):
        journal.append(event)
    result = assess(recovery_snapshot(), journal)
    assert result.status is RecoveryStatus.REQUIRES_RECONCILIATION
    assert (
        RecoveryReason.FINAL_OUTPUT_DELIVERY_UNKNOWN in result.reasons
    )
    assert not result.model_replay_allowed
    assert not result.tool_replay_allowed
    assert not result.retrieval_replay_allowed


def test_delivered_with_unknown_memory_requires_manual_coordination():
    journal = InMemoryRunEventJournal()
    for event in started_step_chain(delivery="DELIVERED"):
        journal.append(event)
    result = assess(recovery_snapshot(), journal)
    assert result.status is RecoveryStatus.REQUIRES_RECONCILIATION
    assert (
        RecoveryReason.FINAL_OUTPUT_MEMORY_COMMIT_UNKNOWN
        in result.reasons
    )
    assert not result.automatic_resume_supported


def test_memory_committed_without_terminal_never_rewrites_or_resends():
    journal = InMemoryRunEventJournal()
    for event in started_step_chain(
        delivery="DELIVERED", memory="SUCCEEDED"
    ):
        journal.append(event)
    result = assess(recovery_snapshot(), journal)
    assert result.status is RecoveryStatus.REQUIRES_RECONCILIATION
    assert (
        RecoveryReason.MEMORY_COMMITTED_WITHOUT_TERMINAL
        in result.reasons
    )
    assert not result.automatic_resume_supported
    assert not result.output_reconstruction_supported


def test_terminal_delivered_memory_failed_is_terminal_not_resumed():
    journal = InMemoryRunEventJournal()
    for event in started_step_chain(
        delivery="DELIVERED",
        memory="FAILED",
        terminal="FAILED",
    ):
        journal.append(event)
    result = assess(recovery_snapshot(), journal)
    assert result.status is RecoveryStatus.TERMINAL
    assert RecoveryReason.RUN_TERMINAL in result.reasons
