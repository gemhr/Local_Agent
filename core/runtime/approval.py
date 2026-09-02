#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run-scoped Tool Approval domain (Stage5-Phase7-WP1).

该模块实现 Minimum Credible Tool Approval HITL 的 typed domain：

- ``ApprovalRequest``：immutable/frozen 审批请求，只保存安全身份与 digest，
  绝不保存 raw arguments / prompt / path / resource content。
- ``ApprovalDecision``：只允许人类 ``APPROVE`` / ``REJECT``；``actor_id`` 只是
  受限审计标签，不构成授权。
- ``ApprovalStatus``：单 approval 内部 lifecycle。``APPROVED`` 与
  ``EXECUTION_CLAIMED`` 语义不同：Human approval 本身不执行工具；只有
  ``APPROVED -> claim_execution() -> EXECUTION_CLAIMED`` 成功的原 worker 才能
  进入 ToolExecution。
- ``ApprovalCommandResult``：不返回 raw invocation / tool result / raw args 的
  安全命令结果。
- ``ToolApprovalController``：严格 run-scoped。它是唯一 pending truth owner、
  decision CAS owner、execution-claim owner、wait/wakeup owner。它不拥有持久化、
  不执行工具、不判断风险、不做 RBAC。

Atomicity model：
所有 mutation（create/decide/invalidate/claim）都通过一个 async critical
section funnel 到 owner Event Loop，并由单一 ``asyncio.Lock`` 串行化。非 owner
loop 线程（已 claim 的同步 worker、future transport command 线程）经
``asyncio.run_coroutine_threadsafe`` 提交；owner loop 线程使用 async variant。
因此同一 invocation 最多一个 active approval、同一 approval 至多一次 execution
claim，且状态只在对应对应 Journal evidence 可靠 publish 后才对 waiter effective
（Journal-first，fail closed）。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable
from uuid import uuid4

from core.runtime.cancellation import RunCancelledError
from core.runtime.context import RunContext, RunDeadlineExceededError
from core.runtime.event_emitter import EventEmitterSyncError, StepEventEmitter
from core.runtime.event_journal import JournalError
from core.runtime.events import (
    RuntimeEventType,
    ToolApprovalDecidedPayload,
    ToolApprovalRequestedPayload,
)
from core.runtime.state import AgentState
from core.runtime.state_machine import (
    AgentStateMachine,
    StepEventType,
    StepStateEvent,
)
from core.runtime.tool_contract import (
    ToolInvocation,
    canonical_json_digest,
    safe_key_digest,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_digest(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


class ApprovalDecisionValue(str, Enum):
    """只允许的人类决定；INVALIDATED_* 是 Runtime fact，不是人类决定。"""

    APPROVE = "APPROVE"
    REJECT = "REJECT"


class ApprovalStatus(str, Enum):
    """单 approval 内部 lifecycle。

    - ``PENDING``：等待决定。
    - ``APPROVED``：人类已批准，但尚未取得 execution claim。
    - ``REJECTED``：人类已拒绝（零 ToolExecution）。
    - ``INVALIDATED_CANCELLED`` / ``INVALIDATED_TIMEOUT``：Runtime lifecycle
      fact，在 execution claim 前由 cancel/deadline 产生。
    - ``EXECUTION_CLAIMED``：唯一执行门闩已通过；只有该原 invocation 可执行。
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    INVALIDATED_CANCELLED = "INVALIDATED_CANCELLED"
    INVALIDATED_TIMEOUT = "INVALIDATED_TIMEOUT"
    EXECUTION_CLAIMED = "EXECUTION_CLAIMED"


class ApprovalCommandErrorCode(str, Enum):
    """Approval command surface 的固定安全错误码。"""

    UNKNOWN_APPROVAL = "APPROVAL_UNKNOWN"
    UNKNOWN_RUN = "APPROVAL_UNKNOWN_RUN"
    RUN_INACTIVE = "APPROVAL_RUN_INACTIVE"
    INVALID_STATE = "APPROVAL_INVALID_STATE"
    BINDING_MISMATCH = "APPROVAL_BINDING_MISMATCH"
    DUPLICATE_INVOCATION = "APPROVAL_DUPLICATE_INVOCATION"
    ALREADY_DECIDED = "APPROVAL_ALREADY_DECIDED"
    DECISION_CONFLICT = "APPROVAL_DECISION_CONFLICT"
    CONTROLLER_CLOSED = "APPROVAL_CONTROLLER_CLOSED"
    JOURNAL_UNAVAILABLE = "APPROVAL_JOURNAL_UNAVAILABLE"
    CLAIM_NOT_APPROVED = "APPROVAL_CLAIM_NOT_APPROVED"
    CLAIM_ALREADY_EXECUTED = "APPROVAL_CLAIM_ALREADY_EXECUTED"
    CLAIM_BINDING_MISMATCH = "APPROVAL_CLAIM_BINDING_MISMATCH"
    STATE_TRANSITION_FAILED = "APPROVAL_STATE_TRANSITION_FAILED"
    PUBLICATION_FAILED = "APPROVAL_PUBLICATION_FAILED"


class ApprovalError(RuntimeError):
    """Typed approval failure that never carries raw invocation content."""

    def __init__(self, error_code: ApprovalCommandErrorCode, safe_message: str) -> None:
        if not isinstance(error_code, ApprovalCommandErrorCode):
            raise TypeError("error_code 必须是 ApprovalCommandErrorCode")
        if not isinstance(safe_message, str) or not safe_message.strip():
            raise ValueError("safe_message 必须是非空安全说明")
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code.value})")


class ToolApprovalRejectedError(RuntimeError):
    """给 Step Driver/Executor 的安全 reject 结果；只携带固定错误码。

    REJECT 语义：Step 必须以 ``TOOL_APPROVAL_REJECTED`` 失败；零 resource
    authorization（如当前 Contract 让 gate 前置）、零 ToolExecution、零
    TOOL_STARTED。它不等价于 cancel Run。
    """

    error_code = "TOOL_APPROVAL_REJECTED"

    def __init__(self, safe_message: str = "Tool 调用已被拒绝审批") -> None:
        if not isinstance(safe_message, str) or not safe_message.strip():
            raise ValueError("safe_message 必须是非空安全说明")
        self.safe_message = safe_message
        super().__init__(self.safe_message)


def compute_invocation_binding_digest(
    *,
    invocation_identity_digest: str,
    tool_name: str,
    arguments_digest: str,
    idempotency_key_digest: str | None,
    resource_key_digest: str | None,
    risk_level: str | None,
    risk_facts: tuple[str, ...],
) -> str:
    """canonical JSON + SHA-256 计算 immutable invocation/risk binding digest。

    不使用 ``repr()`` / ``hash()`` / object id / process-dependent hash。只覆盖
    足以证明"同一份 immutable invocation + risk contract"的安全字段；绝不包含
    raw arguments / prompt / path / resource content。
    """
    if not isinstance(risk_facts, tuple):
        raise TypeError("risk_facts 必须是 tuple")
    if any(not isinstance(item, str) for item in risk_facts):
        raise TypeError("risk_facts 只能包含字符串")
    payload = {
        "invocation_identity_digest": invocation_identity_digest,
        "tool_name": tool_name,
        "arguments_digest": arguments_digest,
        "idempotency_key_digest": idempotency_key_digest,
        "resource_key_digest": resource_key_digest,
        "risk_level": risk_level,
        "risk_facts": sorted(risk_facts),
    }
    return canonical_json_digest(payload)


def compute_actor_id_digest(actor_id: str | None) -> str | None:
    if actor_id is None:
        return None
    return safe_key_digest(actor_id)  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Immutable/frozen typed approval request（安全投影，不包含正文）。"""

    approval_id: str
    run_id: str
    step_id: str
    invocation_id: str
    tool_name: str
    invocation_identity_digest: str
    arguments_digest: str
    idempotency_key_digest: str | None
    resource_key_digest: str | None
    risk_level: str | None
    risk_facts: tuple[str, ...]
    invocation_binding_digest: str
    requested_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.approval_id, "approval_id")
        _require_text(self.run_id, "run_id")
        _require_text(self.step_id, "step_id")
        _require_text(self.invocation_id, "invocation_id")
        _require_text(self.tool_name, "tool_name")
        _require_digest(self.invocation_identity_digest, "invocation_identity_digest")
        _require_digest(self.arguments_digest, "arguments_digest")
        if self.idempotency_key_digest is not None:
            _require_digest(self.idempotency_key_digest, "idempotency_key_digest")
        if self.resource_key_digest is not None:
            _require_digest(self.resource_key_digest, "resource_key_digest")
        if self.risk_level is not None:
            _require_text(self.risk_level, "risk_level")
        if not isinstance(self.risk_facts, tuple) or any(
            not isinstance(item, str) for item in self.risk_facts
        ):
            raise ValueError("risk_facts 必须是 str tuple（稳定有序）")
        _require_digest(self.invocation_binding_digest, "invocation_binding_digest")
        if self.requested_at.tzinfo is None or self.requested_at.utcoffset() != timedelta(0):
            raise ValueError("requested_at 必须是 timezone-aware UTC datetime")

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "tool_name": self.tool_name,
            "invocation_identity_digest": self.invocation_identity_digest,
            "arguments_digest": self.arguments_digest,
            "idempotency_key_digest": self.idempotency_key_digest,
            "resource_key_digest": self.resource_key_digest,
            "risk_level": self.risk_level,
            "risk_facts": self.risk_facts,
            "invocation_binding_digest": self.invocation_binding_digest,
            "requested_at": self.requested_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """一次人类决定；首次有效决定不可修改。"""

    approval_id: str
    decision: ApprovalDecisionValue
    decided_at: datetime
    actor_id_digest: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.approval_id, "approval_id")
        if not isinstance(self.decision, ApprovalDecisionValue):
            raise TypeError("decision 必须是 ApprovalDecisionValue")
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() != timedelta(0):
            raise ValueError("decided_at 必须是 timezone-aware UTC datetime")
        if self.actor_id_digest is not None:
            _require_digest(self.actor_id_digest, "actor_id_digest")


@dataclass(frozen=True, slots=True)
class ApprovalCommandResult:
    """安全 command result；不返回 raw invocation / tool result / raw args。"""

    run_id: str
    approval_id: str
    effective_status: ApprovalStatus
    idempotent: bool = False
    safe_error_code: str | None = None
    decided_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.safe_error_code is None


@runtime_checkable
class ApprovalStepStateBridge(Protocol):
    """Controller 只通过该 bridge 经 AgentStateMachine 修改 Step status。

    只有 AgentStateMachine 写 Step status；Controller 不直接 mutate 状态。
    """

    def running_to_waiting(self, step_id: str, occurred_at: datetime) -> None: ...
    def waiting_to_running(self, step_id: str, occurred_at: datetime) -> None: ...
    def waiting_to_failed_rejected(self, step_id: str, occurred_at: datetime) -> None: ...
    def waiting_to_cancelled(self, step_id: str, occurred_at: datetime) -> None: ...


class AgentStateApprovalBridge:
    """基于真实 AgentStateMachine + AgentState 的默认 Step status bridge。"""

    def __init__(self, state_machine: AgentStateMachine, agent_state: AgentState) -> None:
        if not isinstance(state_machine, AgentStateMachine):
            raise TypeError("state_machine 必须是 AgentStateMachine")
        if not isinstance(agent_state, AgentState):
            raise TypeError("agent_state 必须是 AgentState")
        self._state_machine = state_machine
        self._agent_state = agent_state

    def running_to_waiting(self, step_id: str, occurred_at: datetime) -> None:
        self._state_machine.apply_step_event(
            self._agent_state,
            StepStateEvent(
                StepEventType.APPROVAL_REQUESTED,
                step_id,
                occurred_at=occurred_at,
            ),
        )

    def waiting_to_running(self, step_id: str, occurred_at: datetime) -> None:
        self._state_machine.apply_step_event(
            self._agent_state,
            StepStateEvent(
                StepEventType.APPROVAL_APPROVED,
                step_id,
                occurred_at=occurred_at,
            ),
        )

    def waiting_to_failed_rejected(self, step_id: str, occurred_at: datetime) -> None:
        self._state_machine.apply_step_event(
            self._agent_state,
            StepStateEvent(
                StepEventType.APPROVAL_REJECTED,
                step_id,
                occurred_at=occurred_at,
                error_code="TOOL_APPROVAL_REJECTED",
                error_message="Tool 调用已被拒绝审批",
            ),
        )

    def waiting_to_cancelled(self, step_id: str, occurred_at: datetime) -> None:
        self._state_machine.apply_step_event(
            self._agent_state,
            StepStateEvent(
                StepEventType.CANCELLED,
                step_id,
                occurred_at=occurred_at,
                error_code="RUN_CANCELLED",
                error_message="运行取消使审批失效",
            ),
        )


@dataclass(slots=True)
class _ApprovalRecord:
    approval_id: str
    request: ApprovalRequest
    decision: ApprovalDecision | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    _wake_event: threading.Event = field(default_factory=threading.Event)


class ToolApprovalController:
    """Strictly run-scoped pending/decision/claim owner。

    线程模型：所有 mutation 经 owner Event Loop 上的单一 ``asyncio.Lock``
    串行化（create/decide/invalidate/claim 同一临界区）。非 owner loop 线程调用
    同步入口时会经 ``asyncio.run_coroutine_threadsafe`` 提交到 owner loop；owner
    loop 线程必须调用 async variant。Journal-first：evidence 可靠 publish 后状态
    才对 waiter effective。

    任何调用方都不能直接执行工具；``claim_execution()`` 成功后原 invocation 才
    可进入既有 resource authorization / ToolExecution 链。
    """

    def __init__(
        self,
        *,
        run_id: str,
        run_context: RunContext,
        state_bridge: ApprovalStepStateBridge,
        deadline_check: Callable[[], float | None],
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if not isinstance(run_context, RunContext):
            raise TypeError("run_context 必须是 RunContext")
        if not isinstance(state_bridge, ApprovalStepStateBridge):
            raise TypeError("state_bridge 必须实现 ApprovalStepStateBridge")
        if not callable(deadline_check):
            raise TypeError("deadline_check 必须可调用")
        self.run_id = run_id
        self._run_context = run_context
        self._state_bridge = state_bridge
        self._deadline_check = deadline_check
        self._closed = False
        self._approvals: dict[str, _ApprovalRecord] = {}
        self._active_by_invocation: dict[str, str] = {}
        self._step_emitter_resolver: Callable[[str], StepEventEmitter | None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = loop
        self._lock = asyncio.Lock()
        self._mutations = 0
        if self._loop is not None and (
            self._loop.is_closed() or not self._loop.is_running()
        ):
            raise ApprovalError(
                ApprovalCommandErrorCode.INVALID_STATE,
                "controller 绑定的 Event Loop 不可用",
            )

    # ------------------------------------------------------------------
    # Wiring helpers
    # ------------------------------------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("controller 已绑定其他 Event Loop")
        self._loop = loop

    def bind_step_emitter_resolver(
        self, resolver: Callable[[str], StepEventEmitter | None]
    ) -> None:
        if self._step_emitter_resolver is not None:
            raise RuntimeError("step emitter resolver 已绑定")
        self._step_emitter_resolver = resolver

    @property
    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Run finalization：标记关闭并清空内部登记。

        Coordinator 应先调用 ``invalidate`` 完成 cancel/deadline 失效发布与
        Step 状态收口，再调用本方法释放登记。
        """
        self._closed = True
        self._approvals.clear()
        self._active_by_invocation.clear()

    def pending_count(self) -> int:
        return sum(
            1
            for record in self._approvals.values()
            if record.status is ApprovalStatus.PENDING
        )

    def get(self, approval_id: str) -> ApprovalRequest | None:
        record = self._approvals.get(approval_id)
        return record.request if record is not None else None

    def status_of(self, approval_id: str) -> ApprovalStatus | None:
        record = self._approvals.get(approval_id)
        return record.status if record is not None else None

    # ------------------------------------------------------------------
    # Sync (off-loop) entry points
    # ------------------------------------------------------------------

    def request_approval(
        self,
        *,
        step_id: str,
        invocation: ToolInvocation,
        tool_name: str,
        risk_level: str | None,
        risk_facts: tuple[str, ...],
        event_emitter: StepEventEmitter | None,
        component: str = "tool_approval_controller",
    ) -> ApprovalRequest:
        loop = self._require_loop()
        coro = self._request_approval_coro(
            step_id=step_id,
            invocation=invocation,
            tool_name=tool_name,
            risk_level=risk_level,
            risk_facts=risk_facts,
            event_emitter=event_emitter,
            component=component,
        )
        return _run_on_loop(loop, coro)

    def decide(
        self,
        *,
        run_id: str,
        approval_id: str,
        invocation_id: str,
        decision: ApprovalDecisionValue,
        actor_id: str | None = None,
    ) -> ApprovalCommandResult:
        loop = self._require_loop()
        coro = self._decide_coro(
            run_id=run_id,
            approval_id=approval_id,
            invocation_id=invocation_id,
            decision=decision,
            actor_id=actor_id,
        )
        return _run_on_loop(loop, coro)

    def claim_execution(
        self,
        *,
        approval_id: str,
        invocation: ToolInvocation,
    ) -> ApprovalCommandResult:
        loop = self._require_loop()
        coro = self._claim_execution_coro(
            approval_id=approval_id, invocation=invocation
        )
        return _run_on_loop(loop, coro)

    def invalidate(
        self,
        *,
        status: ApprovalStatus,
        actor_id: str | None = None,
    ) -> tuple[ApprovalCommandResult, ...]:
        loop = self._require_loop()
        coro = self._invalidate_coro(status=status, actor_id=actor_id)
        return _run_on_loop(loop, coro)

    # ------------------------------------------------------------------
    # Async (owner-loop) entry points
    # ------------------------------------------------------------------

    async def request_approval_async(
        self,
        *,
        step_id: str,
        invocation: ToolInvocation,
        tool_name: str,
        risk_level: str | None,
        risk_facts: tuple[str, ...],
        event_emitter: StepEventEmitter | None,
        component: str = "tool_approval_controller",
    ) -> ApprovalRequest:
        return await self._request_approval_coro(
            step_id=step_id,
            invocation=invocation,
            tool_name=tool_name,
            risk_level=risk_level,
            risk_facts=risk_facts,
            event_emitter=event_emitter,
            component=component,
        )

    async def decide_async(
        self,
        *,
        run_id: str,
        approval_id: str,
        invocation_id: str,
        decision: ApprovalDecisionValue,
        actor_id: str | None = None,
    ) -> ApprovalCommandResult:
        return await self._decide_coro(
            run_id=run_id,
            approval_id=approval_id,
            invocation_id=invocation_id,
            decision=decision,
            actor_id=actor_id,
        )

    async def claim_execution_async(
        self,
        *,
        approval_id: str,
        invocation: ToolInvocation,
    ) -> ApprovalCommandResult:
        return await self._claim_execution_coro(
            approval_id=approval_id, invocation=invocation
        )

    async def invalidate_async(
        self,
        *,
        status: ApprovalStatus,
        actor_id: str | None = None,
    ) -> tuple[ApprovalCommandResult, ...]:
        return await self._invalidate_coro(status=status, actor_id=actor_id)

    # ------------------------------------------------------------------
    # Async internal implementations (all funneled to the async lock)
    # ------------------------------------------------------------------

    async def _request_approval_coro(
        self,
        *,
        step_id: str,
        invocation: ToolInvocation,
        tool_name: str,
        risk_level: str | None,
        risk_facts: tuple[str, ...],
        event_emitter: StepEventEmitter | None,
        component: str,
    ) -> ApprovalRequest:
        if not isinstance(invocation, ToolInvocation):
            raise TypeError("invocation 必须是 ToolInvocation")
        _require_text(step_id, "step_id")
        _require_text(tool_name, "tool_name")
        if not isinstance(risk_facts, tuple) or any(
            not isinstance(item, str) for item in risk_facts
        ):
            raise ValueError("risk_facts 必须是 str tuple")

        now = utc_now()
        invocation_identity_digest = safe_key_digest(invocation.invocation_id)
        assert invocation_identity_digest is not None
        idempotency_key_digest = safe_key_digest(invocation.idempotency_key)
        resource_key_digest = safe_key_digest(invocation.resource_key)
        binding_digest = compute_invocation_binding_digest(
            invocation_identity_digest=invocation_identity_digest,
            tool_name=invocation.tool_name,
            arguments_digest=invocation.arguments_digest,
            idempotency_key_digest=idempotency_key_digest,
            resource_key_digest=resource_key_digest,
            risk_level=risk_level,
            risk_facts=risk_facts,
        )
        approval_id = uuid4().hex
        request = ApprovalRequest(
            approval_id=approval_id,
            run_id=self.run_id,
            step_id=step_id,
            invocation_id=invocation.invocation_id,
            tool_name=tool_name,
            invocation_identity_digest=invocation_identity_digest,
            arguments_digest=invocation.arguments_digest,
            idempotency_key_digest=idempotency_key_digest,
            resource_key_digest=resource_key_digest,
            risk_level=risk_level,
            risk_facts=risk_facts,
            invocation_binding_digest=binding_digest,
            requested_at=now,
        )

        async with self._lock:
            self._raise_if_closed()
            active = self._active_by_invocation.get(invocation.invocation_id)
            if active is not None:
                raise ApprovalError(
                    ApprovalCommandErrorCode.DUPLICATE_INVOCATION,
                    "同一 invocation 已存在 active approval",
                )
            # Journal-first：先可靠发布 Requested evidence。
            if event_emitter is not None:
                payload = ToolApprovalRequestedPayload(
                    approval_id=approval_id,
                    tool_name=request.tool_name,
                    invocation_identity_digest=request.invocation_identity_digest,
                    arguments_digest=request.arguments_digest,
                    idempotency_key_digest=request.idempotency_key_digest,
                    risk_level=request.risk_level,
                    risk_facts=(
                        "|".join(sorted(request.risk_facts))
                        if request.risk_facts
                        else "NONE"
                    ),
                )
                try:
                    await event_emitter.emit(
                        RuntimeEventType.TOOL_APPROVAL_REQUESTED,
                        payload,
                        component=component,
                    )
                except (
                    JournalError,
                    EventEmitterSyncError,
                    RuntimeError,
                ) as exc:
                    raise ApprovalError(
                        ApprovalCommandErrorCode.PUBLICATION_FAILED,
                        "Tool Approval 请求证据无法可靠发布",
                    ) from exc
            try:
                self._state_bridge.running_to_waiting(step_id, utc_now())
            except Exception as exc:
                raise ApprovalError(
                    ApprovalCommandErrorCode.STATE_TRANSITION_FAILED,
                    "Step 无法进入 WAITING_FOR_APPROVAL",
                ) from exc
            record = _ApprovalRecord(approval_id=approval_id, request=request)
            self._approvals[approval_id] = record
            self._active_by_invocation[invocation.invocation_id] = approval_id
            self._mutations += 1
            return request

    async def _decide_coro(
        self,
        *,
        run_id: str,
        approval_id: str,
        invocation_id: str,
        decision: ApprovalDecisionValue,
        actor_id: str | None,
    ) -> ApprovalCommandResult:
        if not isinstance(decision, ApprovalDecisionValue):
            raise TypeError("decision 必须是 ApprovalDecisionValue")
        if run_id != self.run_id:
            return ApprovalCommandResult(
                run_id=run_id,
                approval_id=approval_id,
                effective_status=ApprovalStatus.PENDING,
                safe_error_code=ApprovalCommandErrorCode.UNKNOWN_RUN.value,
            )
        async with self._lock:
            self._raise_if_closed()
            record = self._approvals.get(approval_id)
            if record is None or record.request.invocation_id != invocation_id:
                return ApprovalCommandResult(
                    run_id=run_id,
                    approval_id=approval_id,
                    effective_status=ApprovalStatus.PENDING,
                    safe_error_code=ApprovalCommandErrorCode.UNKNOWN_APPROVAL.value,
                )
            if record.status is not ApprovalStatus.PENDING:
                return self._late_decision_result(record, decision)
            inactive_status = self._inactive_status()
            if inactive_status is not None:
                return ApprovalCommandResult(
                    run_id=run_id,
                    approval_id=approval_id,
                    effective_status=inactive_status,
                    safe_error_code=ApprovalCommandErrorCode.RUN_INACTIVE.value,
                )
            return await self._publish_and_decide(record, decision, actor_id)

    async def _publish_and_decide(
        self,
        record: _ApprovalRecord,
        decision: ApprovalDecisionValue,
        actor_id: str | None,
    ) -> ApprovalCommandResult:
        now = utc_now()
        actor_digest = compute_actor_id_digest(actor_id)
        request = record.request
        payload = ToolApprovalDecidedPayload(
            approval_id=request.approval_id,
            invocation_identity_digest=request.invocation_identity_digest,
            decision_status=(
                "APPROVED"
                if decision is ApprovalDecisionValue.APPROVE
                else "REJECTED"
            ),
            actor_id_digest=actor_digest,
        )
        emitter = self._resolve_emitter(request.step_id)
        if emitter is None:
            # 无 step emitter 时无法 Journal-first 发布；approve 不成为有效
            # execution authorization，Tool 不执行（fail closed）。
            return ApprovalCommandResult(
                run_id=self.run_id,
                approval_id=record.approval_id,
                effective_status=ApprovalStatus.PENDING,
                safe_error_code=ApprovalCommandErrorCode.JOURNAL_UNAVAILABLE.value,
            )
        try:
            await emitter.emit(
                RuntimeEventType.TOOL_APPROVAL_DECIDED,
                payload,
                component="tool_approval_controller",
            )
        except (JournalError, EventEmitterSyncError, RuntimeError) as exc:
            del exc
            return ApprovalCommandResult(
                run_id=self.run_id,
                approval_id=record.approval_id,
                effective_status=ApprovalStatus.PENDING,
                safe_error_code=ApprovalCommandErrorCode.PUBLICATION_FAILED.value,
            )
        return self._finalize_decision(record, decision, actor_digest, now)

    def _finalize_decision(
        self,
        record: _ApprovalRecord,
        decision: ApprovalDecisionValue,
        actor_digest: str | None,
        now: datetime,
    ) -> ApprovalCommandResult:
        # 在 async lock 内被调用；同一临界区第二次检查。
        if record.status is not ApprovalStatus.PENDING:
            return self._late_decision_result(record, decision)
        try:
            if decision is ApprovalDecisionValue.APPROVE:
                self._state_bridge.waiting_to_running(record.request.step_id, now)
                record.status = ApprovalStatus.APPROVED
            else:
                # REJECT：Step 立即经状态机 WAITING -> FAILED 收口（固定错误码
                # TOOL_APPROVAL_REJECTED）。等待 worker 随后醒来只会上抛
                # ToolApprovalRejectedError，由 executor 幂等处理已 FAILED 状态。
                self._state_bridge.waiting_to_failed_rejected(
                    record.request.step_id, now
                )
                record.status = ApprovalStatus.REJECTED
        except Exception as exc:
            del exc
            return ApprovalCommandResult(
                run_id=self.run_id,
                approval_id=record.approval_id,
                effective_status=record.status,
                safe_error_code=(
                    ApprovalCommandErrorCode.STATE_TRANSITION_FAILED.value
                ),
            )
        record.decision = ApprovalDecision(
            approval_id=record.approval_id,
            decision=decision,
            decided_at=now,
            actor_id_digest=actor_digest,
        )
        self._mutations += 1
        record._wake_event.set()
        return ApprovalCommandResult(
            run_id=self.run_id,
            approval_id=record.approval_id,
            effective_status=record.status,
            idempotent=False,
            decided_at=now,
        )

    async def _claim_execution_coro(
        self,
        *,
        approval_id: str,
        invocation: ToolInvocation,
    ) -> ApprovalCommandResult:
        if not isinstance(invocation, ToolInvocation):
            raise TypeError("invocation 必须是 ToolInvocation")
        async with self._lock:
            self._raise_if_closed()
            record = self._approvals.get(approval_id)
            if record is None:
                return ApprovalCommandResult(
                    run_id=self.run_id,
                    approval_id=approval_id,
                    effective_status=ApprovalStatus.PENDING,
                    safe_error_code=ApprovalCommandErrorCode.UNKNOWN_APPROVAL.value,
                )
            request = record.request
            if (
                invocation.invocation_id != request.invocation_id
                or invocation.tool_name != request.tool_name
                or invocation.arguments_digest != request.arguments_digest
                or safe_key_digest(invocation.idempotency_key)
                != request.idempotency_key_digest
                or safe_key_digest(invocation.resource_key)
                != request.resource_key_digest
            ):
                return ApprovalCommandResult(
                    run_id=self.run_id,
                    approval_id=approval_id,
                    effective_status=record.status,
                    safe_error_code=(
                        ApprovalCommandErrorCode.CLAIM_BINDING_MISMATCH.value
                    ),
                )
            if record.status is not ApprovalStatus.APPROVED:
                error_code = (
                    ApprovalCommandErrorCode.CLAIM_ALREADY_EXECUTED
                    if record.status is ApprovalStatus.EXECUTION_CLAIMED
                    else ApprovalCommandErrorCode.CLAIM_NOT_APPROVED
                )
                return ApprovalCommandResult(
                    run_id=self.run_id,
                    approval_id=approval_id,
                    effective_status=record.status,
                    safe_error_code=error_code.value,
                )
            inactive_status = self._inactive_status()
            if inactive_status is not None:
                record.status = inactive_status
                record._wake_event.set()
                return ApprovalCommandResult(
                    run_id=self.run_id,
                    approval_id=approval_id,
                    effective_status=inactive_status,
                    safe_error_code=(
                        ApprovalCommandErrorCode.RUN_INACTIVE.value
                    ),
                )
            record.status = ApprovalStatus.EXECUTION_CLAIMED
            self._mutations += 1
            record._wake_event.set()
            return ApprovalCommandResult(
                run_id=self.run_id,
                approval_id=approval_id,
                effective_status=ApprovalStatus.EXECUTION_CLAIMED,
            )

    async def _invalidate_coro(
        self,
        *,
        status: ApprovalStatus,
        actor_id: str | None,
    ) -> tuple[ApprovalCommandResult, ...]:
        if status not in {
            ApprovalStatus.INVALIDATED_CANCELLED,
            ApprovalStatus.INVALIDATED_TIMEOUT,
        }:
            raise ValueError("invalidate 只接受 INVALIDATED_CANCELLED/TIMEOUT")
        now = utc_now()
        actor_digest = compute_actor_id_digest(actor_id)
        results: list[ApprovalCommandResult] = []
        async with self._lock:
            for record in tuple(self._approvals.values()):
                if record.status in {
                    ApprovalStatus.EXECUTION_CLAIMED,
                    ApprovalStatus.REJECTED,
                    ApprovalStatus.INVALIDATED_CANCELLED,
                    ApprovalStatus.INVALIDATED_TIMEOUT,
                }:
                    continue
                payload = ToolApprovalDecidedPayload(
                    approval_id=record.request.approval_id,
                    invocation_identity_digest=(
                        record.request.invocation_identity_digest
                    ),
                    decision_status=status.value,
                    actor_id_digest=actor_digest,
                )
                emitter = self._resolve_emitter(record.request.step_id)
                if emitter is not None:
                    try:
                        await emitter.emit(
                            RuntimeEventType.TOOL_APPROVAL_DECIDED,
                            payload,
                            component="tool_approval_controller",
                            ignore_run_cancellation=True,
                        )
                    except (JournalError, EventEmitterSyncError, RuntimeError):
                        # 失效是 Run 终结路径；继续推进状态以唤醒 waiter。
                        pass
                try:
                    if status is ApprovalStatus.INVALIDATED_CANCELLED:
                        self._state_bridge.waiting_to_cancelled(
                            record.request.step_id, now
                        )
                    else:
                        # Existing Run/Step contract 把 deadline/timeout 投影为
                        # cancellation；不引入 StepStatus.TIMED_OUT。
                        self._state_bridge.waiting_to_cancelled(
                            record.request.step_id, now
                        )
                except Exception:
                    pass
                record.status = status
                self._mutations += 1
                record._wake_event.set()
                results.append(
                    ApprovalCommandResult(
                        run_id=self.run_id,
                        approval_id=record.approval_id,
                        effective_status=status,
                        decided_at=now,
                    )
                )
        return tuple(results)

    # ------------------------------------------------------------------
    # Waiter (原 worker 专用；非 mutation，poll + cancellation)
    # ------------------------------------------------------------------

    def wait_for_decision(
        self,
        *,
        approval_id: str,
        poll_seconds: float = 0.01,
    ) -> ApprovalCommandResult:
        """已 claim worker 专用：等待 decision signal 或 cancel/deadline。

        唤醒后调用方必须再调用 ``run_context.raise_if_inactive()``；cancel /
        deadline 会在此传播（不创建第二套 cancellation）。
        """
        if (
            isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or poll_seconds <= 0
        ):
            raise ValueError("poll_seconds 必须是正数")
        deadline_exhausted = False
        while True:
            self._run_context.raise_if_inactive()
            record = self._approvals.get(approval_id)
            if record is None:
                return ApprovalCommandResult(
                    run_id=self.run_id,
                    approval_id=approval_id,
                    effective_status=ApprovalStatus.INVALIDATED_CANCELLED,
                    safe_error_code=(
                        ApprovalCommandErrorCode.UNKNOWN_APPROVAL.value
                    ),
                )
            if record.status is not ApprovalStatus.PENDING:
                return ApprovalCommandResult(
                    run_id=self.run_id,
                    approval_id=approval_id,
                    effective_status=record.status,
                    decided_at=(
                        record.decision.decided_at
                        if record.decision is not None
                        else None
                    ),
                )
            remaining = self._deadline_check()
            if remaining is not None and remaining <= 0:
                if deadline_exhausted:
                    raise RunDeadlineExceededError(
                        "approval wait exceeded run deadline"
                    )
                deadline_exhausted = True
            record._wake_event.wait(poll_seconds)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
        if loop is None:
            raise ApprovalError(
                ApprovalCommandErrorCode.INVALID_STATE,
                "controller 尚未绑定 Event Loop",
            )
        if loop.is_closed() or not loop.is_running():
            raise ApprovalError(
                ApprovalCommandErrorCode.INVALID_STATE,
                "controller 所属 Event Loop 不可用",
            )
        return loop

    def _resolve_emitter(self, step_id: str) -> StepEventEmitter | None:
        resolver = self._step_emitter_resolver
        if resolver is None:
            return None
        return resolver(step_id)

    def _inactive_status(self) -> ApprovalStatus | None:
        if self._run_context.cancellation_token.is_cancelled():
            return ApprovalStatus.INVALIDATED_CANCELLED
        remaining = self._deadline_check()
        if remaining is not None and remaining <= 0:
            return ApprovalStatus.INVALIDATED_TIMEOUT
        return None

    def _late_decision_result(
        self,
        record: _ApprovalRecord,
        decision: ApprovalDecisionValue,
    ) -> ApprovalCommandResult:
        if record.status in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.EXECUTION_CLAIMED,
        } and decision is ApprovalDecisionValue.APPROVE:
            return ApprovalCommandResult(
                run_id=self.run_id,
                approval_id=record.approval_id,
                effective_status=record.status,
                idempotent=True,
                decided_at=(
                    record.decision.decided_at if record.decision else None
                ),
            )
        return ApprovalCommandResult(
            run_id=self.run_id,
            approval_id=record.approval_id,
            effective_status=record.status,
            safe_error_code=ApprovalCommandErrorCode.DECISION_CONFLICT.value,
            decided_at=(
                record.decision.decided_at if record.decision else None
            ),
        )

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise ApprovalError(
                ApprovalCommandErrorCode.CONTROLLER_CLOSED,
                "Approval Controller 已关闭",
            )


def _run_on_loop(loop: asyncio.AbstractEventLoop, coro) -> Any:
    """从非 owner loop 线程同步等待 owner loop 上的 coroutine 结果。"""
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        if running is loop:
            raise ApprovalError(
                ApprovalCommandErrorCode.INVALID_STATE,
                "Owner Event Loop 线程必须调用 async variant",
            )
        raise ApprovalError(
            ApprovalCommandErrorCode.INVALID_STATE,
            "其他 Event Loop 线程不能同步等待本 controller",
        )
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result()
    except RuntimeError as exc:
        if loop.is_closed() or not loop.is_running():
            raise ApprovalError(
                ApprovalCommandErrorCode.INVALID_STATE,
                "controller 所属 Event Loop 不可用",
            ) from exc
        raise


__all__ = [
    "ApprovalCommandErrorCode",
    "ApprovalCommandResult",
    "ApprovalDecision",
    "ApprovalDecisionValue",
    "ApprovalError",
    "ApprovalRequest",
    "ApprovalStatus",
    "ApprovalStepStateBridge",
    "ToolApprovalController",
    "ToolApprovalRejectedError",
    "compute_actor_id_digest",
    "compute_invocation_binding_digest",
]
