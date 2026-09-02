#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Process-local active-run controls without retaining run business payloads."""

from __future__ import annotations

import asyncio
import inspect
import math
import threading
import time
from datetime import UTC, datetime
from typing import Awaitable, Callable

from core.runtime.cancellation import CancellationReason, CancellationSource
from core.runtime.state import AgentState


ForceAbortCallback = Callable[[CancellationReason], Awaitable[None] | None]


class ActiveRunControlHandle:
    """A safe control plane for one active run.

    The handle intentionally contains no prompt, output, tool/retrieval data,
    paths, API keys, or full scope reference.
    """

    __slots__ = (
        "run_id",
        "runtime_mode",
        "owner",
        "started_at",
        "cancellation_source",
        "_completed",
        "_force_abort_callback",
        "_active_step_count",
        "_approval_controller_resolver",
    )

    def __init__(
        self,
        *,
        run_id: str,
        runtime_mode: str,
        cancellation_source: CancellationSource,
        owner: str,
        started_at: datetime | None = None,
        force_abort_callback: ForceAbortCallback | None = None,
        active_step_count: Callable[[], int] | None = None,
        approval_controller_resolver: Callable[[], object | None] | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if not isinstance(runtime_mode, str) or not runtime_mode.strip():
            raise ValueError("runtime_mode must be a non-empty string")
        if not isinstance(cancellation_source, CancellationSource):
            raise TypeError("cancellation_source must be CancellationSource")
        self.run_id = run_id
        self.runtime_mode = runtime_mode
        self.owner = owner
        self.started_at = started_at or datetime.now(UTC)
        self.cancellation_source = cancellation_source
        self._completed = threading.Event()
        self._force_abort_callback = force_abort_callback
        self._active_step_count = active_step_count
        self._approval_controller_resolver = approval_controller_resolver

    @property
    def is_completed(self) -> bool:
        return self._completed.is_set()

    def request_cancel(self, reason: CancellationReason) -> bool:
        return self.cancellation_source.cancel(reason)

    async def wait_completed(self, timeout: float) -> bool:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout < 0
        ):
            raise ValueError("timeout must be a non-negative number")
        return await asyncio.to_thread(self._completed.wait, float(timeout))

    async def force_abort(self, reason: CancellationReason) -> None:
        self.request_cancel(reason)
        callback = self._force_abort_callback
        if callback is None:
            return
        result = callback(reason)
        if inspect.isawaitable(result):
            await result

    def bind_force_abort(self, callback: ForceAbortCallback) -> None:
        if self._force_abort_callback is not None:
            raise RuntimeError("force-abort callback is already bound")
        self._force_abort_callback = callback

    def bind_approval_controller_resolver(
        self, resolver: Callable[[], object | None]
    ) -> None:
        """绑定 run-scoped ToolApprovalController 解析器（不保存 business truth）。"""
        if self._approval_controller_resolver is not None:
            raise RuntimeError("approval controller resolver is already bound")
        self._approval_controller_resolver = resolver

    def approval_controller(self) -> object | None:
        """返回当前 run 的 ToolApprovalController 或 None。"""
        resolver = self._approval_controller_resolver
        if resolver is None:
            return None
        try:
            return resolver()
        except Exception:
            return None

    async def decide_tool_approval(
        self,
        *,
        approval_id: str,
        invocation_id: str,
        decision: object,
        actor_id: str | None = None,
    ) -> object:
        """WP1 typed domain command forwarding（供 WP2 transport 调用）。

        只转发给当前 run 的 controller，绝不保存 approval business payload。
        不存在 controller 时返回 not-found/inactive typed result。
        """
        from core.runtime.approval import (
            ApprovalCommandErrorCode,
            ApprovalCommandResult,
            ApprovalDecisionValue,
            ApprovalStatus,
            ToolApprovalController,
        )

        controller = self.approval_controller()
        if controller is None:
            return ApprovalCommandResult(
                run_id=self.run_id,
                approval_id=approval_id,
                effective_status=ApprovalStatus.PENDING,
                safe_error_code=ApprovalCommandErrorCode.UNKNOWN_APPROVAL.value,
            )
        if not isinstance(controller, ToolApprovalController):
            raise TypeError("run 的 approval controller 类型非法")
        if not isinstance(decision, ApprovalDecisionValue):
            raise TypeError("decision 必须是 ApprovalDecisionValue")
        if self.run_id != controller.run_id:
            return ApprovalCommandResult(
                run_id=self.run_id,
                approval_id=approval_id,
                effective_status=ApprovalStatus.PENDING,
                safe_error_code=ApprovalCommandErrorCode.UNKNOWN_RUN.value,
            )
        return await controller.decide_async(
            run_id=self.run_id,
            approval_id=approval_id,
            invocation_id=invocation_id,
            decision=decision,
            actor_id=actor_id,
        )

    async def approve_tool_approval(
        self,
        *,
        approval_id: str,
        invocation_id: str,
        actor_id: str | None = None,
    ) -> object:
        from core.runtime.approval import ApprovalDecisionValue

        return await self.decide_tool_approval(
            approval_id=approval_id,
            invocation_id=invocation_id,
            decision=ApprovalDecisionValue.APPROVE,
            actor_id=actor_id,
        )

    async def reject_tool_approval(
        self,
        *,
        approval_id: str,
        invocation_id: str,
        actor_id: str | None = None,
    ) -> object:
        from core.runtime.approval import ApprovalDecisionValue

        return await self.decide_tool_approval(
            approval_id=approval_id,
            invocation_id=invocation_id,
            decision=ApprovalDecisionValue.REJECT,
            actor_id=actor_id,
        )

    def mark_completed(self) -> None:
        self._completed.set()

    def active_step_count(self) -> int:
        callback = self._active_step_count
        if callback is None:
            return 0
        try:
            return max(0, int(callback()))
        except Exception:
            return 0

    def snapshot(self) -> dict[str, str | bool | None]:
        reason = self.cancellation_source.token.reason
        return {
            "run_id": self.run_id,
            "runtime_mode": self.runtime_mode,
            "owner": self.owner,
            "started_at": self.started_at.isoformat(),
            "completed": self.is_completed,
            "cancelled": self.cancellation_source.token.is_cancelled(),
            "cancellation_reason": (
                reason.value if isinstance(reason, CancellationReason) else reason
            ),
        }


class RunHandle(ActiveRunControlHandle):
    """Compatibility constructor for pre-Day-23 coordinator tests.

    Production assembly uses ``ActiveRunControlHandle``.  This adapter keeps
    the old positional API and state identity check at the coordinator
    boundary; the application services container never retains it.
    """

    __slots__ = ("agent_state",)

    def __init__(
        self,
        run_id: str,
        cancellation_source: CancellationSource,
        agent_state: AgentState,
        owner: str,
    ) -> None:
        self.agent_state = agent_state
        super().__init__(
            run_id=run_id,
            runtime_mode="LEGACY_COMPAT",
            cancellation_source=cancellation_source,
            owner=owner,
            active_step_count=lambda: len(agent_state.active_step_ids),
        )


class RunRegistry:
    """Thread-safe registry of safe active-run control handles."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._empty = threading.Condition(self._lock)
        self._handles: dict[str, ActiveRunControlHandle] = {}

    def register(
        self, handle: ActiveRunControlHandle
    ) -> ActiveRunControlHandle:
        if not isinstance(handle, ActiveRunControlHandle):
            raise TypeError("handle must be ActiveRunControlHandle")
        with self._lock:
            if handle.run_id in self._handles:
                raise ValueError("active run_id already registered")
            self._handles[handle.run_id] = handle
            return handle

    def get(self, run_id: str) -> ActiveRunControlHandle | None:
        with self._lock:
            return self._handles.get(run_id)

    def active_handles(self) -> tuple[ActiveRunControlHandle, ...]:
        with self._lock:
            return tuple(self._handles.values())

    def snapshot(self, run_id: str | None = None):
        with self._lock:
            if run_id is not None:
                handle = self._handles.get(run_id)
                return handle.snapshot() if handle else None
            return {key: value.snapshot() for key, value in self._handles.items()}

    def observability_snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "active_runs": len(self._handles),
                "active_steps": sum(
                    handle.active_step_count() for handle in self._handles.values()
                ),
            }

    def cancel(self, run_id: str, reason: CancellationReason) -> bool | None:
        handle = self.get(run_id)
        return None if handle is None else handle.request_cancel(reason)

    async def decide_tool_approval(
        self,
        run_id: str,
        approval_id: str,
        invocation_id: str,
        decision: object,
        actor_id: str | None = None,
    ) -> object:
        """Registry 级 typed command forwarding；不存在 handle 时返回 inactive。"""
        handle = self.get(run_id)
        if handle is None:
            from core.runtime.approval import (
                ApprovalCommandErrorCode,
                ApprovalCommandResult,
                ApprovalStatus,
            )

            return ApprovalCommandResult(
                run_id=run_id,
                approval_id=approval_id,
                effective_status=ApprovalStatus.PENDING,
                safe_error_code=ApprovalCommandErrorCode.UNKNOWN_APPROVAL.value,
            )
        return await handle.decide_tool_approval(
            approval_id=approval_id,
            invocation_id=invocation_id,
            decision=decision,
            actor_id=actor_id,
        )

    async def approve_tool_approval(
        self,
        run_id: str,
        approval_id: str,
        invocation_id: str,
        actor_id: str | None = None,
    ) -> object:
        from core.runtime.approval import ApprovalDecisionValue

        return await self.decide_tool_approval(
            run_id,
            approval_id,
            invocation_id,
            ApprovalDecisionValue.APPROVE,
            actor_id=actor_id,
        )

    async def reject_tool_approval(
        self,
        run_id: str,
        approval_id: str,
        invocation_id: str,
        actor_id: str | None = None,
    ) -> object:
        from core.runtime.approval import ApprovalDecisionValue

        return await self.decide_tool_approval(
            run_id,
            approval_id,
            invocation_id,
            ApprovalDecisionValue.REJECT,
            actor_id=actor_id,
        )

    def unregister(self, run_id: str) -> bool:
        with self._lock:
            handle = self._handles.pop(run_id, None)
            if handle is None:
                return False
            handle.mark_completed()
            if not self._handles:
                self._empty.notify_all()
            return True

    def cancel_all(self, reason: CancellationReason) -> tuple[str, ...]:
        with self._lock:
            handles = tuple(self._handles.items())
        return tuple(
            run_id
            for run_id, handle in handles
            if handle.request_cancel(reason)
        )

    def wait_until_empty(self, timeout_seconds: float) -> tuple[str, ...]:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds < 0
        ):
            raise ValueError("timeout_seconds must not be negative")
        deadline = time.monotonic() + float(timeout_seconds)
        with self._empty:
            while self._handles:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._empty.wait(remaining)
            return tuple(sorted(self._handles))


process_run_registry = RunRegistry()


__all__ = [
    "ActiveRunControlHandle",
    "RunHandle",
    "RunRegistry",
    "process_run_registry",
]
