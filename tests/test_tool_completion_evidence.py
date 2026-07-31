from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from core.runtime import (
    OperationIdempotency,
    RunEventEmitter,
    RuntimeEventChannel,
    RuntimeEventType,
    ToolExecutionService,
)
from tests.tool_fault_test_support import PhaseAwareToolAdapter, make_context


@pytest.mark.asyncio
async def test_completion_evidence_is_the_frozen_event_fact_source():
    adapter = PhaseAwareToolAdapter(
        idempotency=OperationIdempotency.NON_IDEMPOTENT,
        idempotency_key="raw-idempotency-key",
    )
    context, _ = make_context()
    invocation = adapter.build_invocation("TOOL_OUTPUT_SECRET")
    channel = RuntimeEventChannel(
        8, run_id=context.run_id, cancellation_token=context.cancellation_token
    )
    emitter = RunEventEmitter(
        run_id=context.run_id, trace_id=context.trace_id, channel=channel
    ).for_step("step")

    result = await ToolExecutionService().execute(
        invocation=invocation,
        adapter=adapter,
        run_context=context,
        step_id="step",
        event_emitter=emitter,
    )
    await channel.close()
    events = [event async for event in channel]
    completed = next(
        event.payload
        for event in events
        if event.event_type is RuntimeEventType.TOOL_COMPLETED
    )

    assert result.completion_evidence is completed
    assert completed.provider_started is True
    assert completed.side_effect_state == "COMMITTED"
    assert completed.compensation_state == "NOT_ATTEMPTED"
    assert completed.retry_disposition == "UNSAFE"
    assert completed.result_present is True
    assert completed.result_digest == result.output.digest
    with pytest.raises(FrozenInstanceError):
        completed.side_effect_state = "UNKNOWN"
    safe = str(events[-1].to_safe_dict()) + repr(result.completion_evidence)
    for secret in (
        "TOOL_OUTPUT_SECRET",
        "raw-idempotency-key",
        "raw-resource-key",
        "tool-fault",
    ):
        assert secret not in safe
