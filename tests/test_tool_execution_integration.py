import json
import re
from dataclasses import replace
from types import SimpleNamespace

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
    NativeToolCall,
    TaskCapabilityRequirements,
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
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.tool_governance import (
    PRODUCTION_AGENT_IDS,
    ToolGovernanceContext,
    ToolGovernanceOutcome,
    ToolGovernanceService,
    ToolPolicy,
    ToolPolicyCatalog,
    ToolRiskLevel,
    register_default_tool_policies,
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


def test_registry_registers_exactly_six_production_tools_all_adapter_backed():
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    registrations = registry.registrations()
    assert tuple(
        registration.descriptor.name for registration in registrations
    ) == (
        "workspace_read_file",
        "workspace_write_file",
        "list_files",
        "analyze_excel",
        "get_system_status",
        "complex_workflow_simulator",
    )
    # 全部六个 Tool 都是 adapter-backed，且 Descriptor/Adapter Tool identity 一致
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
    # WP2-B：测试桩注入确定性 governance（5 个 production Agent 对该测试 Tool
    # explicit ALLOW），使测试 Tool 走与生产一致的两级 Gate。
    catalog = ToolPolicyCatalog(
        tool_registry=registry,
        agent_registry=DEFAULT_AGENT_REGISTRY,
    )
    catalog.register(
        ToolPolicy(
            tool_name=safe_name,
            allowed_agent_ids=frozenset(PRODUCTION_AGENT_IDS),
            approval_required_threshold=ToolRiskLevel.HIGH,
        )
    )
    catalog.freeze()
    governance_service = ToolGovernanceService(catalog, DEFAULT_AGENT_REGISTRY)
    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    router.tool_governance_service = governance_service
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
    system_text = "\n".join(
        message["content"] for message in messages if message["role"] == "system"
    )
    tool_messages = [
        message["content"]
        for message in messages
        if message["role"] == "user"
        and "unique-runtime-observation" in message["content"]
    ]
    assert "unique-runtime-observation" not in system_text
    assert len(tool_messages) == 1
    assert tool_messages[0].count("unique-runtime-observation") == 1
    assert "get_system_status" in tool_messages[0]
    assert "不可信外部数据" in system_text


def test_native_invalid_arguments_are_repaired_once_before_execution():
    calls = {"tool": 0, "model": 0, "governance": 0, "execution": 0}
    adapter = LegacyStringToolAdapter(
        tool_name="get_system_status",
        function=lambda value: calls.__setitem__("tool", calls["tool"] + 1) or value,
    )
    router = make_router_for_tool_path(
        tool_name="get_system_status", tool_args="", adapter=adapter
    )
    router._supports_native_tool_calling = lambda *_args: True
    router.max_tokens = 32
    router.tool_plan_max_tokens = 16
    router.tool_governance_service = RecordingGovernanceService(
        router.tool_governance_service, calls
    )
    router.tool_execution_service = RecordingToolExecutionService(
        router.tool_execution_service, calls
    )
    context, _ = create_run_context(entry_agent_id="core_router", timeout_seconds=2)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=1)))
    tool_name = adapter.spec.tool_name
    responses = iter((
        SimpleNamespace(
            output="", response=SimpleNamespace(
                native_tool_call=NativeToolCall("first", tool_name, 42),
                assistant_message={"role": "assistant", "tool_calls": []},
            )
        ),
        SimpleNamespace(
            output="", response=SimpleNamespace(
                native_tool_call=NativeToolCall("repair", tool_name, "correct"),
                assistant_message={"role": "assistant", "tool_calls": [{"id": "repair"}]},
            )
        ),
        SimpleNamespace(output="final", response=SimpleNamespace(native_tool_call=None)),
    ))

    def invoke(**_kwargs):
        calls["model"] += 1
        return next(responses)

    router._invoke_model_contract = invoke
    result = router._complete_final_response(
        "core_router", "use the tool", run_context=context,
        capability_requirements=TaskCapabilityRequirements(), unified_invocation=True,
    )

    assert result == "final"
    assert calls == {"tool": 1, "model": 3, "governance": 1, "execution": 1}


@pytest.mark.parametrize(
    "repair_call",
    (
        None,  # content-only / malformed repair response
        NativeToolCall("repair", "different_tool", "correct"),
        NativeToolCall("repair", "get_system_status", 42),
    ),
    ids=("content_only", "different_tool", "second_invalid_arguments"),
)
def test_native_repair_failure_stops_before_governance_approval_and_execution(repair_call):
    calls = {"tool": 0, "model": 0, "governance": 0, "execution": 0}
    adapter = LegacyStringToolAdapter(
        tool_name="get_system_status",
        function=lambda value: calls.__setitem__("tool", calls["tool"] + 1) or value,
    )
    router = make_router_for_tool_path(
        tool_name="get_system_status", tool_args="", adapter=adapter
    )
    router._supports_native_tool_calling = lambda *_args: True
    router.max_tokens = 32
    router.tool_plan_max_tokens = 16
    router.tool_governance_service = RecordingGovernanceService(
        router.tool_governance_service, calls
    )
    router.tool_execution_service = RecordingToolExecutionService(
        router.tool_execution_service, calls
    )

    class ApprovalSpy:
        calls = 0

        def request_approval(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("repair failure must not request approval")

    approval_spy = ApprovalSpy()
    context, _ = create_run_context(entry_agent_id="core_router", timeout_seconds=2)
    context.attach_budget_ledger(BudgetLedger(RunBudget(max_tool_calls=1)))
    tool_name = adapter.spec.tool_name
    responses = iter((
        SimpleNamespace(output="", response=SimpleNamespace(
            native_tool_call=NativeToolCall("first", tool_name, 42),
            assistant_message={"role": "assistant", "tool_calls": []},
        )),
        SimpleNamespace(output="", response=SimpleNamespace(
            native_tool_call=repair_call,
            assistant_message={"role": "assistant", "tool_calls": [{"id": "repair"}]},
        )),
    ))

    def invoke(**_kwargs):
        calls["model"] += 1
        return next(responses)

    router._invoke_model_contract = invoke
    with pytest.raises(ToolExecutionFailed):
        router._complete_final_response(
            "core_router", "use the tool", run_context=context,
            capability_requirements=TaskCapabilityRequirements(), unified_invocation=True,
            approval_controller=approval_spy,
        )
    assert calls == {"tool": 0, "model": 2, "governance": 0, "execution": 0}
    assert approval_spy.calls == 0


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
    system_text = "\n".join(
        message["content"] for message in messages if message["role"] == "system"
    )
    tool_messages = [
        message["content"]
        for message in messages
        if message["role"] == "user"
        and "complex_workflow_simulator" in message["content"]
    ]
    assert "complex_workflow_simulator" not in system_text
    assert '"compensation_attempted":false' not in system_text
    assert len(tool_messages) == 1
    assert '"compensation_attempted":false' in tool_messages[0]
    assert "不可信外部数据" in system_text


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
        "workspace_read_file",
        "workspace_write_file",
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
    assert "operation_id 和幂等键由系统补齐" in prompt
    assert "failure_injection" not in prompt
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
    # 广义业务动作可到达 planner，无须给出 Tool name；普通聊天不额外触发一次
    # 未经模型选择/预算路径的 planner 调用。
    assert router._tool_intent_likely("请对 demo-resource 增加一个项目") is True
    assert router._tool_intent_likely("解释一下什么是幂等性") is False


def _planner_router(responses):
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    router.agents_config = DEFAULT_AGENT_REGISTRY.legacy_display_config()
    router.tool_plan_max_tokens = 120
    iterator = iter(responses)
    router._collect_model_response = lambda *_args, **_kwargs: next(iterator)
    return router


class RecordingGovernanceService:
    """记录 native repair 是否在 validation 前错误进入 Governance。"""

    def __init__(self, delegate, calls):
        self._delegate = delegate
        self._calls = calls

    def authorize_tool(self, *args):
        self._calls["governance"] += 1
        return self._delegate.authorize_tool(*args)

    def evaluate_invocation(self, *args):
        return self._delegate.evaluate_invocation(*args)


class RecordingToolExecutionService:
    def __init__(self, delegate, calls):
        self._delegate = delegate
        self._calls = calls

    def execute_sync(self, *args, **kwargs):
        self._calls["execution"] += 1
        return self._delegate.execute_sync(*args, **kwargs)


def test_natural_language_selects_simulator_without_wire_contract():
    router = _planner_router(
        [
            'CALL: complex_workflow_simulator({"resource_key":"demo-resource",'
            '"execution_mode":"NON_IDEMPOTENT_SIMULATION","items":[{"item_id":'
            '"item-1","action":"ADD","quantity":1}]})'
        ]
    )
    request = "对 demo-resource 中的 item-1 做一次真实的增加 1 操作；按系统规则处理风险。"
    assert "complex_workflow_simulator" not in request
    assert "NON_IDEMPOTENT_SIMULATION" not in request
    selected = router._plan_tool_call(
        [{"role": "system", "content": "ignored"}, {"role": "user", "content": request}],
        "core_router",
    )
    assert selected is not None
    assert selected[0] == "complex_workflow_simulator"
    invocation = router.tool_registry.require(selected[0]).adapter.build_invocation(selected[1])
    arguments = invocation.arguments
    assert invocation.resource_key == "demo-resource"
    assert arguments["operation_id"].startswith("workflow-")
    assert "failure_injection" not in arguments

    catalog = ToolPolicyCatalog(
        tool_registry=router.tool_registry,
        agent_registry=DEFAULT_AGENT_REGISTRY,
    )
    register_default_tool_policies(catalog)
    catalog.freeze()
    registration = router.tool_registry.require(selected[0])
    decision = ToolGovernanceService(
        catalog, DEFAULT_AGENT_REGISTRY
    ).evaluate_invocation(
        ToolGovernanceContext(
            principal_agent_id="core_router", run_id="run-1", step_id="step-1"
        ),
        registration,
        invocation,
        registration.adapter.spec_for(invocation),
    )
    assert decision.risk_level is ToolRiskLevel.HIGH
    assert decision.outcome is ToolGovernanceOutcome.APPROVAL_REQUIRED


def test_generated_identities_are_per_invocation_and_idempotency_is_retained():
    adapter = ComplexWorkflowToolAdapter()
    arguments = json.dumps(
        {
            "resource_key": "demo-resource",
            "execution_mode": "IDEMPOTENT_COMMIT",
            "items": [{"item_id": "item-1", "action": "ADD", "quantity": 1}],
        }
    )
    first = adapter.build_invocation(arguments)
    second = adapter.build_invocation(arguments)

    assert first.arguments["operation_id"] != second.arguments["operation_id"]
    assert first.idempotency_key != second.idempotency_key
    assert first.arguments["idempotency_key"] == first.idempotency_key


def test_ordinary_chat_skips_planner_and_preserves_no_tool():
    router = _planner_router(["NO_TOOL"])
    assert router._plan_tool_call(
        [{"role": "system", "content": "ignored"}, {"role": "user", "content": "解释一下什么是幂等性。"}],
        "core_router",
    ) is None


def test_tool_name_mention_without_invocation_can_return_no_tool():
    router = _planner_router(["NO_TOOL"])
    assert router._plan_tool_call(
        [
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": "解释 complex_workflow_simulator 的作用。"},
        ],
        "core_router",
    ) is None


def test_natural_language_selects_existing_status_tool():
    router = _planner_router(["CALL: get_system_status()"])
    request = "请告诉我这台电脑当前的基本状态。"
    assert "get_system_status" not in request
    assert router._plan_tool_call(
        [{"role": "system", "content": "ignored"}, {"role": "user", "content": request}],
        "core_router",
    ) == ("get_system_status", "")


def test_bounded_validation_repair_revalidates_once():
    router = _planner_router(
        [
            'CALL: complex_workflow_simulator({"resource_key":"demo-resource",'
            '"execution_mode":"DRY_RUN","items":[{"item_id":"item-1",'
            '"action":"ADD","quantity":1}]})'
        ]
    )
    adapter = router.tool_registry.require("complex_workflow_simulator").adapter
    invocation = router._build_valid_tool_invocation(
        adapter=adapter,
        tool_name="complex_workflow_simulator",
        tool_args='{"resource_key":"demo-resource","execution_mode":"INVALID","items":[]}',
        messages=[{"role": "system", "content": "ignored"}, {"role": "user", "content": "预演增加 1"}],
        agent_id="core_router",
    )
    assert invocation.resource_key == "demo-resource"
    assert invocation.arguments["execution_mode"] == "DRY_RUN"


def test_bounded_validation_repair_stops_after_one_failed_retry():
    router = _planner_router(['CALL: complex_workflow_simulator({"execution_mode":"INVALID"})'])
    adapter = router.tool_registry.require("complex_workflow_simulator").adapter
    with pytest.raises(Exception) as captured:
        router._build_valid_tool_invocation(
            adapter=adapter,
            tool_name="complex_workflow_simulator",
            tool_args='{"execution_mode":"INVALID"}',
            messages=[{"role": "system", "content": "ignored"}, {"role": "user", "content": "预演"}],
            agent_id="core_router",
        )
    assert captured.value.safe_error_code == "TOOL_VALIDATION_ERROR"


def test_explicit_registered_tool_call_with_json_bypasses_model_planner():
    registry = ToolRegistry()
    register_all_tools(registry)
    registry.freeze()
    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    request = (
        "请明确调用 complex_workflow_simulator，执行以下非幂等模拟操作："
        + complex_payload(execution_mode="NON_IDEMPOTENT_SIMULATION")
    )

    assert router._plan_tool_call(
        [{"role": "user", "content": request}], "core_router"
    ) == ("complex_workflow_simulator", complex_payload(
        execution_mode="NON_IDEMPOTENT_SIMULATION"
    ))
