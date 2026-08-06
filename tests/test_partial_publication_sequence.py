"""WP4 partial publication: journal/enqueue split and step sequence fix."""

from __future__ import annotations

import pytest

from core.runtime import (
    CoordinatedRuntimeFactory,
    FaultPoint,
    RunStatus,
    RuntimeEventType,
)
from tests._event_fault_fixtures import event_controller
from tests._wp3_fixtures import (
    Wp3RecordingRouter,
    make_wp3_services,
    shape2_planning_json,
)


def records(services, run_id: str):
    return services.event_journal.read_after(run_id, 0, 1000)


@pytest.mark.asyncio
async def test_partial_persisted_output_consumes_sequence_and_fails_unknown() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        shape2_planning_json(),
        output_for={"synthesis_agent": "SECRET_FINAL_CANDIDATE"},
    )
    controller = event_controller(
        FaultPoint.EVENT_BEFORE_CHANNEL_ENQUEUE,
        event_type=RuntimeEventType.OUTPUT_DELTA,
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        "coordinate one review",
        fault_controller=controller,
    )

    result = await scope.execute()

    assert result.status is RunStatus.FAILED
    assert result.stop_reason.value == "UNHANDLED_ERROR"
    assert result.error_code == "FINAL_OUTPUT_DELIVERY_UNKNOWN"
    step_records = records(services, scope.run_id)
    output_records = [
        item
        for item in step_records
        if item.event_type is RuntimeEventType.OUTPUT_DELTA
    ]
    assert len(output_records) == 1
    output_seq = output_records[0].step_sequence
    assert output_records[0].safe_payload["text_digest"] == (
        __import__("hashlib").sha256(
            "SECRET_FINAL_CANDIDATE".encode("utf-8")
        ).hexdigest()
    )
    synthesis_completed = [
        item
        for item in step_records
        if item.event_type is RuntimeEventType.STEP_COMPLETED
        and item.step_id == "synthesis"
    ]
    assert len(synthesis_completed) == 1
    assert synthesis_completed[0].step_sequence == output_seq + 1
    # Journal sequences are unique and monotonic per step.
    synthesis_sequences = [
        item.step_sequence
        for item in step_records
        if item.step_id == "synthesis"
        and item.step_sequence is not None
    ]
    assert synthesis_sequences == sorted(synthesis_sequences)
    assert len(synthesis_sequences) == len(set(synthesis_sequences))
    error_records = [
        item
        for item in step_records
        if item.event_type is RuntimeEventType.ERROR
    ]
    assert len(error_records) == 1
    assert (
        error_records[0].safe_payload["safe_error_code"]
        == "FINAL_OUTPUT_DELIVERY_UNKNOWN"
    )
    assert "SECRET_FINAL_CANDIDATE" not in repr(step_records)
    await scope.close()


@pytest.mark.asyncio
async def test_pre_journal_failure_does_not_consume_sequence_and_fails_known() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(shape2_planning_json())
    controller = event_controller(
        FaultPoint.EVENT_BEFORE_JOURNAL_APPEND,
        event_type=RuntimeEventType.OUTPUT_DELTA,
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        "coordinate one review",
        fault_controller=controller,
    )

    result = await scope.execute()

    assert result.status is RunStatus.FAILED
    assert result.error_code == "FINAL_OUTPUT_DELIVERY_FAILED"
    step_records = records(services, scope.run_id)
    output_records = [
        item
        for item in step_records
        if item.event_type is RuntimeEventType.OUTPUT_DELTA
    ]
    assert output_records == []
    synthesis_completed = [
        item
        for item in step_records
        if item.event_type is RuntimeEventType.STEP_COMPLETED
        and item.step_id == "synthesis"
    ]
    assert len(synthesis_completed) == 1
    # No journal append happened for OUTPUT (sequence not consumed); the
    # emitter's next free sequence is 2 (after STEP_STARTED sequence 1).
    assert synthesis_completed[0].step_sequence == 2
    assert "SECRET" not in repr(step_records)
    await scope.close()
