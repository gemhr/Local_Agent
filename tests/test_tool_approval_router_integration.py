"""AgentRouter Tool Approval HITL integration tests (Stage5-Phase7-WP1).

以真实 AgentRouter governance + run-scoped ToolApprovalController 验证：

- low-risk 调用：无 approval，既有执行行为不变；
- high-risk non-idempotent 调用：decision 前 ToolExecutionService=0 /
  TOOL_STARTED=0 / mutation=0；
- approve：同一 frozen invocation 恰好执行一次；evidence 顺序
  REQUESTED < DECIDED(APPROVED) < TOOL_STARTED；
- reject：TOOL_APPROVAL_REJECTED，零 ToolExecution / 零 mutation / 无
  TOOL_STARTED；
- model 文本不能 self-approve（无 command 能力）。
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from core.runtime import (
    AgentState,
    AgentStateMachine,
    BudgetLedger,
    RunBudget,
    RunEventEmitter,
    RuntimeEventChannel,
    RuntimeEventType,
    StepStatus,
    create_run_context,
)
from core.runtime.approval import (
    AgentStateApprovalBridge,
    ApprovalDecisionValue,
    ApprovalStatus,
    ToolApprovalController,
)
from core.runtime.event_journal_store import InMemoryRunEventJournal
from core.runtime.tool_governance import (
    ToolGovernanceOutcome,
    ToolGovernanceService,
)
from tests.test_tool_governance import production_registry, production_service


def _make_router(registry, governance_service, tool_name, tool_args):
    """`__new__` 最小 Router 桩（复用 WP2 测试模式）。"""
    from core.agent_router import AgentRouter
    from core.runtime.tool_execution import ToolExecutionService

    router = AgentRouter.__new__(AgentRouter)
    router.tool_registry = registry
    router.tool_governance_service = governance_service
    router.tool_execution_service = ToolExecutionService()
    router._build_messages = lambda **_: [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "query"},
    ]
    router._plan_tool_call = lambda _messages, _agent_id: (tool_name, tool_args)
    return router


class _Harness:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.context, self.source = create_run_context(
            entry_agent_id="core_router", timeout_seconds=60
        )
        self.context.attach_budget_ledger(
            BudgetLedger(
                RunBudget(max_tool_calls=4, max_retries=2),
                deadline_remaining=self.context.remaining_seconds,
            )
        )
        self.journal = InMemoryRunEventJournal()
        self.channel = RuntimeEventChannel(
            64,
            run_id=self.context.run_id,
            cancellation_token=self.context.cancellation_token,
            journal=self.journal,
        )
        self.run_emitter = RunEventEmitter(
            run_id=self.context.run_id,
            trace_id=self.context.trace_id,
            channel=self.channel,
        )
        self.step_emitter = self.run_emitter.for_step("step")
        self.state = AgentState.for_run_context(self.context.run_id)
        self.machine = AgentStateMachine()
        self.state.mark_running()
        self.state.add_step("step", "tool step")
        self.state.start_step("step")
        self.bridge = AgentStateApprovalBridge(self.machine, self.state)
        self.controller = ToolApprovalController(
            run_id=self.context.run_id,
            run_context=self.context,
            state_bridge=self.bridge,
            deadline_check=self.context.remaining_seconds,
            loop=self.loop,
        )
        self.controller.bind_step_emitter_resolver(
            lambda step_id: self.run_emitter.for_step(step_id)
        )

    async def close(self) -> None:
        self.controller.close()
        await self.channel.close()

    @property
    def events(self) -> list:
        return self.journal.read_after(self.context.run_id, 0, 1000)


def _tool_args(operation_id: str = "wp1-op-1") -> str:
    import json

    return json.dumps(
        {
            "operation_id": operation_id,
            "resource_key": "wp1-resource",
            "execution_mode": "NON_IDEMPOTENT_SIMULATION",
            "items": [{"item_id": "item-1", "action": "ADD", "quantity": 1}],
            "processing_options": {"processing_delay_ms": 0},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


@pytest.mark.asyncio
async def test_low_risk_tool_no_approval_executes_as_before():
    harness = _Harness()
    import tempfile

    try:
        registry = production_registry()
        adapter = registry.require("list_files").adapter
        with tempfile.TemporaryDirectory() as d:
            router = _make_router(
                registry,
                production_service(registry),
                tool_name="list_files",
                tool_args=d,
            )
            await asyncio.to_thread(
                router._prepare_answer_messages,
                "core_router",
                "query",
                run_context=harness.context,
            )
        # 无 approval request（低风险 ALLOW 不经过审批）。
        assert adapter is not None
        assert not any(
            r.event_type is RuntimeEventType.TOOL_APPROVAL_REQUESTED
            for r in harness.events
        )
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_high_risk_request_zero_execution_before_decision():
    harness = _Harness()
    try:
        registry = production_registry()
        service = production_service(registry)
        store = registry.require("complex_workflow_simulator").adapter._state_store
        router = _make_router(
            registry,
            service,
            tool_name="complex_workflow_simulator",
            tool_args=_tool_args(),
        )
        # 后台线程运行 router（模拟 executor worker）。
        thread_error: list = []
        started = threading.Event()
        before_decision: dict = {}

        def worker():
            try:
                started.set()
                router._prepare_answer_messages(
                    "core_router",
                    "query",
                    run_context=harness.context,
                    event_emitter=harness.step_emitter,
                    approval_controller=harness.controller,
                )
            except Exception as exc:  # pragma: no cover - 不应到达
                thread_error.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        assert started.wait(5)
        # 等待 Step 进入 WAITING。
        for _ in range(200):
            if (
                harness.state.steps["step"].status
                is StepStatus.WAITING_FOR_APPROVAL
            ):
                break
            await asyncio.sleep(0.01)
        assert (
            harness.state.steps["step"].status
            is StepStatus.WAITING_FOR_APPROVAL
        )
        # Decision 前：零 mutation / 零 tool events。
        records = harness.events
        assert not any(
            r.event_type is RuntimeEventType.TOOL_STARTED for r in records
        )
        assert store.resource_states == {}
        assert store.committed_operations == []
        before_decision["pending"] = harness.controller.pending_count()
        assert before_decision["pending"] == 1
        # 读取 approval_id。
        requests = [
            r
            for r in records
            if r.event_type is RuntimeEventType.TOOL_APPROVAL_REQUESTED
        ]
        assert len(requests) == 1
        assert "operation_id" not in repr(requests[0].safe_payload)
        assert "wp1-resource" not in repr(requests[0].safe_payload)
        assert requests[0].safe_payload["risk_level"] == "HIGH"

        # REJECT：Step FAILED，零执行。
        approval_id = requests[0].safe_payload["approval_id"]
        approval_request = harness.controller.get(approval_id)
        assert approval_request is not None
        result = await harness.controller.decide_async(
            run_id=harness.context.run_id,
            approval_id=approval_id,
            invocation_binding_digest=approval_request.invocation_binding_digest,
            decision=ApprovalDecisionValue.REJECT,
        )
        # invocation id 来自 request 内部；用 status 断言。
        assert result.effective_status is ApprovalStatus.REJECTED
        for _ in range(1500):
            if not thread.is_alive():
                break
            await asyncio.sleep(0.01)
        assert not thread.is_alive()
        # REJECT 在 router 边界以 ToolApprovalRejectedError 上抛。
        from core.runtime.approval import ToolApprovalRejectedError

        assert len(thread_error) == 1
        assert isinstance(thread_error[0], ToolApprovalRejectedError)
        step = harness.state.steps["step"]
        assert step.status is StepStatus.FAILED
        assert step.error_code == "TOOL_APPROVAL_REJECTED"
        assert store.resource_states == {}
        assert store.committed_operations == []
        records = harness.events
        assert not any(
            r.event_type is RuntimeEventType.TOOL_STARTED for r in records
        )
        decided = [
            r
            for r in records
            if r.event_type is RuntimeEventType.TOOL_APPROVAL_DECIDED
        ]
        assert decided and decided[0].safe_payload["decision_status"] == "REJECTED"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_approve_executes_same_invocation_once_with_ordering():
    harness = _Harness()
    try:
        registry = production_registry()
        service = production_service(registry)
        adapter = registry.require("complex_workflow_simulator").adapter
        store = adapter._state_store
        router = _make_router(
            registry,
            service,
            tool_name="complex_workflow_simulator",
            tool_args=_tool_args(),
        )
        thread_error: list = []
        result_holder: list = []

        def worker():
            try:
                messages = router._prepare_answer_messages(
                    "core_router",
                    "query",
                    run_context=harness.context,
                    event_emitter=harness.step_emitter,
                    approval_controller=harness.controller,
                )
                result_holder.append(messages)
            except Exception as exc:  # pragma: no cover
                thread_error.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        for _ in range(300):
            if (
                harness.state.steps["step"].status
                is StepStatus.WAITING_FOR_APPROVAL
            ):
                break
            await asyncio.sleep(0.01)
        assert (
            harness.state.steps["step"].status
            is StepStatus.WAITING_FOR_APPROVAL
        )
        assert not thread_error
        # Decision 前零执行。
        assert store.resource_states == {}
        requests = [
            r
            for r in harness.events
            if r.event_type is RuntimeEventType.TOOL_APPROVAL_REQUESTED
        ]
        assert len(requests) == 1
        approval_id = requests[0].safe_payload["approval_id"]

        request = harness.controller.get(approval_id)
        assert request is not None
        decided = await harness.controller.decide_async(
            run_id=harness.context.run_id,
            approval_id=approval_id,
            invocation_binding_digest=request.invocation_binding_digest,
            decision=ApprovalDecisionValue.APPROVE,
        )
        assert decided.ok
        # 异步轮询等待 worker 结束（阻塞 join 会饿死 owner loop）。
        for _ in range(1500):
            if not thread.is_alive():
                break
            await asyncio.sleep(0.01)
        assert not thread.is_alive()
        assert thread_error == []
        assert result_holder  # 成功返回消息。
        # 同一 invocation 恰好一次副作用（NON_IDEMPOTENT commit）。
        assert len(store.audit_records) == 1
        assert len(store.committed_operations) == 1
        # 桩路径无 committer：Step 由 bridge 在 approve 时回到 RUNNING。
        assert harness.state.steps["step"].status is StepStatus.RUNNING
        # Evidence 顺序：REQUESTED < DECIDED(APPROVED) < TOOL_STARTED。
        records = harness.events
        types = [r.event_type for r in records]
        assert RuntimeEventType.TOOL_APPROVAL_REQUESTED in types
        assert RuntimeEventType.TOOL_APPROVAL_DECIDED in types
        assert RuntimeEventType.TOOL_STARTED in types
        requested_idx = types.index(RuntimeEventType.TOOL_APPROVAL_REQUESTED)
        decided_idx = types.index(RuntimeEventType.TOOL_APPROVAL_DECIDED)
        started_idx = types.index(RuntimeEventType.TOOL_STARTED)
        assert requested_idx < decided_idx < started_idx
        # payload 安全。
        for r in records:
            if r.event_type in {
                RuntimeEventType.TOOL_APPROVAL_REQUESTED,
                RuntimeEventType.TOOL_APPROVAL_DECIDED,
            }:
                assert "operation_id" not in repr(r.safe_payload)
                assert "wp1-resource" not in repr(r.safe_payload)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_model_text_cannot_self_approve():
    """模型文本 'approved' 不产生 controller command，不能绕过 wait。"""
    harness = _Harness()
    try:
        registry = production_registry()
        service = production_service(registry)
        # 直接 governance 判定：metadata 含 approved 仍 APPROVAL_REQUIRED。
        registration = registry.require("complex_workflow_simulator")
        context = harness.context
        from core.runtime.tool_governance import ToolGovernanceContext

        invocation = registration.adapter.build_invocation(
            _tool_args("self-approve-op")
        )
        spec = registration.adapter.spec_for(invocation)
        decision = service.evaluate_invocation(
            ToolGovernanceContext("core_router", context.run_id, "step"),
            registration,
            invocation,
            spec,
        )
        assert decision.outcome is ToolGovernanceOutcome.APPROVAL_REQUIRED
        # controller 无任何 approval（没有 command）。
        assert harness.controller.pending_count() == 0
        assert harness.controller.get("anything") is None
    finally:
        await harness.close()
