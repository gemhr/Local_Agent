import pytest

from core.runtime import (
    FaultPoint, ToolExecutionError, ToolExecutionFailed, ToolExecutionService,
    safe_key_digest,
)
from tests.tool_fault_test_support import CountingToolAdapter, make_context, make_controller
from tests.test_tool_execution_integration import make_router_for_tool_path


@pytest.mark.asyncio
async def test_controller_is_request_scoped_and_invocation_matching_is_isolated():
    service = ToolExecutionService()
    adapter = CountingToolAdapter()
    context_a, _ = make_context()
    context_b, _ = make_context()
    invocation_a = adapter.build_invocation()
    invocation_b = adapter.build_invocation()
    controller = make_controller(
        FaultPoint.TOOL_BEFORE_INVOCATION,
        invocation_id_digest=safe_key_digest(invocation_a.invocation_id),
    )
    failed = await service.execute(
        invocation=invocation_a, adapter=adapter, run_context=context_a,
        step_id="step", fault_controller=controller,
    )
    succeeded = await service.execute(
        invocation=invocation_b, adapter=adapter, run_context=context_b,
        step_id="step", fault_controller=None,
    )
    assert isinstance(failed, ToolExecutionError)
    assert succeeded.output.content == "ok"
    assert adapter.provider_call_count == 1
    assert not hasattr(service, "fault_controller")


def test_agent_router_transports_request_controller_to_real_tool_service_path():
    adapter = CountingToolAdapter()
    legacy_calls = []
    router = make_router_for_tool_path(
        tool_name=adapter.spec.tool_name,
        tool_args="TOOL_ARGUMENT_SECRET",
        adapter=adapter,
        legacy_function=lambda value: legacy_calls.append(value),
    )
    context, _ = make_context()
    with pytest.raises(ToolExecutionFailed) as raised:
        router._prepare_answer_messages(
            "core_router", "query", run_context=context,
            fault_controller=make_controller(FaultPoint.TOOL_BEFORE_PROVIDER_CALL),
        )
    assert raised.value.error.provider_started is False
    assert adapter.provider_call_count == 0
    assert legacy_calls == []
