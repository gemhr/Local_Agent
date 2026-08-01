from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict

import pytest

from core.runtime import ToolCompletionGapFixture
from core.runtime import (
    FaultPoint,
    InMemoryRunEventJournal,
    OperationIdempotency,
    RunEventEmitter,
    RuntimeEventChannel,
    RuntimeEventType,
    ToolExecutionError,
    ToolExecutionService,
)
from tests.tool_fault_test_support import (
    PhaseAwareToolAdapter,
    make_context,
    make_controller,
)


@pytest.mark.parametrize(
    "fixture",
    [
        ToolCompletionGapFixture(True, False, False, True, False, "NOT_STARTED", "SAFE", "NOT_STARTED"),
        ToolCompletionGapFixture(True, False, False, True, True, "COMMITTED", "UNSAFE", "SUCCEEDED"),
        ToolCompletionGapFixture(True, False, False, True, True, "UNKNOWN", "OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN"),
        ToolCompletionGapFixture(True, False, False, False, None, "UNKNOWN", "OUTCOME_UNKNOWN", "EVIDENCE_LOST"),
        ToolCompletionGapFixture(True, False, False, True, True, "COMMITTED", "UNSAFE", "STARTED_EVENT_CORRUPTED", started_event_valid=False),
    ],
)
def test_completion_gap_fixture_retains_only_safe_b2b_facts(fixture):
    values = asdict(fixture)
    assert set(values) == {
        "started_event_present",
        "completed_event_present",
        "run_terminal_present",
        "local_completion_evidence_present",
        "provider_started",
        "side_effect_state",
        "retry_disposition",
        "outcome_classification",
        "started_event_valid",
    }
    assert fixture.completed_event_present is False
    text = repr(fixture)
    for secret in (
        "TOOL_ARGUMENT_SECRET",
        "TOOL_OUTPUT_SECRET",
        "raw-idempotency-key",
        "raw-resource-key",
        "provider-secret-error",
    ):
        assert secret not in text
    with pytest.raises(FrozenInstanceError):
        fixture.side_effect_state = "UNKNOWN"


def test_completion_gap_fixture_rejects_completed_event_and_unsafe_lost_evidence():
    with pytest.raises(ValueError, match="cannot contain"):
        ToolCompletionGapFixture(True, True, False, True, True, "COMMITTED", "UNSAFE", "SUCCEEDED")
    with pytest.raises(ValueError, match="provider_started"):
        ToolCompletionGapFixture(True, False, False, False, True, "UNKNOWN", "OUTCOME_UNKNOWN", "EVIDENCE_LOST")
    with pytest.raises(ValueError, match="outcome_classification"):
        ToolCompletionGapFixture(
            True,
            False,
            False,
            True,
            True,
            "COMMITTED",
            "UNSAFE",
            "TOOL_OUTPUT_SECRET",
        )


@pytest.mark.asyncio
async def test_real_tool_publication_gap_builds_fixture_from_journal_and_frozen_evidence():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT
    )
    context, _ = make_context()
    journal = InMemoryRunEventJournal()
    channel = RuntimeEventChannel(
        4,
        run_id=context.run_id,
        cancellation_token=context.cancellation_token,
        journal=journal,
    )
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    ).for_step("step")
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation("TOOL_ARGUMENT_SECRET"),
        adapter=adapter,
        run_context=context,
        step_id="step",
        event_emitter=emitter,
        fault_controller=make_controller(
            FaultPoint.TOOL_BEFORE_COMPLETION_EVENT
        ),
    )
    assert isinstance(result, ToolExecutionError)
    records = journal.read_after(context.run_id, 0, 10)
    types = {record.event_type for record in records}
    evidence = result.completion_evidence
    assert evidence is not None

    fixture = ToolCompletionGapFixture(
        started_event_present=RuntimeEventType.TOOL_STARTED in types,
        completed_event_present=RuntimeEventType.TOOL_COMPLETED in types,
        run_terminal_present=RuntimeEventType.RUN_COMPLETED in types,
        local_completion_evidence_present=True,
        provider_started=bool(evidence.provider_started),
        side_effect_state=str(evidence.side_effect_state),
        retry_disposition=str(evidence.retry_disposition),
        outcome_classification=str(evidence.outcome_classification),
    )
    assert fixture == ToolCompletionGapFixture(
        True, False, False, True, True, "COMMITTED", "UNSAFE", "SUCCEEDED"
    )
    assert adapter.provider_entered_count == 1
    assert adapter.external_effect_applied_count == 1
    assert "TOOL_ARGUMENT_SECRET" not in repr(fixture)
    await channel.abort()
