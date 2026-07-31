from __future__ import annotations

import pytest

from core.runtime import (
    FaultPoint,
    OperationIdempotency,
    RunEventEmitter,
    RuntimeEventChannel,
    ToolExecutionError,
    ToolExecutionService,
    safe_key_digest,
)
from tests.tool_fault_test_support import (
    PhaseAwareToolAdapter,
    make_context,
    make_controller,
)


@pytest.mark.asyncio
async def test_completion_publication_fault_is_scoped_to_one_invocation():
    service = ToolExecutionService()
    first = PhaseAwareToolAdapter(idempotency=OperationIdempotency.NON_IDEMPOTENT)
    second = PhaseAwareToolAdapter(idempotency=OperationIdempotency.NON_IDEMPOTENT)
    first_context, _ = make_context()
    second_context, _ = make_context()
    first_invocation = first.build_invocation()
    second_invocation = second.build_invocation()
    first_channel = RuntimeEventChannel(
        8,
        run_id=first_context.run_id,
        cancellation_token=first_context.cancellation_token,
    )
    second_channel = RuntimeEventChannel(
        8,
        run_id=second_context.run_id,
        cancellation_token=second_context.cancellation_token,
    )
    first_emitter = RunEventEmitter(
        run_id=first_context.run_id,
        trace_id=first_context.trace_id,
        channel=first_channel,
    ).for_step("same-step")
    second_emitter = RunEventEmitter(
        run_id=second_context.run_id,
        trace_id=second_context.trace_id,
        channel=second_channel,
    ).for_step("same-step")
    controller = make_controller(
        FaultPoint.TOOL_BEFORE_COMPLETION_EVENT,
        invocation_id_digest=safe_key_digest(first_invocation.invocation_id),
    )

    failed = await service.execute(
        invocation=first_invocation,
        adapter=first,
        run_context=first_context,
        step_id="same-step",
        event_emitter=first_emitter,
        fault_controller=controller,
    )
    succeeded = await service.execute(
        invocation=second_invocation,
        adapter=second,
        run_context=second_context,
        step_id="same-step",
        event_emitter=second_emitter,
        fault_controller=controller,
    )
    await first_channel.close()
    await second_channel.close()

    assert isinstance(failed, ToolExecutionError)
    assert failed.safe_error_code == "TOOL_COMPLETION_PUBLICATION_FAILED"
    assert failed.completion_evidence is not None
    assert succeeded.output.content == "ok"
    assert first.external_effect_applied_count == 1
    assert second.external_effect_applied_count == 1
    controller.close()
    assert failed.completion_evidence.side_effect_state == "COMMITTED"
