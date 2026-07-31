import pytest

from core.runtime import FaultPoint, RuntimeActivityTracker, ToolExecutionService
from tests.tool_fault_test_support import CountingToolAdapter, make_context, make_controller


@pytest.mark.asyncio
async def test_provider_pre_call_fault_releases_lease_permit_and_budget_reservation():
    adapter = CountingToolAdapter(resource_key="RESOURCE_SECRET")
    context, _ = make_context()
    tracker = RuntimeActivityTracker(context.run_id)
    context.attach_activity_tracker(tracker)
    service = ToolExecutionService()
    result = await service.execute(
        invocation=adapter.build_invocation(), adapter=adapter,
        run_context=context, step_id="step",
        fault_controller=make_controller(FaultPoint.TOOL_BEFORE_PROVIDER_CALL),
    )
    assert result.provider_started is False
    assert not service.concurrency_controller.is_resource_held("RESOURCE_SECRET")
    assert context.budget_ledger.snapshot().active_reservation_count == 0
    assert service.concurrency_controller.worker_snapshot()["active_worker_count"] == 0
    counts, unknown, _ = tracker.counts()
    assert counts["tool_attempts_active"] == 0
    assert counts["detached_tool_workers"] == 0
    assert unknown is False
