import pytest

from core.runtime import FaultPoint, ToolExecutionService
from tests.tool_fault_test_support import CountingToolAdapter, make_context, make_controller


async def run(controller):
    adapter = CountingToolAdapter()
    context, _ = make_context()
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation(), adapter=adapter,
        run_context=context, step_id="step", fault_controller=controller,
    )
    snapshot = context.budget_ledger.snapshot()
    return (
        result.status, result.output.content, adapter.provider_call_count,
        snapshot.committed_usage, snapshot.active_reservation_count,
    )


@pytest.mark.asyncio
async def test_disabled_controller_matches_no_controller_semantics():
    assert await run(None) == await run(
        make_controller(FaultPoint.TOOL_BEFORE_PROVIDER_CALL, enabled=False)
    )
