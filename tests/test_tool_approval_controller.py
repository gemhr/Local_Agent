"""ToolApprovalController unit tests (Stage5-Phase7-WP1).

覆盖：create request / duplicate active、approve、reject、duplicate approve、
approve vs reject、cross binding、claim exactly once、approve then claim、
cancel/timeout before claim、late decision、unknown approval、closed/inactive
controller、真实并发竞争，以及 worker wait_for_decision 语义。
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
    ApprovalCommandErrorCode,
    ApprovalDecisionValue,
    ApprovalError,
    ApprovalStatus,
    AgentStateApprovalBridge,
    ToolApprovalController,
)
from core.runtime.cancellation import CancellationReason
from core.runtime.context import RunContext
from core.runtime.tool_contract import ToolInvocation


def _make_invocation(
    *,
    tool_name: str = "complex_workflow_simulator",
    args: dict | None = None,
    invocation_id: str | None = None,
    idempotency_key: str | None = "key-1",
    resource_key: str | None = "resource-1",
) -> ToolInvocation:
    return ToolInvocation.create(
        tool_name=tool_name,
        arguments=args or {"operation_id": "op-1"},
        invocation_id=invocation_id,
        idempotency_key=idempotency_key,
        resource_key=resource_key,
    )


class _Harness:
    """真实 RunContext + AgentState + EventChannel 的最小 approval 装配。

    Owner Event Loop 是创建 harness 时的 running loop。
    """

    def __init__(self, *, timeout_seconds: float = 60.0) -> None:
        self.loop = asyncio.get_running_loop()
        self.context, self.source = create_run_context(
            entry_agent_id="core_router", timeout_seconds=timeout_seconds
        )
        self.context.attach_budget_ledger(
            BudgetLedger(
                RunBudget(max_tool_calls=4, max_retries=2),
                deadline_remaining=self.context.remaining_seconds,
            )
        )
        self.state = AgentState.for_run_context(self.context.run_id)
        self.machine = AgentStateMachine()
        self.state.add_step("step", "tool step")
        self.state.start_step("step")
        self.bridge = AgentStateApprovalBridge(self.machine, self.state)
        self.channel = RuntimeEventChannel(
            16,
            run_id=self.context.run_id,
            cancellation_token=self.context.cancellation_token,
        )
        self.run_emitter = RunEventEmitter(
            run_id=self.context.run_id,
            trace_id=self.context.trace_id,
            channel=self.channel,
        )
        self.step_emitter = self.run_emitter.for_step("step")
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

    async def request(self, invocation: ToolInvocation, **kwargs) -> object:
        return await self.controller.request_approval_async(
            step_id="step",
            invocation=invocation,
            tool_name=invocation.tool_name,
            risk_level=kwargs.get("risk_level", "HIGH"),
            risk_facts=kwargs.get("risk_facts", ()),
            event_emitter=kwargs.get("event_emitter", self.step_emitter),
        )

    async def decide(
        self,
        approval_id: str,
        invocation_binding_digest: str,
        decision,
        actor_id=None,
    ):
        return await self.controller.decide_async(
            run_id=self.context.run_id,
            approval_id=approval_id,
            invocation_binding_digest=invocation_binding_digest,
            decision=decision,
            actor_id=actor_id,
        )

    async def claim(self, approval_id: str, invocation: ToolInvocation):
        return await self.controller.claim_execution_async(
            approval_id=approval_id, invocation=invocation
        )

    def cancel(self) -> None:
        self.source.cancel(CancellationReason.REQUEST_CANCELLED)

    async def sync_in_thread(self, fn, *args):
        """在真实后台线程中运行 controller 同步入口（owner loop 保持运行）。"""
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


@pytest.mark.asyncio
async def test_create_request_frozen_fields_and_step_waiting():
    harness = _Harness()
    try:
        invocation = _make_invocation()
        request = await harness.request(invocation)
        assert request.run_id == harness.context.run_id
        assert request.approval_id
        assert request.step_id == "step"
        assert request.invocation_id == invocation.invocation_id
        assert request.tool_name == "complex_workflow_simulator"
        assert request.arguments_digest == invocation.arguments_digest
        assert len(request.invocation_binding_digest) == 64
        assert request.risk_level == "HIGH"
        # 同一 invocation 不允许第二个 active approval。
        with pytest.raises(ApprovalError) as exc:
            await harness.request(invocation)
        assert (
            exc.value.error_code
            is ApprovalCommandErrorCode.DUPLICATE_INVOCATION
        )
        # Step 进入 WAITING_FOR_APPROVAL，started_at 保留、仍在 active。
        step = harness.state.steps["step"]
        assert step.status is StepStatus.WAITING_FOR_APPROVAL
        assert step.started_at is not None
        assert step.ended_at is None
        assert "step" in harness.state.active_step_ids
        harness.state.validate()
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_request_without_emitter_no_journal_fail_closed():
    """Requested evidence 无法可靠发布时必须 fail closed。"""
    harness = _Harness()
    try:
        invocation = _make_invocation()
        await harness.channel.close()
        with pytest.raises(ApprovalError) as exc:
            await harness.request(invocation, event_emitter=harness.step_emitter)
        assert exc.value.error_code is ApprovalCommandErrorCode.PUBLICATION_FAILED
        # Step 未进入 waiting。
        assert harness.state.steps["step"].status is StepStatus.RUNNING
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_approve_then_claim_runs_claim_returns_execution_claimed():
    harness = _Harness()
    try:
        invocation = _make_invocation()
        request = await harness.request(invocation)
        decided = await harness.decide(
            request.approval_id,
            request.invocation_binding_digest,
            ApprovalDecisionValue.APPROVE,
        )
        assert decided.ok
        assert decided.effective_status is ApprovalStatus.APPROVED
        # APPROVED 本身不执行工具。
        claim = await harness.claim(request.approval_id, invocation)
        assert claim.ok
        assert claim.effective_status is ApprovalStatus.EXECUTION_CLAIMED
        # 第二次 claim -> 不允许。
        claim2 = await harness.claim(request.approval_id, invocation)
        assert not claim2.ok
        assert (
            claim2.safe_error_code
            == ApprovalCommandErrorCode.CLAIM_ALREADY_EXECUTED.value
        )
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_decided_publish_failure_fail_closed_no_authorization():
    """TOOL_APPROVAL_DECIDED 无法可靠发布 -> APPROVE 不能成为 execution
    authorization；claim 必须失败（工具零执行）。"""
    harness = _Harness()
    try:
        invocation = _make_invocation()
        request = await harness.request(invocation)
        # 关闭 channel 使后续 DECIDED publish 失败。
        await harness.channel.close()
        decided = await harness.decide(
            request.approval_id,
            request.invocation_binding_digest,
            ApprovalDecisionValue.APPROVE,
        )
        assert not decided.ok
        assert (
            decided.safe_error_code
            == ApprovalCommandErrorCode.PUBLICATION_FAILED.value
        )
        assert decided.effective_status is ApprovalStatus.PENDING
        # 即使 channel 恢复也不能 claim（未 APPROVED）。
        claim = await harness.claim(request.approval_id, invocation)
        assert (
            claim.safe_error_code
            == ApprovalCommandErrorCode.CLAIM_NOT_APPROVED.value
        )
    finally:
        harness.controller.close()


@pytest.mark.asyncio
async def test_duplicate_approve_is_idempotent_no_second_wake():
    harness = _Harness()
    try:
        invocation = _make_invocation()
        request = await harness.request(invocation)
        first = await harness.decide(
            request.approval_id,
            request.invocation_binding_digest,
            ApprovalDecisionValue.APPROVE,
        )
        second = await harness.decide(
            request.approval_id,
            request.invocation_binding_digest,
            ApprovalDecisionValue.APPROVE,
        )
        assert first.effective_status is ApprovalStatus.APPROVED
        assert second.effective_status is ApprovalStatus.APPROVED
        assert second.idempotent is True
        assert not second.safe_error_code
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_approve_vs_reject_first_wins():
    harness = _Harness()
    try:
        invocation = _make_invocation()
        request = await harness.request(invocation)
        first = await harness.decide(
            request.approval_id,
            request.invocation_binding_digest,
            ApprovalDecisionValue.REJECT,
        )
        second = await harness.decide(
            request.approval_id,
            request.invocation_binding_digest,
            ApprovalDecisionValue.APPROVE,
        )
        assert first.effective_status is ApprovalStatus.REJECTED
        assert not first.safe_error_code
        assert second.effective_status is ApprovalStatus.REJECTED
        assert second.safe_error_code == ApprovalCommandErrorCode.DECISION_CONFLICT.value
        # reject 后 zero claim，zero execution。
        claim = await harness.claim(request.approval_id, invocation)
        assert claim.safe_error_code == ApprovalCommandErrorCode.CLAIM_NOT_APPROVED.value
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_cross_binding_rejected():
    harness = _Harness()
    try:
        invocation_a = _make_invocation(invocation_id="inv-A")
        request = await harness.request(invocation_a)
        # 同一 tool_name、类似 args 的 B invocation 有自己的 binding digest；
        # B 的 digest 不能授权 A 的 approval。（同一 Step 已 WAITING，不能为
        # B 再建第二个 active approval，故直接计算 B 的真实 binding digest。）
        from core.runtime.approval import compute_invocation_binding_digest
        from core.runtime.tool_contract import safe_key_digest

        invocation_b = _make_invocation(
            invocation_id="inv-B", args={"operation_id": "op-2"}
        )
        digest_b = compute_invocation_binding_digest(
            invocation_identity_digest=safe_key_digest(
                invocation_b.invocation_id
            ),
            tool_name=invocation_b.tool_name,
            arguments_digest=invocation_b.arguments_digest,
            idempotency_key_digest=safe_key_digest(invocation_b.idempotency_key),
            resource_key_digest=safe_key_digest(invocation_b.resource_key),
            risk_level="HIGH",
            risk_facts=(),
        )
        assert digest_b != request.invocation_binding_digest
        decision_b = await harness.decide(
            request.approval_id,
            digest_b,
            ApprovalDecisionValue.APPROVE,
        )
        assert (
            decision_b.safe_error_code
            == ApprovalCommandErrorCode.BINDING_MISMATCH.value
        )
        assert decision_b.effective_status is ApprovalStatus.PENDING
        claim_b = await harness.claim(request.approval_id, invocation_b)
        assert (
            claim_b.safe_error_code
            == ApprovalCommandErrorCode.CLAIM_BINDING_MISMATCH.value
        )
        # A 不受影响。
        decision_a = await harness.decide(
            request.approval_id,
            request.invocation_binding_digest,
            ApprovalDecisionValue.APPROVE,
        )
        assert decision_a.ok
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_claim_rejects_same_id_and_tool_with_changed_immutable_fields():
    """execution claim 必须绑定完整的 frozen ToolInvocation，而非只比对 ID。"""
    harness = _Harness()
    try:
        invocation = _make_invocation(invocation_id="inv-A")
        request = await harness.request(invocation)
        decided = await harness.decide(
            request.approval_id,
            request.invocation_binding_digest,
            ApprovalDecisionValue.APPROVE,
        )
        assert decided.ok

        for replacement in (
            _make_invocation(
                invocation_id="inv-A", args={"operation_id": "other"}
            ),
            _make_invocation(invocation_id="inv-A", idempotency_key="other-key"),
            _make_invocation(invocation_id="inv-A", resource_key="other-resource"),
        ):
            claim = await harness.claim(request.approval_id, replacement)
            assert (
                claim.safe_error_code
                == ApprovalCommandErrorCode.CLAIM_BINDING_MISMATCH.value
            )
            assert (
                harness.controller.status_of(request.approval_id)
                is ApprovalStatus.APPROVED
            )

        assert (await harness.claim(request.approval_id, invocation)).ok
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_claim_requires_approval_first():
    harness = _Harness()
    try:
        invocation = _make_invocation()
        request = await harness.request(invocation)
        claim = await harness.claim(request.approval_id, invocation)
        assert claim.safe_error_code == ApprovalCommandErrorCode.CLAIM_NOT_APPROVED.value
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_unknown_approval_late_decision():
    harness = _Harness()
    try:
        result = await harness.decide("nope", "inv", ApprovalDecisionValue.APPROVE)
        assert result.safe_error_code == ApprovalCommandErrorCode.UNKNOWN_APPROVAL.value
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_approve_then_cancel_before_claim_zero_execution():
    harness = _Harness()
    try:
        invocation = _make_invocation()
        request = await harness.request(invocation)
        decided = await harness.decide(
            request.approval_id,
            request.invocation_binding_digest,
            ApprovalDecisionValue.APPROVE,
        )
        assert decided.ok
        harness.cancel()
        claim = await harness.claim(request.approval_id, invocation)
        assert claim.effective_status is ApprovalStatus.INVALIDATED_CANCELLED
        late = await harness.decide(
            request.approval_id,
            request.invocation_binding_digest,
            ApprovalDecisionValue.APPROVE,
        )
        assert late.effective_status is ApprovalStatus.INVALIDATED_CANCELLED
        # WP2：已失效 approval 的 late decision 是 typed 410 事实，非 conflict。
        assert late.safe_error_code == ApprovalCommandErrorCode.INVALIDATED.value
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_timeout_before_claim_late_approve_invalid():
    harness = _Harness()
    try:
        invocation = _make_invocation()
        request = await harness.request(invocation)
        results = await harness.controller.invalidate_async(
            status=ApprovalStatus.INVALIDATED_TIMEOUT
        )
        assert results
        assert (
            results[0].effective_status is ApprovalStatus.INVALIDATED_TIMEOUT
        )
        claim = await harness.claim(request.approval_id, invocation)
        assert claim.effective_status is ApprovalStatus.INVALIDATED_TIMEOUT
        late = await harness.decide(
            request.approval_id,
            request.invocation_binding_digest,
            ApprovalDecisionValue.APPROVE,
        )
        assert late.effective_status is ApprovalStatus.INVALIDATED_TIMEOUT
        assert late.safe_error_code == ApprovalCommandErrorCode.INVALIDATED.value
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_controller_closed_rejects_new_requests():
    harness = _Harness()
    harness.controller.close()
    invocation = _make_invocation()
    with pytest.raises(ApprovalError) as exc:
        await harness.request(invocation)
    assert exc.value.error_code is ApprovalCommandErrorCode.CONTROLLER_CLOSED
    await harness.close()


@pytest.mark.asyncio
async def test_risk_facts_digest_stable_sorted():
    from core.runtime.approval import compute_invocation_binding_digest

    invocation = _make_invocation(invocation_id="inv-A")
    idigest = invocation.invocation_id
    arguments_digest = invocation.arguments_digest
    key_digest = "k" * 0  # placeholder replaced below
    # digest 与 risk_facts tuple 顺序无关（内部排序）。
    first = compute_invocation_binding_digest(
        invocation_identity_digest=idigest,
        tool_name=invocation.tool_name,
        arguments_digest=arguments_digest,
        idempotency_key_digest=None,
        resource_key_digest=None,
        risk_level="HIGH",
        risk_facts=("B", "A"),
    )
    second = compute_invocation_binding_digest(
        invocation_identity_digest=idigest,
        tool_name=invocation.tool_name,
        arguments_digest=arguments_digest,
        idempotency_key_digest=None,
        resource_key_digest=None,
        risk_level="HIGH",
        risk_facts=("A", "B"),
    )
    assert first == second
    assert key_digest == "" or True  # keep variable referenced


@pytest.mark.asyncio
async def test_risk_facts_digest_differs_when_arguments_differ():
    from core.runtime.approval import compute_invocation_binding_digest

    invocation = _make_invocation(invocation_id="inv-A", args={"operation_id": "op-1"})
    invocation2 = _make_invocation(
        invocation_id="inv-B", args={"operation_id": "op-2"}
    )
    first = compute_invocation_binding_digest(
        invocation_identity_digest=invocation.invocation_id,
        tool_name=invocation.tool_name,
        arguments_digest=invocation.arguments_digest,
        idempotency_key_digest=None,
        resource_key_digest=None,
        risk_level="HIGH",
        risk_facts=(),
    )
    second = compute_invocation_binding_digest(
        invocation_identity_digest=invocation2.invocation_id,
        tool_name=invocation2.tool_name,
        arguments_digest=invocation2.arguments_digest,
        idempotency_key_digest=None,
        resource_key_digest=None,
        risk_level="HIGH",
        risk_facts=(),
    )
    assert first != second


# ---------------------------------------------------------------------------
# Worker wait semantics（真实后台线程 + owner loop 并行）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_wait_returns_approved_after_decide():
    harness = _Harness()
    try:
        invocation = _make_invocation()
        request_holder: list = []
        wait_result_holder: list = []

        def worker():
            request = harness.controller.request_approval(
                step_id="step",
                invocation=invocation,
                tool_name=invocation.tool_name,
                risk_level="HIGH",
                risk_facts=(),
                event_emitter=harness.step_emitter,
            )
            request_holder.append(request)
            result = harness.controller.wait_for_decision(
                approval_id=request.approval_id
            )
            wait_result_holder.append(result)

        thread = threading.Thread(target=worker)
        thread.start()
        # 等请求创建完成。
        for _ in range(200):
            if request_holder:
                break
            await asyncio.sleep(0.01)
        assert request_holder
        decided = await harness.decide(
            request_holder[0].approval_id,
            request_holder[0].invocation_binding_digest,
            ApprovalDecisionValue.APPROVE,
        )
        assert decided.ok
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert wait_result_holder
        assert (
            wait_result_holder[0].effective_status is ApprovalStatus.APPROVED
        )
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_worker_wait_returns_rejected_after_decide():
    harness = _Harness()
    try:
        invocation = _make_invocation()
        request_holder: list = []
        wait_result_holder: list = []

        def worker():
            request = harness.controller.request_approval(
                step_id="step",
                invocation=invocation,
                tool_name=invocation.tool_name,
                risk_level="HIGH",
                risk_facts=(),
                event_emitter=harness.step_emitter,
            )
            request_holder.append(request)
            result = harness.controller.wait_for_decision(
                approval_id=request.approval_id
            )
            wait_result_holder.append(result)

        thread = threading.Thread(target=worker)
        thread.start()
        for _ in range(200):
            if request_holder:
                break
            await asyncio.sleep(0.01)
        assert request_holder
        await harness.decide(
            request_holder[0].approval_id,
            request_holder[0].invocation_binding_digest,
            ApprovalDecisionValue.REJECT,
        )
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert wait_result_holder[0].effective_status is ApprovalStatus.REJECTED
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_worker_wait_raises_on_cancel():
    harness = _Harness()
    try:
        invocation = _make_invocation()
        approval_id_holder: list[str] = []
        error_holder: list = []

        def worker():
            try:
                request = harness.controller.request_approval(
                    step_id="step",
                    invocation=invocation,
                    tool_name=invocation.tool_name,
                    risk_level="HIGH",
                    risk_facts=(),
                    event_emitter=harness.step_emitter,
                )
                approval_id_holder.append(request.approval_id)
                harness.controller.wait_for_decision(
                    approval_id=request.approval_id, poll_seconds=0.005
                )
            except Exception as exc:  # pragma: no cover - assertion path
                error_holder.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        for _ in range(200):
            if approval_id_holder:
                break
            await asyncio.sleep(0.01)
        assert approval_id_holder
        harness.cancel()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert error_holder
    finally:
        await harness.close()


# ---------------------------------------------------------------------------
# Concurrency tests（真实并发，非串行模拟）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_concurrent_approves_single_claim():
    harness = _Harness()
    try:
        invocation = _make_invocation()
        request = await harness.request(invocation)
        results = await asyncio.gather(
            *[
                harness.decide(
                    request.approval_id,
                    request.invocation_binding_digest,
                    ApprovalDecisionValue.APPROVE,
                )
                for _ in range(2)
            ]
        )
        assert all(r.ok for r in results)
        assert len([r for r in results if r.idempotent]) == 1
        assert (
            harness.controller.status_of(request.approval_id)
            is ApprovalStatus.APPROVED
        )
        claims = await asyncio.gather(
            *[
                harness.claim(request.approval_id, invocation)
                for _ in range(2)
            ]
        )
        claimed = [
            c
            for c in claims
            if c.ok and c.effective_status is ApprovalStatus.EXECUTION_CLAIMED
        ]
        assert len(claimed) == 1
        assert (
            harness.controller.status_of(request.approval_id)
            is ApprovalStatus.EXECUTION_CLAIMED
        )
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_approve_vs_reject_race_first_wins():
    harness = _Harness()
    try:
        invocation = _make_invocation()
        request = await harness.request(invocation)
        results = await asyncio.gather(
            harness.decide(
                request.approval_id,
                request.invocation_binding_digest,
                ApprovalDecisionValue.APPROVE,
            ),
            harness.decide(
                request.approval_id,
                request.invocation_binding_digest,
                ApprovalDecisionValue.REJECT,
            ),
        )
        statuses = {r.effective_status for r in results}
        conflicts = [
            r
            for r in results
            if r.safe_error_code
            == ApprovalCommandErrorCode.DECISION_CONFLICT.value
        ]
        assert statuses in (
            {ApprovalStatus.APPROVED},
            {ApprovalStatus.REJECTED},
        )
        assert len(conflicts) == 1
        final_status = harness.controller.status_of(request.approval_id)
        assert final_status in (
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
        )
        claim = await harness.claim(request.approval_id, invocation)
        if final_status is ApprovalStatus.APPROVED:
            assert claim.ok
        else:
            assert (
                claim.safe_error_code
                == ApprovalCommandErrorCode.CLAIM_NOT_APPROVED.value
            )
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_approve_vs_cancel_race_consistent():
    harness = _Harness()
    try:
        invocation = _make_invocation()
        request = await harness.request(invocation)

        async def do_cancel():
            await asyncio.sleep(0)
            harness.cancel()

        results = await asyncio.gather(
            harness.decide(
                request.approval_id,
                request.invocation_binding_digest,
                ApprovalDecisionValue.APPROVE,
            ),
            do_cancel(),
        )
        _decided = results[0]
        final_status = harness.controller.status_of(request.approval_id)
        assert final_status in (
            ApprovalStatus.APPROVED,
            ApprovalStatus.INVALIDATED_CANCELLED,
        )
        claim = await harness.claim(request.approval_id, invocation)
        if (
            final_status is ApprovalStatus.APPROVED
            and harness.context.cancellation_token.is_cancelled()
        ):
            assert claim.effective_status is ApprovalStatus.INVALIDATED_CANCELLED
        elif final_status is ApprovalStatus.INVALIDATED_CANCELLED:
            assert (
                claim.safe_error_code
                == ApprovalCommandErrorCode.CLAIM_NOT_APPROVED.value
            )
    finally:
        await harness.close()
