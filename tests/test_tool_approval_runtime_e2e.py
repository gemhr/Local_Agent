"""Stage5-Phase7-WP1 full Coordinated scope HITL wiring tests.

以真实 CoordinatedRuntimeFactory / RunCoordinator / RunRegistry 驱动整个 Run。
Driver 模拟真实 router 行为：检测到工具需要审批时调用 run-scoped controller
request -> wait -> approve(reject/claim)，验证：

- 每个 active Run 恰好一个 run-scoped controller（factory wiring）；
- registry command surface 能对 active Run 转发 approve/reject；
- approve 后原 invocation 恰好执行一次，Step SUCCEEDED；
- reject 后 Step FAILED / TOOL_APPROVAL_REJECTED、零 ToolExecution；
- pending cancel 后 late approve 零执行，Run 走 cancellation 终态；
- run terminal 会 invalidate/close controller。
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from core.runtime import (
    CoordinatedRuntimeFactory,
    RunStatus,
    StepStatus,
    StopReason,
)
from core.runtime.approval import (
    ApprovalCommandResult,
    ApprovalDecisionValue,
    ApprovalStatus,
    ToolApprovalController,
)
from core.runtime.cancellation import CancellationReason
from core.runtime.events import RuntimeEventType
from tests._runtime_assembly_fixtures import FakeRouter, make_services


def _event_records(services, run_id: str):
    return services.event_journal.read_after(run_id, 0, 1000)


class ApprovalDriverRouter(FakeRouter):
    """单步 direct Run；complete_single_agent 中模拟真实工具审批等待。

    通过 kwargs 中由 ResolvedSingleStepDriver 注入的 approval_controller 与
    event_emitter 执行 request/wait/claim。工具执行被记录为一次副作用。
    """

    def __init__(self, *, approve: bool | None = None) -> None:
        super().__init__()
        self.approve = approve
        self.executed = 0
        self.rejected_steps: list[str] = []
        self.late_after_cancel: list[str] = []
        self.run_id: str | None = None

    def complete_planning_decision(self, user_request: str, **kwargs) -> str:
        return '{"schema_version": 1, "decision": "DIRECT_ANSWER", "agent_id": "core_router", "reason_code": "HITL"}'

    def complete_single_agent(self, agent_id: str, query: str, **kwargs) -> str:
        self.calls_for_agent(agent_id)
        run_context = kwargs.get("run_context")
        event_emitter = kwargs.get("event_emitter")
        controller = kwargs.get("approval_controller")
        self.run_id = run_context.run_id if run_context is not None else None
        step_id = event_emitter.step_id if event_emitter is not None else "answer"
        assert controller is not None, "Coordinated router 必须注入 approval_controller"

        from core.runtime.tool_contract import ToolInvocation

        invocation = ToolInvocation.create(
            tool_name="complex_workflow_simulator",
            arguments={"operation_id": "hitl-op"},
        )
        request = controller.request_approval(
            step_id=step_id,
            invocation=invocation,
            tool_name=invocation.tool_name,
            risk_level="HIGH",
            risk_facts=(),
            event_emitter=event_emitter,
        )
        result = controller.wait_for_decision(approval_id=request.approval_id)
        if result.effective_status in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.EXECUTION_CLAIMED,
        }:
            claim = controller.claim_execution(
                approval_id=request.approval_id, invocation=invocation
            )
            if claim.ok:
                self.executed += 1
                return f"executed-{invocation.invocation_id}"
            return f"claim-failed-{claim.effective_status.value}"
        if result.effective_status is ApprovalStatus.REJECTED:
            self.rejected_steps.append(step_id)
            from core.runtime.approval import ToolApprovalRejectedError

            raise ToolApprovalRejectedError(
                "Tool 调用已被拒绝审批（TOOL_APPROVAL_REJECTED）"
            )
        if result.effective_status is ApprovalStatus.INVALIDATED_TIMEOUT:
            from core.runtime.context import RunDeadlineExceededError

            raise RunDeadlineExceededError("approval wait exceeded deadline")
        # INVALIDATED_CANCELLED / unknown。
        self.late_after_cancel.append(step_id)
        raise RuntimeError("simulated approval cancellation")

    def calls_for_agent(self, agent_id: str):
        if not hasattr(self, "_calls"):
            self._calls = {}
        self._calls[agent_id] = self._calls.get(agent_id, 0) + 1


async def _poll(condition, timeout=15.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = condition()
        if value:
            return value
        await asyncio.sleep(0.02)
    return condition()


async def _start_run(router, **scope_kwargs):
    services = make_services(snapshot_enabled=False)
    registry = services.run_registry
    scope = await CoordinatedRuntimeFactory(router, services).create_run_scope(
        "core_router", "question", **scope_kwargs
    )
    run_id = scope.run_id
    execute_task = asyncio.create_task(scope.execute())
    await _poll(lambda: registry.get(run_id) is not None)
    return services, registry, scope, run_id, execute_task


async def _wait_approval_pending(services, run_id, controller_holder):
    """等待出现 TOOL_APPROVAL_REQUESTED 且 controller 有 pending。"""

    def _state():
        records = _event_records(services, run_id)
        requested = [
            r
            for r in records
            if r.event_type is RuntimeEventType.TOOL_APPROVAL_REQUESTED
        ]
        controller = controller_holder.get("controller")
        if requested and controller is not None and controller.pending_count() >= 1:
            return (requested, controller)
        return None

    result = await _poll(lambda: _state())
    assert result is not None, "approval request 未产生"
    return result


def _controller_of(scope) -> ToolApprovalController:
    return scope.coordinator.tool_approval_controller


@pytest.mark.asyncio
async def test_scope_has_exactly_one_run_scoped_controller():
    router = ApprovalDriverRouter(approve=True)
    services, registry, scope, run_id, execute_task = await _start_run(router)
    holder = {}
    try:
        controller = await _poll(
            lambda: scope.coordinator.tool_approval_controller or None
        )
        holder["controller"] = controller
        assert controller is not None
        assert controller.run_id == run_id
        # run_handle 的 resolver 指向同一 controller。
        assert registry.get(run_id).approval_controller() is controller
        requested, controller = await _wait_approval_pending(services, run_id, holder)
        approval_id = requested[0].safe_payload["approval_id"]
        approval_request = controller.get(approval_id)
        result = await registry.get(run_id).approve_tool_approval(
            approval_id=approval_id,
            invocation_binding_digest=approval_request.invocation_binding_digest,
        )
        assert result.ok
        result = await execute_task
        assert result.status is RunStatus.SUCCEEDED
        # Run terminal 已 close controller。
        assert scope.coordinator.tool_approval_controller is None
    finally:
        await scope.close()


@pytest.mark.asyncio
async def test_registry_command_surface_approve_executes_once():
    router = ApprovalDriverRouter(approve=True)
    services, registry, scope, run_id, execute_task = await _start_run(router)
    holder = {}
    try:
        controller = await _poll(
            lambda: scope.coordinator.tool_approval_controller or None
        )
        holder["controller"] = controller
        requested, controller = await _wait_approval_pending(services, run_id, holder)
        assert len(requested) == 1
        approval_id = requested[0].safe_payload["approval_id"]
        approval_request = controller.get(approval_id)
        assert approval_request is not None

        handle = registry.get(run_id)
        result = await handle.approve_tool_approval(
            approval_id=approval_id,
            invocation_binding_digest=approval_request.invocation_binding_digest,
            actor_id="reviewer",
        )
        assert isinstance(result, ApprovalCommandResult)
        assert result.ok
        assert result.effective_status is ApprovalStatus.APPROVED

        run_result = await execute_task
        assert run_result.status is RunStatus.SUCCEEDED
        assert run_result.stop_reason is StopReason.COMPLETED
        assert router.executed == 1
        records = _event_records(services, run_id)
        types = [r.event_type for r in records]
        requested_idx = types.index(RuntimeEventType.TOOL_APPROVAL_REQUESTED)
        decided_idx = types.index(RuntimeEventType.TOOL_APPROVAL_DECIDED)
        assert requested_idx < decided_idx
        decided = [
            r
            for r in records
            if r.event_type is RuntimeEventType.TOOL_APPROVAL_DECIDED
        ]
        assert decided[0].safe_payload["decision_status"] == "APPROVED"
        assert decided[0].safe_payload["actor_id_digest"] is not None
        # 无 raw args / path。
        for r in records:
            if r.event_type in {
                RuntimeEventType.TOOL_APPROVAL_REQUESTED,
                RuntimeEventType.TOOL_APPROVAL_DECIDED,
            }:
                assert "hitl-op" not in repr(r.safe_payload)
    finally:
        await scope.close()


@pytest.mark.asyncio
async def test_registry_command_surface_reject_fails_step():
    router = ApprovalDriverRouter(approve=False)
    services, registry, scope, run_id, execute_task = await _start_run(router)
    holder = {}
    try:
        controller = await _poll(
            lambda: scope.coordinator.tool_approval_controller or None
        )
        holder["controller"] = controller
        requested, controller = await _wait_approval_pending(services, run_id, holder)
        approval_id = requested[0].safe_payload["approval_id"]
        approval_request = controller.get(approval_id)
        handle = registry.get(run_id)
        result = await handle.reject_tool_approval(
            approval_id=approval_id,
            invocation_binding_digest=approval_request.invocation_binding_digest,
        )
        assert result.ok
        assert result.effective_status is ApprovalStatus.REJECTED

        run_result = await execute_task
        assert run_result.status is RunStatus.FAILED
        assert run_result.failed_step_ids == ("answer",)
        assert router.executed == 0
        assert router.rejected_steps == ["answer"]
        records = _event_records(services, run_id)
        assert not any(
            r.event_type is RuntimeEventType.TOOL_STARTED for r in records
        )
        # Step 终态错误码（Run 级 coarse code 沿用既有 typed failure 映射）。
        completed = [
            r
            for r in records
            if r.event_type is RuntimeEventType.STEP_COMPLETED
            and r.safe_payload.get("status") == "FAILED"
        ]
        assert completed
        assert (
            completed[-1].safe_payload.get("safe_error_code")
            == "TOOL_APPROVAL_REJECTED"
        )
    finally:
        await scope.close()


@pytest.mark.asyncio
async def test_pending_cancel_invalidates_and_late_approve_zero_execution():
    router = ApprovalDriverRouter(approve=True)
    services, registry, scope, run_id, execute_task = await _start_run(router)
    holder = {}
    try:
        controller = await _poll(
            lambda: scope.coordinator.tool_approval_controller or None
        )
        holder["controller"] = controller
        requested, controller = await _wait_approval_pending(services, run_id, holder)
        approval_id = requested[0].safe_payload["approval_id"]
        # cancel Run。
        scope.request_cancel(CancellationReason.REQUEST_CANCELLED)
        # late approve（worker 尚未 claim）：controller 已失效，approve 不授权执行。
        approval_request = controller.get(approval_id)
        handle = registry.get(run_id)
        late = await handle.approve_tool_approval(
            approval_id=approval_id,
            invocation_binding_digest=approval_request.invocation_binding_digest,
        )
        assert isinstance(late, ApprovalCommandResult)
        assert late.effective_status in {
            ApprovalStatus.INVALIDATED_CANCELLED,
            ApprovalStatus.PENDING,
        }
        run_result = await execute_task
        assert run_result.status is RunStatus.CANCELLED
        assert router.executed == 0
        # wait_for_decision 观察到 cancellation 后经 raise_if_inactive 上抛，
        # 不会到达 claim；不产生任何 tool 执行。
        assert router.late_after_cancel == []
        records = _event_records(services, run_id)
        assert not any(
            r.event_type is RuntimeEventType.TOOL_STARTED for r in records
        )
    finally:
        await scope.close()
