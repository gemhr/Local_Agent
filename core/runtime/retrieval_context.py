#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Retrieval Execution 的 Run、预算、事件与截止时间组合上下文。"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from core.runtime.budget import BudgetLedger
from core.runtime.context import RunContext, RunDeadlineExceededError
from core.runtime.event_emitter import StepEventEmitter
from core.runtime.fault_injection import FaultInjectionController
from core.runtime.retrieval_contract import RetrievalExecutionSpec, RetrievalStage


class RetrievalDeadlineExceededError(TimeoutError):
    """Retrieval 自身的单调截止时间已耗尽。"""


@dataclass(frozen=True, slots=True)
class RetrievalExecutionContext:
    """只检查运行条件，不修改 RunStatus、StepStatus 或取消原因。"""

    run_context: RunContext
    step_id: str
    budget_ledger: BudgetLedger
    event_emitter: StepEventEmitter | None
    retrieval_deadline_monotonic: float
    spec: RetrievalExecutionSpec
    fault_controller: FaultInjectionController | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.step_id, str) or not self.step_id.strip():
            raise ValueError("step_id 必须是非空字符串")
        if (
            isinstance(self.retrieval_deadline_monotonic, bool)
            or not isinstance(self.retrieval_deadline_monotonic, (int, float))
            or not math.isfinite(self.retrieval_deadline_monotonic)
        ):
            raise ValueError("retrieval_deadline_monotonic 必须是有限数")

    @classmethod
    def create(
        cls,
        *,
        run_context: RunContext,
        step_id: str,
        budget_ledger: BudgetLedger,
        event_emitter: StepEventEmitter | None,
        spec: RetrievalExecutionSpec,
        requested_timeout_seconds: float,
        fault_controller: FaultInjectionController | None = None,
    ) -> "RetrievalExecutionContext":
        timeout = min(spec.total_timeout_seconds, requested_timeout_seconds)
        run_remaining = run_context.remaining_seconds()
        if run_remaining is not None:
            timeout = min(timeout, run_remaining)
        return cls(
            run_context=run_context,
            step_id=step_id,
            budget_ledger=budget_ledger,
            event_emitter=event_emitter,
            retrieval_deadline_monotonic=time.monotonic() + max(0.0, timeout),
            spec=spec,
            fault_controller=fault_controller,
        )

    def raise_if_cancelled(self) -> None:
        """按 Run Cancellation、Run Deadline、Retrieval Deadline 的顺序检查。"""
        try:
            self.run_context.raise_if_inactive()
        except RunDeadlineExceededError:
            raise
        if self.remaining_seconds() <= 0:
            raise RetrievalDeadlineExceededError("retrieval deadline exceeded")

    def remaining_seconds(self) -> float:
        """返回 Retrieval 与 Run 两层截止时间的最小剩余秒数。"""
        remaining = max(0.0, self.retrieval_deadline_monotonic - time.monotonic())
        run_remaining = self.run_context.remaining_seconds()
        if run_remaining is not None:
            remaining = min(remaining, max(0.0, run_remaining))
        return remaining

    def before_stage(self, stage: RetrievalStage) -> float:
        """阶段开始前检查取消，并计算三层有效 Timeout。"""
        self.raise_if_cancelled()
        effective = min(self.spec.timeout_for(stage), self.remaining_seconds())
        if effective <= 0:
            raise RetrievalDeadlineExceededError(
                f"{stage.value} has no remaining execution time"
            )
        return effective


__all__ = [
    "RetrievalDeadlineExceededError",
    "RetrievalExecutionContext",
]
