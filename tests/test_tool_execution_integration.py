import json
import re
from dataclasses import replace

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
from core.runtime.tool_registry import (
    ToolDescriptor,
    ToolRegistration,
    ToolRegistry,
    ToolRegistryError,
    ToolRegistryErrorCode,
)
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
    assert (
        events[0].payload.invocation_identity_digest
        == events[2].payload.invocation_identity_digest
    )
    assert (
        events[0].payload.attempt_identity_digest
        != events[2].payload.attempt_identity_digest
    )
    assert events[0].payload.invocation_id is None
    assert events[0].payload.attempt_id is None
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


def test_registry_registers_exactly_four_production_tools_all_adapter_backed():
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    registrations = registry.registrations()
    assert tuple(
        registration.descriptor.name for registration in registrations
    ) == (
        "list_files",
        "analyze_excel",
        "get_system_status",
        "complex_workflow_simulator",
    )
    # 全部四个 Tool 都是 adapter-backed，且 Descriptor/Adapter Tool identity 一致
    for registration in registrations:
        assert registration.adapter.spec.tool_name == registration.descriptor.name


def _safe_registry_tool_name(name: str) -> str:
    """把测试支撑 adapter 的非生产安全 name 派生为 registry 安全 identity。

    仅作用于测试 helper；生产 Tool name 均符合冻结正则，不受影响。
    """
    safe = re.sub(r"[^a-z0-9_]", "_", name.lower())
    if not safe or not safe[0].isalpha():
        safe = "tool_" + safe
    return safe


def make_router_for_tool_path(
    *,
    tool_name,
    tool_args,
    adapter,
    service=None,
    legacy_function=None,
):
    """构造仅含单个 Tool 的已冻结 Registry 的 AgentRouter 测试桩。

    ``legacy_function`` 仅为兼容既存非 allowlisted 测试的旧签名保留；
    生产/测试中已不存在 legacy direct-call 路径，该参数不会被执行。
    """
    safe_name = _safe_registry_tool_name(tool_name)
    if safe_name != adapter.spec.tool_name:
        # 测试支撑 adapter（如 CountingToolAdapter）可能使用非生产安全 name；
        # 为通过 registry 冻结校验，在 helper 内派生等价安全 identity 并同步
        # 该 adapter 的 spec。不修改生产工具配置。
        adapter.spec = replace(adapter.spec, tool_name=safe_name)
    registry = ToolRegistry()
    registry.register(
        ToolRegistration(
            descriptor=ToolDescriptor(
                name=safe_name,
                description=f"test description for {safe_name}",
            ),
            adapter=adapter,
        )
    )
    registry.freeze()
    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    router.tool_execution_service = service or ToolExecutionService()
    router._build_messages = lambda **_: [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "query"},
    ]
    router._plan_tool_call = lambda _messages, _agent_id: (safe_name, tool_args)
    return router


def test_agent_router_adapter_path_executes_and_budgets_once():
    calls = {"adapter": 0}

    def migrated_function(_):
        calls["adapter"] += 1
        return "unique-runtime-observation"

    adapter = LegacyStringToolAdapter(
        tool_name="get_system_status",
        function=migrated_function,
    )
    router = make_router_for_tool_path(
        tool_name="get_system_status",
        tool_args="",
        adapter=adapter,
    )
    context, _ = create_run_context(entry_agent_id="router", timeout_seconds=2)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=1)))

    messages = router._prepare_answer_messages(
        "core_router", "query", run_context=context
    )

    assert calls == {"adapter": 1}
    usage = context.budget_ledger.snapshot().committed_usage
    assert (usage.tool_calls, usage.retries) == (1, 0)
    assert messages[0]["content"].count("unique-runtime-observation") == 1
    assert messages[0]["content"].count("已使用工具：get_system_status") == 1


def test_agent_router_adapter_error_does_not_fall_back_to_legacy_callable():
    calls = {"adapter": 0}

    def migrated_function(_):
        calls["adapter"] += 1
        return "ERROR: safe adapter failure"

    adapter = LegacyStringToolAdapter(
        tool_name="get_system_status",
        function=migrated_function,
        error_prefixes=("ERROR:",),
    )
    router = make_router_for_tool_path(
        tool_name="get_system_status",
        tool_args="",
        adapter=adapter,
    )
    context, _ = create_run_context(entry_agent_id="router", timeout_seconds=2)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=1)))

    with pytest.raises(ToolExecutionFailed):
        router._prepare_answer_messages(
            "core_router", "query", run_context=context
        )

    assert calls == {"adapter": 1}
    assert context.budget_ledger.snapshot().committed_usage.tool_calls == 1


def test_agent_router_complex_retry_uses_service_budget_and_injects_once():
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
        service=service,
    )
    context, _ = create_run_context(entry_agent_id="router", timeout_seconds=2)
    context.attach_budget_ledger(
        BudgetLedger(RunBudget(max_tool_calls=2, max_retries=1))
    )

    messages = router._prepare_answer_messages(
        "core_router", "query", run_context=context
    )

    assert len(adapter.attempts) == 2
    assert len(store.committed_operations) == 1
    usage = context.budget_ledger.snapshot().committed_usage
    assert (usage.tool_calls, usage.retries) == (2, 1)
    assert messages[0]["content"].count(
        "已使用工具：complex_workflow_simulator"
    ) == 1
    assert messages[0]["content"].count("工具观察结果：") == 1


# ---- Legacy Tool 迁移（list_files / analyze_excel）----

def test_list_files_registration_is_adapter_backed():
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    registration = registry.require("list_files")
    assert isinstance(registration.adapter, LegacyStringToolAdapter)
    assert registration.adapter.spec.tool_name == "list_files"


@pytest.mark.asyncio
async def test_list_files_migrated_through_runtime_contract_with_events(tmp_path):
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    adapter = registry.require("list_files").adapter
    context, _ = create_run_context(
        entry_agent_id="integration", timeout_seconds=2
    )
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=1)))
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

    invocation = adapter.build_invocation(str(tmp_path))
    result = await ToolExecutionService().execute(
        invocation=invocation,
        adapter=adapter,
        run_context=context,
        step_id="step",
        event_emitter=emitter,
    )
    await channel.close()
    events = [event async for event in channel]

    # success content 兼容 + ToolExecutionService 路径 + Tool event/evidence
    assert result.status == ToolExecutionStatus.SUCCEEDED
    assert result.output.content.startswith(f"Files in {tmp_path}:")
    assert [event.event_type for event in events] == [
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_COMPLETED,
    ]
    assert events[1].payload.succeeded is True
    assert events[1].payload.result_digest == result.output.digest


@pytest.mark.asyncio
async def test_list_files_legacy_error_prefix_becomes_safe_tool_error():
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    adapter = registry.require("list_files").adapter
    invocation = adapter.build_invocation("C:/definitely/not/exist-xyz")
    result = await ToolExecutionService().execute(
        invocation=invocation,
        adapter=adapter,
        run_context=make_context(),
        step_id="step",
    )
    assert isinstance(result, ToolExecutionError)
    assert result.safe_error_code == "LEGACY_TOOL_REPORTED_ERROR"
    assert result.status == ToolExecutionStatus.FAILED


@pytest.mark.asyncio
async def test_analyze_excel_migrated_success_is_compatible(tmp_path):
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    adapter = registry.require("analyze_excel").adapter
    invocation = adapter.build_invocation(str(csv_path))
    result = await ToolExecutionService().execute(
        invocation=invocation,
        adapter=adapter,
        run_context=make_context(),
        step_id="step",
    )
    assert result.status == ToolExecutionStatus.SUCCEEDED
    assert "Analysis for sample.csv" in result.output.content


@pytest.mark.asyncio
async def test_analyze_excel_legacy_error_prefix_becomes_safe_tool_error():
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    adapter = registry.require("analyze_excel").adapter
    invocation = adapter.build_invocation("C:/definitely/not/exist-xyz.csv")
    result = await ToolExecutionService().execute(
        invocation=invocation,
        adapter=adapter,
        run_context=make_context(),
        step_id="step",
    )
    assert isinstance(result, ToolExecutionError)
    assert result.safe_error_code == "LEGACY_TOOL_REPORTED_ERROR"


# ---- AgentRouter / ToolRegistry 兼容视图 ----

def test_agent_router_receives_frozen_registry():
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    assert router.tool_registry is registry
    assert router.tool_registry.frozen is True


def test_router_tools_is_read_only_compatibility_view():
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    view = router.tools
    assert set(view) == {
        "list_files",
        "analyze_excel",
        "get_system_status",
        "complex_workflow_simulator",
    }
    assert view["list_files"]["description"].startswith(
        "List files in a local directory"
    )
    assert isinstance(view["list_files"]["adapter"], LegacyStringToolAdapter)
    # attempted mutation 不改变 canonical registry
    with pytest.raises(TypeError):
        view["list_files"] = {"description": "mutated", "adapter": None}
    with pytest.raises(TypeError):
        view["list_files"]["description"] = "mutated"
    assert registry.require("list_files").descriptor.description.startswith(
        "List files in a local directory"
    )


def test_planner_prompt_derives_description_from_descriptor():
    from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY

    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    router.agents_config = DEFAULT_AGENT_REGISTRY.legacy_display_config()
    prompt = router._build_tool_planner_prompt("core_router")
    assert "- list_files: List files in a local directory." in prompt
    assert "- complex_workflow_simulator: Run a deterministic" in prompt
    assert prompt.count("可用工具：") == 1


def test_unknown_planner_tool_does_not_execute():
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    assert router._parse_tool_call("CALL: not_a_tool()") is None
    assert router._parse_tool_call("CALL: list_files(some-dir)") == (
        "list_files",
        "some-dir",
    )


def test_internal_required_lookup_fails_closed():
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    with pytest.raises(ToolRegistryError) as captured:
        registry.require("not_a_tool")
    assert captured.value.error_code is ToolRegistryErrorCode.NOT_REGISTERED


def test_tool_intent_exact_name_derives_from_registry():
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    # exact canonical name（来自 Registry，不再硬编码）
    assert router._tool_intent_likely(
        "please run complex_workflow_simulator now"
    ) is True
    # 通用兼容关键词仍保留
    assert router._tool_intent_likely("list the files in a folder") is True
    assert router._tool_intent_likely("hello") is False
