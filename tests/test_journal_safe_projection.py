from __future__ import annotations

import hashlib

import pytest

from core.runtime.event_journal import JournalRecord
from core.runtime.event_journal_store import InMemoryRunEventJournal
from core.runtime.events import (
    ErrorPayload,
    OutputDeltaPayload,
    PlanCreatedPayload,
    PlanningStartedPayload,
    RetrievalStartedPayload,
    RunCompletedPayload,
    RunStartedPayload,
    RuntimeEventType,
    StepCompletedPayload,
    StepStartedPayload,
)
from core.runtime.journal_tail_reducer import (
    JournalTailValidationError,
    JournalTailValidator,
    LimitedJournalTailReducer,
)
from core.runtime.recovery_contract import RecoveryReason, RecoveryStatus
from tests._recovery_fixtures import recovery_snapshot, runtime_event


def test_output_delta_journal_keeps_only_digest_and_length():
    journal = InMemoryRunEventJournal()
    text = "SECRET_FINAL_OUTPUT\nline2"
    journal.append(
        runtime_event(
            1,
            RuntimeEventType.OUTPUT_DELTA,
            OutputDeltaPayload(text),
        )
    )
    record = journal.read_after("run", 0, 10)[0]
    assert record.safe_payload == {
        "text_length": len(text),
        "text_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    assert "SECRET_FINAL_OUTPUT" not in repr(record)
    assert "SECRET_FINAL_OUTPUT" not in str(record.safe_payload)


def test_planning_and_step_events_keep_safe_allowlist_only():
    journal = InMemoryRunEventJournal()
    journal.append(
        runtime_event(
            1,
            RuntimeEventType.PLANNING_STARTED,
            PlanningStartedPayload(1, 15000),
        )
    )
    journal.append(
        runtime_event(
            2,
            RuntimeEventType.PLAN_CREATED,
            PlanCreatedPayload("plan", 1, "a" * 64, 2, "MODEL", shape="2"),
        )
    )
    journal.append(
        runtime_event(
            3,
            RuntimeEventType.STEP_STARTED,
            StepStartedPayload(
                "RUNNING",
                agent_id="code_expert",
                execution_kind="AGENT",
                output_policy="INTERNAL",
                dependency_count=0,
            ),
            step_id="code",
            step_sequence=1,
        )
    )
    journal.append(
        runtime_event(
            4,
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload(
                "SUCCEEDED",
                duration_ms=10,
                result_char_count=15,
                delivery_status="DELIVERED",
                delivery_duration_ms=2,
            ),
            step_id="code",
            step_sequence=1,
        )
    )
    records = journal.read_after("run", 0, 10)
    by_type = {record.event_type: record for record in records}
    assert by_type[RuntimeEventType.PLAN_CREATED].safe_payload["shape"] == "2"
    step_started = by_type[RuntimeEventType.STEP_STARTED].safe_payload
    assert step_started["agent_id"] == "code_expert"
    assert step_started["execution_kind"] == "AGENT"
    assert step_started["output_policy"] == "INTERNAL"
    assert "instruction" not in step_started
    step_completed = by_type[RuntimeEventType.STEP_COMPLETED].safe_payload
    assert step_completed["result_char_count"] == 15
    assert step_completed["delivery_status"] == "DELIVERED"
    assert "content" not in step_completed
    assert "result" not in step_completed


def test_terminal_events_keep_layered_facts_without_raw_text():
    journal = InMemoryRunEventJournal()
    journal.append(
        runtime_event(
            1,
            RuntimeEventType.ERROR,
            ErrorPayload(
                "FINAL_OUTPUT_MEMORY_COMMIT_FAILED",
                "safe",
                "step_completion",
                True,
                delivery_status="DELIVERED",
                final_step_status="SUCCEEDED",
                memory_commit_status="FAILED",
            ),
        )
    )
    journal.append(
        runtime_event(
            2,
            RuntimeEventType.RUN_COMPLETED,
            RunCompletedPayload(
                "FAILED",
                "UNHANDLED_ERROR",
                duration_ms=50,
                safe_error_code="FINAL_OUTPUT_MEMORY_COMMIT_FAILED",
                delivery_status="DELIVERED",
                final_step_status="SUCCEEDED",
                memory_commit_status="FAILED",
                shape="2",
            ),
        )
    )
    records = journal.read_after("run", 0, 10)
    terminal = records[-1]
    assert terminal.safe_payload["delivery_status"] == "DELIVERED"
    assert terminal.safe_payload["memory_commit_status"] == "FAILED"
    assert terminal.safe_payload["shape"] == "2"
    assert "exception" not in terminal.safe_payload
    assert "path" not in str(terminal.safe_payload)


def test_validator_rejects_duplicate_and_out_of_order_sequences():
    first = JournalRecord.from_event(runtime_event(
        1,
        RuntimeEventType.RUN_STARTED,
        RunStartedPayload("RUNNING"),
    ))
    with pytest.raises(JournalTailValidationError) as exc:
        JournalTailValidator.validate(
            run_id="run",
            snapshot_sequence=0,
            records=(first, first),
        )
    assert exc.value.status is RecoveryStatus.JOURNAL_GAP_OR_CONFLICT

    # sequence 倒退同样拒绝。
    with pytest.raises(JournalTailValidationError):
        JournalTailValidator.validate(
            run_id="run",
            snapshot_sequence=0,
            records=(
                first,
                JournalRecord.from_event(runtime_event(
                    1,
                    RuntimeEventType.RUN_STARTED,
                    RunStartedPayload("RUNNING"),
                )),
                JournalRecord.from_event(runtime_event(
                    2,
                    RuntimeEventType.RUN_STARTED,
                    RunStartedPayload("RUNNING"),
                )),
                JournalRecord.from_event(runtime_event(
                    2,
                    RuntimeEventType.RUN_STARTED,
                    RunStartedPayload("RUNNING"),
                )),
            ),
        )


def test_validator_rejects_events_after_terminal_fail_closed():
    with pytest.raises(JournalTailValidationError) as exc:
        JournalTailValidator.validate(
            run_id="run",
            snapshot_sequence=0,
            records=(
                JournalRecord.from_event(runtime_event(
                    1,
                    RuntimeEventType.RUN_COMPLETED,
                    RunCompletedPayload("SUCCEEDED", "COMPLETED"),
                )),
                JournalRecord.from_event(runtime_event(
                    2,
                    RuntimeEventType.OUTPUT_DELTA,
                    OutputDeltaPayload("late"),
                )),
            ),
        )
    assert exc.value.reason is RecoveryReason.JOURNAL_TERMINAL_CONFLICT


def test_reducer_is_idempotent_and_projects_full_chain():
    journal = InMemoryRunEventJournal()
    journal.append(
        runtime_event(
            1,
            RuntimeEventType.RUN_STARTED,
            RunStartedPayload("RUNNING"),
        )
    )
    journal.append(
        runtime_event(
            2,
            RuntimeEventType.PLANNING_STARTED,
            PlanningStartedPayload(1, 15000),
        )
    )
    journal.append(
        runtime_event(
            3,
            RuntimeEventType.PLAN_CREATED,
            PlanCreatedPayload("p", 1, "a" * 64, 1, "MODEL", shape="1"),
        )
    )
    journal.append(
        runtime_event(
            4,
            RuntimeEventType.STEP_STARTED,
            StepStartedPayload(
                "RUNNING",
                agent_id="knowledge_expert",
                execution_kind="AGENT",
                output_policy="FINAL_PASSTHROUGH",
                dependency_count=0,
            ),
            step_id="step",
            step_sequence=1,
        )
    )
    journal.append(
        runtime_event(
            5,
            RuntimeEventType.OUTPUT_DELTA,
            OutputDeltaPayload("final"),
        )
    )
    journal.append(
        runtime_event(
            6,
            RuntimeEventType.STEP_COMPLETED,
            StepCompletedPayload(
                "SUCCEEDED",
                duration_ms=10,
                result_char_count=5,
                delivery_status="DELIVERED",
                delivery_duration_ms=1,
            ),
            step_id="step",
            step_sequence=1,
        )
    )
    journal.append(
        runtime_event(
            7,
            RuntimeEventType.ERROR,
            ErrorPayload(
                "FINAL_OUTPUT_MEMORY_COMMIT_FAILED",
                "safe",
                "step_completion",
                True,
                delivery_status="DELIVERED",
                final_step_status="SUCCEEDED",
                memory_commit_status="FAILED",
            ),
        )
    )
    journal.append(
        runtime_event(
            8,
            RuntimeEventType.RUN_COMPLETED,
            RunCompletedPayload(
                "FAILED",
                "UNHANDLED_ERROR",
                duration_ms=50,
                safe_error_code="FINAL_OUTPUT_MEMORY_COMMIT_FAILED",
                delivery_status="DELIVERED",
                final_step_status="SUCCEEDED",
                memory_commit_status="FAILED",
                shape="1",
            ),
        )
    )
    records = journal.read_after("run", 0, 20)
    snapshot = recovery_snapshot()
    first = LimitedJournalTailReducer.reduce(snapshot, records)
    second = LimitedJournalTailReducer.reduce(snapshot, records)
    assert first.projection == second.projection
    projection = first.projection
    assert projection.planning_started
    assert projection.plan_created
    assert projection.plan_shape == "1"
    assert projection.output_available
    assert projection.output_publication_attempted
    assert projection.delivery_status == "DELIVERED"
    assert projection.final_step_status == "SUCCEEDED"
    assert projection.memory_commit_status == "FAILED"
    assert projection.run_status == "FAILED"


def test_reducer_projects_partial_persisted_delivery_unknown():
    journal = InMemoryRunEventJournal()
    journal.append(
        runtime_event(
            1,
            RuntimeEventType.RUN_STARTED,
            RunStartedPayload("RUNNING"),
        )
    )
    journal.append(
        runtime_event(
            2,
            RuntimeEventType.OUTPUT_DELTA,
            OutputDeltaPayload("maybe"),
        )
    )
    journal.append(
        runtime_event(
            3,
            RuntimeEventType.ERROR,
            ErrorPayload(
                "FINAL_OUTPUT_DELIVERY_UNKNOWN",
                "safe",
                "output_gate",
                True,
                delivery_status="OUTCOME_UNKNOWN",
                final_step_status="SUCCEEDED",
            ),
        )
    )
    reduction = LimitedJournalTailReducer.reduce(
        recovery_snapshot(),
        journal.read_after("run", 0, 20),
    )
    assert reduction.projection.delivery_status == "OUTCOME_UNKNOWN"
    assert reduction.projection.output_publication_attempted


def test_reducer_ignores_observation_events_without_state_change():
    reduction = LimitedJournalTailReducer.reduce(
        recovery_snapshot(),
        (
            JournalRecord.from_event(runtime_event(
                1,
                RuntimeEventType.RETRIEVAL_STARTED,
                RetrievalStartedPayload("rid", "a" * 64, 1, 3),
            )),
        ),
    )
    assert reduction.projection.delivery_status is None
    assert reduction.projection.output_available is False
    assert reduction.projection.output_publication_attempted is False
    assert (
        RecoveryReason.RETRIEVAL_OUTCOME_UNKNOWN
        in reduction.reconciliation_reasons
    )
