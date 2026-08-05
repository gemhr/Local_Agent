from __future__ import annotations

import asyncio

import pytest

from core.runtime.blocking_executor import BoundedBlockingExecutor
from core.runtime.context import RunContext
from core.runtime.multi_agent_planning import PlanningRequest
from core.runtime.planning_model_adapter import UnifiedPlanningModelAdapter


def test_agent_router_planner_uses_unified_model_contract_and_strict_prompt() -> None:
    from types import SimpleNamespace

    from core.agent_router import AgentRouter
    from core.runtime import BudgetLedger, RunBudget

    captured = {}
    router = object.__new__(AgentRouter)
    router.max_tokens = 640

    def invoke(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(output="{}")

    router._invoke_model_contract = invoke
    context = RunContext.create(entry_agent_id="core_router")
    context.attach_budget_ledger(BudgetLedger(RunBudget()))
    emitter = object()

    assert router.complete_planning_decision(
        "private request", run_context=context, event_emitter=emitter
    ) == "{}"
    assert captured["run_context"] is context
    assert captured["event_emitter"] is emitter
    assert captured["generation_options"] == {"enable_thinking": False}
    system_prompt = captured["messages"][0]["content"]
    assert "DIRECT_ANSWER" in system_prompt
    assert "DELEGATE" in system_prompt
    assert "不得包含 instruction" in system_prompt
    assert "output_policy" in system_prompt
    assert captured["messages"][1]["content"] == "private request"


@pytest.mark.asyncio
async def test_adapter_calls_only_the_unified_router_entry_with_run_context() -> None:
    class Router:
        def __init__(self) -> None:
            self.calls = []

        def complete_planning_decision(self, user_request: str, **kwargs) -> str:
            self.calls.append((user_request, kwargs))
            return '{"schema_version":1,"decision":"DIRECT_ANSWER","agent_id":"core_router","reason_code":"MODEL_DIRECT"}'

    router = Router()
    executor = BoundedBlockingExecutor(max_workers=1, max_pending_tasks=1)
    context = RunContext.create(entry_agent_id="core_router")
    try:
        result = await UnifiedPlanningModelAdapter(
            router, blocking_executor=executor
        ).generate_plan(PlanningRequest("core_router", "private query"), context)
    finally:
        executor.shutdown(wait=True, timeout=1)

    assert '"DIRECT_ANSWER"' in result
    assert len(router.calls) == 1
    assert router.calls[0][0] == "private query"
    assert router.calls[0][1]["run_context"] is context


@pytest.mark.asyncio
async def test_adapter_propagates_task_cancellation_without_content_error() -> None:
    import threading

    release = threading.Event()

    class Router:
        def complete_planning_decision(self, user_request: str, **kwargs) -> str:
            release.wait(1)
            return "{}"

    executor = BoundedBlockingExecutor(max_workers=1, max_pending_tasks=1)
    context = RunContext.create(entry_agent_id="core_router")
    task = asyncio.create_task(
        UnifiedPlanningModelAdapter(
            Router(), blocking_executor=executor
        ).generate_plan(PlanningRequest("core_router", "secret"), context)
    )
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        executor.shutdown(wait=True, timeout=1)
