import json

import pytest

from core.runtime import (
    BudgetLedger,
    ComplexWorkflowToolAdapter,
    LegacyStringToolAdapter,
    RetryDisposition,
    RetryPolicy,
    RunEventEmitter,
    RunBudget,
    RuntimeEventChannel,
    RuntimeEventType,
    ToolExecutionError,
    ToolExecutionFailed,
    ToolExecutionService,
    ToolExecutionStatus,
    ToolSideEffectState,
    create_run_context,
)
from core.runtime.retry import RetryExecutor
from core.agent_router import AgentRouter
from tools.complex_workflow_simulator import InMemoryWorkflowStateStore
from tools.registry import register_all_tools


def make_context():
    context, _ = create_run_context(
        entry_agent_id="integration", timeout_seconds=2
    )
    context.attach_budget_ledger(
        BudgetLedger(RunBudget(max_tool_calls=4, max_retries=2))
    )
    return context


def complex_payload(**changes):
    payload = {
        "operation_id": "operation-1",
        "resource_key": "resource-1",
        "idempotency_key": None,
        "execution_mode": "DRY_RUN",
        "items": [{"item_id": "item-1", "action": "ADD", "quantity": 1}],
    }
    payload.update(changes)
    return json.dumps(payload)


class RecordingComplexWorkflowToolAdapter(ComplexWorkflowToolAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.attempts = []

    def invoke_once(self, invocation, context):
        self.attempts.append(
            {
                "invocation_id": invocation.invocation_id,
                "idempotency_key": invocation.idempotency_key,
                "arguments_digest": invocation.arguments_digest,
                "attempt_id": context.attempt_id,
                "retry_index": context.retry_index,
            }
        )
        return super().invoke_once(invocation, context)


@pytest.mark.asyncio
async def test_complex_dry_run_and_idempotent_replay_use_contract():
    adapter = ComplexWorkflowToolAdapter(sleeper=lambda _: None)
    service = ToolExecutionService()
    dry = adapter.build_invocation(complex_payload())
    result = await service.execute(
        invocation=dry,
        adapter=adapter,
        run_context=make_context(),
        step_id="step",
    )
    assert result.status == ToolExecutionStatus.SUCCEEDED
    assert not result.idempotency_replayed

    text = complex_payload(
        operation_id="operation-2",
        execution_mode="IDEMPOTENT_COMMIT",
        idempotency_key="stable-key",
    )
    first_invocation = adapter.build_invocation(text)
    first = await service.execute(
        invocation=first_invocation,
        adapter=adapter,
        run_context=make_context(),
        step_id="step",
    )
    replay_invocation = adapter.build_invocation(text)
    replay = await service.execute(
        invocation=replay_invocation,
        adapter=adapter,
        run_context=make_context(),
        step_id="step",
    )
    assert first.status == ToolExecutionStatus.SUCCEEDED
    assert replay.idempotency_replayed


@pytest.mark.asyncio
async def test_complex_non_idempotent_transient_does_not_retry():
    adapter = ComplexWorkflowToolAdapter(sleeper=lambda _: None)
    invocation = adapter.build_invocation(
        complex_payload(
            execution_mode="NON_IDEMPOTENT_SIMULATION",
            failure_injection="TRANSIENT_BEFORE_SIDE_EFFECT",
        )
    )
    context = make_context()
    result = await ToolExecutionService().execute(
        invocation=invocation,
        adapter=adapter,
        run_context=context,
        step_id="step",
    )
    assert isinstance(result, ToolExecutionError)
    assert context.budget_ledger.snapshot().committed_usage.tool_calls == 1


@pytest.mark.asyncio
async def test_committed_idempotent_failure_replays_same_invocation_once():
    store = InMemoryWorkflowStateStore()
    adapter = RecordingComplexWorkflowToolAdapter(
        state_store=store, sleeper=lambda _: None
    )
    invocation = adapter.build_invocation(
        complex_payload(
            execution_mode="IDEMPOTENT_COMMIT",
            idempotency_key="stable-post-commit-key",
            failure_injection="FAIL_AFTER_SIDE_EFFECT",
            processing_options={"enable_compensation": False},
        )
    )
    context, _ = create_run_context(
        entry_agent_id="integration", timeout_seconds=2
    )
    context.attach_budget_ledger(
        BudgetLedger(RunBudget(max_tool_calls=2, max_retries=1))
    )
    channel = RuntimeEventChannel(
        8,
        run_id=context.run_id,
        cancellation_token=context.cancellation_token,
    )
    emitter = RunEventEmitter(
        run_id=context.run_id,
        trace_id=context.trace_id,
        channel=channel,
    ).for_step("step")
    service = ToolExecutionService(
        retry_executor=RetryExecutor(
            RetryPolicy(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
            )
        )
    )

    result = await service.execute(
        invocation=invocation,
        adapter=adapter,
        run_context=context,
        step_id="step",
        event_emitter=emitter,
    )
    await channel.close()
    events = [event async for event in channel]

    assert result.status == ToolExecutionStatus.SUCCEEDED
    assert result.idempotency_replayed
    assert result.retry_index == 1
    assert len(adapter.attempts) == 2
    assert {attempt["invocation_id"] for attempt in adapter.attempts} == {
        invocation.invocation_id
    }
    assert {attempt["idempotency_key"] for attempt in adapter.attempts} == {
        "stable-post-commit-key"
    }
    assert {attempt["arguments_digest"] for attempt in adapter.attempts} == {
        invocation.arguments_digest
    }
    assert [attempt["retry_index"] for attempt in adapter.attempts] == [0, 1]
    assert adapter.attempts[0]["attempt_id"] != adapter.attempts[1]["attempt_id"]
    assert len(store.committed_operations) == 1
    usage = context.budget_ledger.snapshot().committed_usage
    assert (usage.tool_calls, usage.retries) == (2, 1)
    assert [event.event_type for event in events] == [
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
    ]
    first_completed = events[1].payload
    assert first_completed.side_effect_state == ToolSideEffectState.COMMITTED.value
    assert (
        first_completed.retry_disposition
        == RetryDisposition.SAFE_WITH_IDEMPOTENCY_KEY.value
    )
    assert events[0].payload.invocation_id == events[2].payload.invocation_id
    assert events[0].payload.attempt_id != events[2].payload.attempt_id
    assert events[0].payload.retry_index == 0
    assert events[2].payload.retry_index == 1


@pytest.mark.asyncio
async def test_non_idempotent_post_commit_failure_does_not_retry():
    store = InMemoryWorkflowStateStore()
    adapter = RecordingComplexWorkflowToolAdapter(
        state_store=store, sleeper=lambda _: None
    )
    invocation = adapter.build_invocation(
        complex_payload(
            execution_mode="NON_IDEMPOTENT_SIMULATION",
            failure_injection="FAIL_AFTER_SIDE_EFFECT",
            processing_options={"enable_compensation": False},
        )
    )
    context = make_context()
    result = await ToolExecutionService().execute(
        invocation=invocation,
        adapter=adapter,
        run_context=context,
        step_id="step",
    )
    assert isinstance(result, ToolExecutionError)
    assert result.side_effect_state == ToolSideEffectState.COMMITTED
    assert result.retry_disposition == RetryDisposition.UNSAFE
    assert len(adapter.attempts) == 1
    assert len(store.committed_operations) == 1
    usage = context.budget_ledger.snapshot().committed_usage
    assert (usage.tool_calls, usage.retries) == (1, 0)


@pytest.mark.asyncio
async def test_read_only_legacy_adapter_validates_error_string_and_output_limit():
    adapter = LegacyStringToolAdapter(
        tool_name="read",
        function=lambda _: "ERROR: hidden legacy failure",
        max_output_bytes=5,
        error_prefixes=("ERROR:",),
    )
    result = await ToolExecutionService().execute(
        invocation=adapter.build_invocation("safe"),
        adapter=adapter,
        run_context=make_context(),
        step_id="step",
    )
    assert isinstance(result, ToolExecutionError)
    assert result.safe_error_code == "LEGACY_TOOL_REPORTED_ERROR"


def test_registry_migrates_only_two_tools_without_breaking_legacy_router():
    class Router:
        def __init__(self):
            self.tools = {}
            self.adapters = {}

        def register_tool(self, name, func, description):
            self.tools[name] = func

        def attach_tool_adapter(self, name, adapter):
            self.adapters[name] = adapter

    router = Router()
    register_all_tools(router)
    assert set(router.adapters) == {
        "complex_workflow_simulator",
        "get_system_status",
    }
    assert set(router.tools) == {
        "list_files",
        "analyze_excel",
        "get_system_status",
        "complex_workflow_simulator",
    }


def make_router_for_tool_path(
    *,
    tool_name,
    tool_args,
    adapter,
    legacy_function,
    service=None,
):
    router = AgentRouter.__new__(AgentRouter)
    router.tools = {
        tool_name: {
            "func": legacy_function,
            "description": "test",
            "adapter": adapter,
        }
    }
    router.tool_execution_service = service or ToolExecutionService()
    router._build_messages = lambda **_: [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "query"},
    ]
    router._plan_tool_call = lambda _messages, _agent_id: (tool_name, tool_args)
    return router


def test_agent_router_migrated_read_only_path_executes_and_budgets_once():
    calls = {"adapter": 0, "legacy": 0}

    def migrated_function(_):
        calls["adapter"] += 1
        return "unique-runtime-observation"

    def legacy_function(_):
        calls["legacy"] += 1
        return "must-not-run"

    adapter = LegacyStringToolAdapter(
        tool_name="get_system_status",
        function=migrated_function,
    )
    router = make_router_for_tool_path(
        tool_name="get_system_status",
        tool_args="",
        adapter=adapter,
        legacy_function=legacy_function,
    )
    context, _ = create_run_context(entry_agent_id="router", timeout_seconds=2)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=1)))

    messages = router._prepare_answer_messages(
        "core_router", "query", run_context=context
    )

    assert calls == {"adapter": 1, "legacy": 0}
    usage = context.budget_ledger.snapshot().committed_usage
    assert (usage.tool_calls, usage.retries) == (1, 0)
    assert messages[0]["content"].count("unique-runtime-observation") == 1
    assert messages[0]["content"].count("已使用工具：get_system_status") == 1


def test_agent_router_migrated_error_does_not_fall_back_to_legacy_string():
    calls = {"adapter": 0, "legacy": 0}

    def migrated_function(_):
        calls["adapter"] += 1
        return "ERROR: safe adapter failure"

    def legacy_function(_):
        calls["legacy"] += 1
        return "legacy success"

    adapter = LegacyStringToolAdapter(
        tool_name="get_system_status",
        function=migrated_function,
        error_prefixes=("ERROR:",),
    )
    router = make_router_for_tool_path(
        tool_name="get_system_status",
        tool_args="",
        adapter=adapter,
        legacy_function=legacy_function,
    )
    context, _ = create_run_context(entry_agent_id="router", timeout_seconds=2)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=1)))

    with pytest.raises(ToolExecutionFailed):
        router._prepare_answer_messages(
            "core_router", "query", run_context=context
        )

    assert calls == {"adapter": 1, "legacy": 0}
    assert context.budget_ledger.snapshot().committed_usage.tool_calls == 1


def test_agent_router_complex_retry_uses_service_budget_and_injects_once():
    legacy_calls = []
    store = InMemoryWorkflowStateStore()
    adapter = RecordingComplexWorkflowToolAdapter(
        state_store=store, sleeper=lambda _: None
    )
    tool_args = complex_payload(
        execution_mode="IDEMPOTENT_COMMIT",
        idempotency_key="router-stable-key",
        failure_injection="FAIL_AFTER_SIDE_EFFECT",
        processing_options={"enable_compensation": False},
    )
    service = ToolExecutionService(
        retry_executor=RetryExecutor(
            RetryPolicy(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
            )
        )
    )
    router = make_router_for_tool_path(
        tool_name="complex_workflow_simulator",
        tool_args=tool_args,
        adapter=adapter,
        legacy_function=lambda value: legacy_calls.append(value),
        service=service,
    )
    context, _ = create_run_context(entry_agent_id="router", timeout_seconds=2)
    context.attach_budget_ledger(
        BudgetLedger(RunBudget(max_tool_calls=2, max_retries=1))
    )

    messages = router._prepare_answer_messages(
        "core_router", "query", run_context=context
    )

    assert legacy_calls == []
    assert len(adapter.attempts) == 2
    assert len(store.committed_operations) == 1
    usage = context.budget_ledger.snapshot().committed_usage
    assert (usage.tool_calls, usage.retries) == (2, 1)
    assert messages[0]["content"].count(
        "已使用工具：complex_workflow_simulator"
    ) == 1
    assert messages[0]["content"].count("工具观察结果：") == 1


def test_agent_router_unmigrated_tool_stays_on_single_legacy_path():
    legacy_calls = []
    router = make_router_for_tool_path(
        tool_name="list_files",
        tool_args="safe",
        adapter=None,
        legacy_function=lambda value: (
            legacy_calls.append(value) or "unique-legacy-observation"
        ),
    )
    context, _ = create_run_context(entry_agent_id="router", timeout_seconds=2)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=1)))

    messages = router._prepare_answer_messages(
        "core_router", "query", run_context=context
    )

    assert legacy_calls == ["safe"]
    assert context.budget_ledger.snapshot().committed_usage.tool_calls == 1
    assert messages[0]["content"].count("unique-legacy-observation") == 1
