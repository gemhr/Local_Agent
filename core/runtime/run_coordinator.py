#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单次 Run 的 Parent Runtime 与统一生命周期所有者。"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.runtime.budget import BudgetExceededError, BudgetLedger, BudgetSnapshot
from core.runtime.cancellation import CancellationReason, RunCancelledError
from core.runtime.context import RunContext, RunDeadlineExceededError
from core.runtime.parallel_execution import (
    ParallelExecutionInfrastructureError,
    ParallelExecutionPolicy,
    ParallelExecutor,
    StepConcurrencySpec,
    StepExecutionDriver,
    StepExecutionMode,
)
from core.runtime.plan_graph import PlanGraphValidationError, PlanGraphValidator
from core.runtime.planning import Plan
from core.runtime.run_registry import RunHandle, RunRegistry
from core.runtime.scheduler import SchedulerError, SchedulerSnapshot, SerialScheduler
from core.runtime.state import AgentState, RunStatus, StepStatus, StopReason
from core.runtime.state_machine import (
    AgentStateMachine,
    RunEventType,
    RunStateEvent,
    StepEventType,
    StepStateEvent,
)
from core.runtime.event_channel import EventChannelClosedError
from core.runtime.event_emitter import RunEventEmitter
from core.runtime.events import (
    BudgetExhaustedPayload,
    CancellationPayload,
    ErrorPayload,
    RunCompletedPayload,
    RunStartedPayload,
    RuntimeEventType,
    StepCompletedPayload,
    TimeoutPayload,
)


_TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
)
_CANCELLED_STOP_REASONS = frozenset(
    {
        StopReason.USER_CANCELLED,
        StopReason.CLIENT_DISCONNECTED,
        StopReason.SYSTEM_SHUTDOWN,
    }
)
_SAFE_MESSAGES = {
    StopReason.COMPLETED: "运行已成功完成",
    StopReason.USER_CANCELLED: "运行已由用户取消",
    StopReason.CLIENT_DISCONNECTED: "客户端连接已断开",
    StopReason.SYSTEM_SHUTDOWN: "系统关闭已取消运行",
    StopReason.DEADLINE_EXCEEDED: "运行已超过截止时间",
    StopReason.BUDGET_EXHAUSTED: "运行预算已耗尽",
    StopReason.NO_ACTION: "当前计划没有可继续执行的步骤",
    StopReason.UNHANDLED_ERROR: "运行未能完成",
}


class RunCoordinatorError(RuntimeError):
    """仅表示 Coordinator 装配或所有权不变量错误。"""

    def __init__(self, error_code: str, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code})")


@dataclass(frozen=True, slots=True)
class RunFinalizationDecision:
    """不修改 State 的不可变 Run 终态决策。"""

    status: RunStatus
    stop_reason: StopReason
    error_code: str | None
    safe_message: str

    def __post_init__(self) -> None:
        if self.status not in _TERMINAL_RUN_STATUSES:
            raise ValueError("终态决策必须使用终态 RunStatus")
        if self.status == RunStatus.SUCCEEDED:
            if self.stop_reason != StopReason.COMPLETED or self.error_code is not None:
                raise ValueError("成功决策必须使用 COMPLETED 且不得包含 error_code")
        elif self.status == RunStatus.CANCELLED:
            if self.stop_reason not in _CANCELLED_STOP_REASONS or not self.error_code:
                raise ValueError("取消决策必须使用合法取消原因和 error_code")
        elif self.stop_reason in _CANCELLED_STOP_REASONS | {StopReason.COMPLETED}:
            raise ValueError("失败决策不得使用完成或取消原因")
        if not isinstance(self.safe_message, str) or not self.safe_message.strip():
            raise ValueError("safe_message 必须是非空安全说明")


@dataclass(frozen=True, slots=True)
class RunCoordinatorResult:
    """Coordinator 返回的安全结构化结果，不保存业务正文或原始异常。"""

    run_id: str
    plan_id: str
    status: RunStatus
    stop_reason: StopReason
    succeeded_step_ids: tuple[str, ...]
    failed_step_ids: tuple[str, ...]
    cancelled_step_ids: tuple[str, ...]
    blocked_step_ids: tuple[str, ...]
    budget_snapshot: BudgetSnapshot
    cleanup_error_codes: tuple[str, ...]
    error_code: str | None = None
    safe_message: str = ""


CleanupCallback = Callable[[], Any]


class RunCoordinator:
    """绑定一个 Run 和一个不可变 Plan 的单次使用 Parent Runtime。"""

    def __init__(
        self,
        *,
        run_context: RunContext,
        plan: Plan,
        agent_state: AgentState,
        budget_ledger: BudgetLedger,
        run_handle: RunHandle,
        scheduler: SerialScheduler,
        executor: ParallelExecutor,
        run_registry: RunRegistry,
        policy: ParallelExecutionPolicy,
        state_machine: AgentStateMachine | None = None,
        event_emitter: RunEventEmitter | None = None,
    ) -> None:
        self.run_context = run_context
        self.plan = plan
        self.agent_state = agent_state
        self.budget_ledger = budget_ledger
        self.run_handle = run_handle
        self.scheduler = scheduler
        self.executor = executor
        self.run_registry = run_registry
        self.policy = policy
        self.state_machine = state_machine or AgentStateMachine()
        self.event_emitter = event_emitter

        self._start_lock = threading.Lock()
        self._started = False
        self._started_monotonic: float | None = None
        self._budget_dimension = "unknown"
        self._finalize_lock = threading.Lock()
        self._finalization_decision: RunFinalizationDecision | None = None
        self._cleanup_lock = threading.Lock()
        self._cleanup_callbacks: list[CleanupCallback] = []
        self._deadline_watcher: threading.Timer | None = None
        self._executor_task: asyncio.Task[Any] | None = None

    def add_cleanup_callback(self, callback: CleanupCallback) -> None:
        """在执行前登记 Run 级清理回调；清理时按逆序运行。"""
        if not callable(callback):
            raise TypeError("cleanup callback 必须可调用")
        with self._cleanup_lock:
            if self._started:
                raise RunCoordinatorError(
                    "COORDINATOR_ALREADY_STARTED",
                    "Coordinator 启动后不能再登记清理回调",
                )
            self._cleanup_callbacks.append(callback)

    async def execute(
        self,
        *,
        driver: StepExecutionDriver,
        execution_mode: StepExecutionMode = StepExecutionMode.ASYNC,
        concurrency_specs: dict[str, StepConcurrencySpec] | None = None,
    ) -> RunCoordinatorResult:
        """执行多批 Scheduler → Executor 循环并安全收口整个 Run。"""
        self._mark_started_once()
        self._validate_ownership()

        registered = False
        cleanup_error_codes: list[str] = []
        decision: RunFinalizationDecision | None = None
        try:
            try:
                self.run_registry.register(self.run_handle)
            except Exception as exc:
                raise RunCoordinatorError(
                    "COORDINATOR_REGISTRATION_FAILED",
                    "RunHandle 注册失败",
                ) from exc
            registered = True

            self.state_machine.apply_run_event(
                self.agent_state, RunStateEvent(RunEventType.STARTED)
            )
            await self._emit_run_started()
            self._start_deadline_watcher()
            PlanGraphValidator.validate(self.plan)
            self.scheduler.prepare(
                self.plan,
                self.agent_state,
                self._event_time(),
                self.policy.max_concurrency,
            )
            decision = await self._execute_batches(
                driver=driver,
                execution_mode=execution_mode,
                concurrency_specs=concurrency_specs,
            )
        except BudgetExceededError as exc:
            self._budget_dimension = exc.dimension
            decision = self._budget_decision()
        except RunDeadlineExceededError:
            decision = self._deadline_decision()
        except RunCancelledError:
            decision = self._cancellation_decision()
        except asyncio.CancelledError:
            decision = self._cancellation_decision(
                fallback_code="COORDINATOR_TASK_CANCELLED"
            )
        except GeneratorExit:
            decision = self._cancellation_decision(
                fallback_code="COORDINATOR_GENERATOR_CLOSED"
            )
        except (
            ParallelExecutionInfrastructureError,
            SchedulerError,
            PlanGraphValidationError,
        ) as exc:
            decision = self._infrastructure_decision(
                getattr(exc, "error_code", "COORDINATOR_INFRASTRUCTURE_ERROR")
            )
        except RunCoordinatorError:
            raise
        except Exception:
            decision = self._infrastructure_decision(
                "COORDINATOR_INFRASTRUCTURE_ERROR"
            )
        finally:
            await self._stop_executor_task(cleanup_error_codes)
            if decision is None:
                decision = self._infrastructure_decision(
                    "COORDINATOR_FINALIZATION_REQUIRED"
                )
            await self._settle_active_steps(decision, cleanup_error_codes)
            decision = self._finalize_once(decision)
            await self._emit_terminal_events(decision)
            self._stop_deadline_watcher(cleanup_error_codes)
            await self._run_cleanup_callbacks(cleanup_error_codes)
            budget_snapshot = self._snapshot_budget(cleanup_error_codes)
            if registered:
                self._unregister(cleanup_error_codes)

        return self._build_result(
            decision=decision,
            budget_snapshot=budget_snapshot,
            cleanup_error_codes=cleanup_error_codes,
        )

    async def _execute_batches(
        self,
        *,
        driver: StepExecutionDriver,
        execution_mode: StepExecutionMode,
        concurrency_specs: dict[str, StepConcurrencySpec] | None,
    ) -> RunFinalizationDecision:
        while True:
            self.run_context.raise_if_inactive()
            snapshot = self.scheduler.evaluate(
                self.plan, self.agent_state, self.policy.max_concurrency
            )
            decision = self._decision_from_snapshot(snapshot)
            if decision is not None:
                return decision

            if not snapshot.claimable_step_ids:
                if snapshot.running_step_ids:
                    return self._infrastructure_decision(
                        "COORDINATOR_ORPHANED_ACTIVE_STEP"
                    )
                return self._no_action_decision()

            self._executor_task = asyncio.create_task(
                self.executor.execute_ready(
                    scheduler=self.scheduler,
                    plan=self.plan,
                    state=self.agent_state,
                    occurred_at=self._event_time(),
                    run_context=self.run_context,
                    driver=driver,
                    policy=self.policy,
                    execution_mode=execution_mode,
                    concurrency_specs=concurrency_specs,
                )
            )
            try:
                # 单批报告只描述已 Claim Step；Run 终态始终由下一轮全 Plan 快照决定。
                await self._executor_task
            finally:
                if self._executor_task.done():
                    self._executor_task = None

    def _decision_from_snapshot(
        self, snapshot: SchedulerSnapshot
    ) -> RunFinalizationDecision | None:
        if self.agent_state.status in _TERMINAL_RUN_STATUSES:
            return self._decision_from_terminal_state()
        token_decision = self._token_decision()
        if token_decision is not None:
            return token_decision
        if snapshot.is_complete and not self.agent_state.active_step_ids:
            return RunFinalizationDecision(
                RunStatus.SUCCEEDED,
                StopReason.COMPLETED,
                None,
                _SAFE_MESSAGES[StopReason.COMPLETED],
            )
        if snapshot.ready_step_ids or snapshot.running_step_ids:
            return None
        if any(
            step.status == StepStatus.FAILED
            for step in self.agent_state.steps.values()
        ):
            return RunFinalizationDecision(
                RunStatus.FAILED,
                StopReason.UNHANDLED_ERROR,
                "STEP_EXECUTION_FAILED",
                "一个或多个步骤执行失败",
            )
        if snapshot.has_unresolved_pending or snapshot.blocked_step_ids:
            return self._no_action_decision()
        return None

    def _mark_started_once(self) -> None:
        with self._start_lock:
            if self._started:
                raise RunCoordinatorError(
                    "COORDINATOR_ALREADY_EXECUTED",
                    "同一 Coordinator 不允许执行两次",
                )
            self._started = True
            self._started_monotonic = time.monotonic()

    def _validate_ownership(self) -> None:
        run_id = self.run_context.run_id
        if self.agent_state.run_id != run_id or self.run_handle.run_id != run_id:
            raise RunCoordinatorError(
                "COORDINATOR_RUN_ID_MISMATCH",
                "Context、State 与 Handle 的 run_id 必须一致",
            )
        if self.run_handle.agent_state is not self.agent_state:
            raise RunCoordinatorError(
                "COORDINATOR_STATE_OWNERSHIP_MISMATCH",
                "RunHandle 必须引用 Coordinator 持有的 AgentState",
            )
        if (
            self.run_handle.cancellation_source.token
            is not self.run_context.cancellation_token
        ):
            raise RunCoordinatorError(
                "COORDINATOR_CANCELLATION_OWNERSHIP_MISMATCH",
                "RunHandle 与 RunContext 必须共享同一取消源",
            )
        if self.run_context.budget_ledger is not self.budget_ledger:
            raise RunCoordinatorError(
                "COORDINATOR_BUDGET_OWNERSHIP_MISMATCH",
                "RunContext 与 Coordinator 必须共享同一 BudgetLedger",
            )
        if self.agent_state.status != RunStatus.CREATED:
            raise RunCoordinatorError(
                "COORDINATOR_STATE_NOT_CREATED",
                "Coordinator 只能接管 CREATED 状态的 Run",
            )

    def _token_decision(self) -> RunFinalizationDecision | None:
        reason = self.run_context.cancellation_token.reason
        if reason is None:
            return None
        try:
            parsed = (
                reason
                if isinstance(reason, CancellationReason)
                else CancellationReason(str(reason))
            )
        except ValueError:
            return self._infrastructure_decision("UNKNOWN_CANCELLATION_REASON")
        if parsed == CancellationReason.DEADLINE_EXCEEDED:
            return RunFinalizationDecision(
                RunStatus.FAILED,
                StopReason.DEADLINE_EXCEEDED,
                "DEADLINE_EXCEEDED",
                _SAFE_MESSAGES[StopReason.DEADLINE_EXCEEDED],
            )
        stop_reason = StopReason(parsed.value)
        return RunFinalizationDecision(
            RunStatus.CANCELLED,
            stop_reason,
            parsed.value,
            _SAFE_MESSAGES[stop_reason],
        )

    def _cancellation_decision(
        self, fallback_code: str = "COORDINATOR_CANCELLED"
    ) -> RunFinalizationDecision:
        return self._token_decision() or self._infrastructure_decision(fallback_code)

    @staticmethod
    def _budget_decision() -> RunFinalizationDecision:
        return RunFinalizationDecision(
            RunStatus.FAILED,
            StopReason.BUDGET_EXHAUSTED,
            "BUDGET_EXHAUSTED",
            _SAFE_MESSAGES[StopReason.BUDGET_EXHAUSTED],
        )

    @staticmethod
    def _deadline_decision() -> RunFinalizationDecision:
        return RunFinalizationDecision(
            RunStatus.FAILED,
            StopReason.DEADLINE_EXCEEDED,
            "DEADLINE_EXCEEDED",
            _SAFE_MESSAGES[StopReason.DEADLINE_EXCEEDED],
        )

    @staticmethod
    def _infrastructure_decision(error_code: str) -> RunFinalizationDecision:
        return RunFinalizationDecision(
            RunStatus.FAILED,
            StopReason.UNHANDLED_ERROR,
            error_code,
            _SAFE_MESSAGES[StopReason.UNHANDLED_ERROR],
        )

    @staticmethod
    def _no_action_decision() -> RunFinalizationDecision:
        return RunFinalizationDecision(
            RunStatus.FAILED,
            StopReason.NO_ACTION,
            "NO_ACTION",
            _SAFE_MESSAGES[StopReason.NO_ACTION],
        )

    def _decision_from_terminal_state(self) -> RunFinalizationDecision:
        state = self.agent_state
        stop_reason = state.stop_reason or StopReason.UNHANDLED_ERROR
        return RunFinalizationDecision(
            status=state.status,
            stop_reason=stop_reason,
            error_code=state.error_code,
            safe_message=_SAFE_MESSAGES.get(stop_reason, "运行已结束"),
        )

    async def _settle_active_steps(
        self,
        decision: RunFinalizationDecision,
        cleanup_error_codes: list[str],
    ) -> None:
        if self.agent_state.status in _TERMINAL_RUN_STATUSES:
            return
        for step_id in tuple(sorted(self.agent_state.active_step_ids)):
            try:
                self.state_machine.apply_step_event(
                    self.agent_state,
                    StepStateEvent(
                        StepEventType.CANCELLED,
                        step_id,
                        occurred_at=self._event_time(),
                        error_code="RUN_TERMINATING",
                        error_message="运行终结前取消仍在执行的步骤",
                    ),
                )
                if self.event_emitter is not None:
                    step_emitter = self.event_emitter.for_step(step_id)
                    if not step_emitter.is_closed:
                        try:
                            step = self.agent_state.steps[step_id]
                            duration_ms = (
                                max(
                                    0,
                                    int(
                                        (
                                            step.ended_at - step.started_at
                                        ).total_seconds()
                                        * 1000
                                    ),
                                )
                                if step.started_at is not None
                                and step.ended_at is not None
                                else 0
                            )
                            await step_emitter.emit(
                                RuntimeEventType.STEP_COMPLETED,
                                StepCompletedPayload(
                                    StepStatus.CANCELLED.value,
                                    "RUN_TERMINATING",
                                    duration_ms=duration_ms,
                                ),
                                component="run_coordinator",
                                close=True,
                                ignore_run_cancellation=True,
                            )
                        except (EventChannelClosedError, RuntimeError):
                            pass
            except Exception:
                cleanup_error_codes.append("ACTIVE_STEP_CLEANUP_FAILED")
        if self.agent_state.active_step_ids:
            cleanup_error_codes.append("ACTIVE_STEP_LEAK")
            decision = self._infrastructure_decision("ACTIVE_STEP_LEAK")
        _ = decision

    async def _emit_run_started(self) -> None:
        if self.event_emitter is None:
            return
        try:
            await self.event_emitter.emit(
                RuntimeEventType.RUN_STARTED,
                RunStartedPayload(self.agent_state.status.value),
                component="run_coordinator",
            )
        except (EventChannelClosedError, RuntimeError):
            return

    async def _emit_terminal_events(
        self, decision: RunFinalizationDecision
    ) -> None:
        """Run 状态提交后按 ERROR/CANCELLATION → RUN_COMPLETED 发布。"""
        if self.event_emitter is None:
            return
        try:
            if decision.stop_reason is StopReason.BUDGET_EXHAUSTED:
                await self.event_emitter.emit(
                    RuntimeEventType.BUDGET_EXHAUSTED,
                    BudgetExhaustedPayload(
                        component="run", dimension=self._budget_dimension
                    ),
                    component="run_coordinator",
                    ignore_run_cancellation=True,
                )
            elif decision.stop_reason is StopReason.DEADLINE_EXCEEDED:
                await self.event_emitter.emit(
                    RuntimeEventType.TIMEOUT,
                    TimeoutPayload(component="run"),
                    component="run_coordinator",
                    ignore_run_cancellation=True,
                )
            if decision.status == RunStatus.CANCELLED:
                await self.event_emitter.emit(
                    RuntimeEventType.CANCELLATION,
                    CancellationPayload(
                        reason=decision.stop_reason.value,
                        component="run",
                    ),
                    component="run_coordinator",
                    ignore_run_cancellation=True,
                )
            elif (
                decision.status == RunStatus.FAILED
                and decision.stop_reason
                not in {
                    StopReason.BUDGET_EXHAUSTED,
                    StopReason.DEADLINE_EXCEEDED,
                }
            ):
                await self.event_emitter.emit(
                    RuntimeEventType.ERROR,
                    ErrorPayload(
                        safe_error_code=decision.error_code
                        or "COORDINATOR_FAILED",
                        safe_message=decision.safe_message,
                        component="run_coordinator",
                        fatal=True,
                    ),
                    component="run_coordinator",
                    ignore_run_cancellation=True,
                )
            await self.event_emitter.emit(
                RuntimeEventType.RUN_COMPLETED,
                RunCompletedPayload(
                    status=self.agent_state.status.value,
                    stop_reason=(
                        self.agent_state.stop_reason.value
                        if self.agent_state.stop_reason is not None
                        else decision.stop_reason.value
                    ),
                    duration_ms=(
                        max(
                            0,
                            int(
                                (
                                    time.monotonic() - self._started_monotonic
                                )
                                * 1000
                            ),
                        )
                        if self._started_monotonic is not None
                        else 0
                    ),
                ),
                component="run_coordinator",
                ignore_run_cancellation=True,
            )
        except (EventChannelClosedError, RuntimeError):
            # Client 已断开时不继续向废弃 Transport 投递 terminal event。
            return

    def _finalize_once(
        self, decision: RunFinalizationDecision
    ) -> RunFinalizationDecision:
        """同步 first-wins 终结；锁内不执行任何 await。"""
        with self._finalize_lock:
            if self._finalization_decision is not None:
                return self._finalization_decision
            if self.agent_state.status in _TERMINAL_RUN_STATUSES:
                self._finalization_decision = self._decision_from_terminal_state()
                return self._finalization_decision
            if self.agent_state.active_step_ids:
                decision = self._infrastructure_decision("ACTIVE_STEP_LEAK")
            event = self._run_event_for_decision(decision)
            self.state_machine.apply_run_event(self.agent_state, event)
            self._finalization_decision = decision
            return decision

    def _run_event_for_decision(
        self, decision: RunFinalizationDecision
    ) -> RunStateEvent:
        occurred_at = self._event_time()
        if decision.status == RunStatus.SUCCEEDED:
            return RunStateEvent(
                RunEventType.COMPLETED,
                occurred_at=occurred_at,
                stop_reason=StopReason.COMPLETED,
            )
        if decision.status == RunStatus.CANCELLED:
            return RunStateEvent(
                RunEventType.CANCELLED,
                occurred_at=occurred_at,
                stop_reason=decision.stop_reason,
                error_code=decision.error_code,
                error_message=decision.safe_message,
            )
        event_type = {
            StopReason.DEADLINE_EXCEEDED: RunEventType.DEADLINE_EXCEEDED,
            StopReason.BUDGET_EXHAUSTED: RunEventType.BUDGET_EXHAUSTED,
            StopReason.NO_ACTION: RunEventType.NO_ACTION,
        }.get(decision.stop_reason, RunEventType.FAILED)
        return RunStateEvent(
            event_type,
            occurred_at=occurred_at,
            stop_reason=decision.stop_reason,
            error_code=decision.error_code,
            error_message=decision.safe_message,
        )

    def _start_deadline_watcher(self) -> None:
        remaining = self.run_context.remaining_seconds()
        if remaining is None:
            return
        self._deadline_watcher = threading.Timer(
            remaining,
            self.run_handle.cancellation_source.cancel,
            args=(CancellationReason.DEADLINE_EXCEEDED,),
        )
        self._deadline_watcher.daemon = True
        self._deadline_watcher.start()

    def _stop_deadline_watcher(self, cleanup_error_codes: list[str]) -> None:
        watcher = self._deadline_watcher
        self._deadline_watcher = None
        if watcher is None:
            return
        try:
            watcher.cancel()
        except Exception:
            cleanup_error_codes.append("DEADLINE_WATCHER_CLEANUP_FAILED")

    async def _stop_executor_task(
        self, cleanup_error_codes: list[str]
    ) -> None:
        task = self._executor_task
        self._executor_task = None
        if task is None or task.done():
            return
        try:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except (Exception, asyncio.CancelledError):
            cleanup_error_codes.append("EXECUTOR_TASK_CLEANUP_FAILED")

    async def _run_cleanup_callbacks(
        self, cleanup_error_codes: list[str]
    ) -> None:
        with self._cleanup_lock:
            callbacks = tuple(reversed(self._cleanup_callbacks))
            self._cleanup_callbacks.clear()
        for callback in callbacks:
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except (Exception, asyncio.CancelledError):
                cleanup_error_codes.append("RUN_CLEANUP_CALLBACK_FAILED")

    def _snapshot_budget(
        self, cleanup_error_codes: list[str]
    ) -> BudgetSnapshot:
        try:
            snapshot = self.budget_ledger.snapshot()
        except Exception as exc:
            raise RunCoordinatorError(
                "BUDGET_SNAPSHOT_FAILED", "预算快照生成失败"
            ) from exc
        if snapshot.active_reservation_count:
            cleanup_error_codes.append("BUDGET_RESERVATION_LEAK")
        return snapshot

    def _unregister(self, cleanup_error_codes: list[str]) -> None:
        try:
            if not self.run_registry.unregister(self.run_context.run_id):
                cleanup_error_codes.append("REGISTRY_HANDLE_MISSING")
        except Exception:
            cleanup_error_codes.append("REGISTRY_UNREGISTER_FAILED")

    def _build_result(
        self,
        *,
        decision: RunFinalizationDecision,
        budget_snapshot: BudgetSnapshot,
        cleanup_error_codes: list[str],
    ) -> RunCoordinatorResult:
        ordered_ids = tuple(step.step_id for step in self.plan.steps)

        def ids_for(status: StepStatus) -> tuple[str, ...]:
            return tuple(
                step_id
                for step_id in ordered_ids
                if self.agent_state.steps.get(step_id) is not None
                and self.agent_state.steps[step_id].status == status
            )

        return RunCoordinatorResult(
            run_id=self.run_context.run_id,
            plan_id=self.plan.plan_id,
            status=self.agent_state.status,
            stop_reason=self.agent_state.stop_reason or decision.stop_reason,
            succeeded_step_ids=ids_for(StepStatus.SUCCEEDED),
            failed_step_ids=ids_for(StepStatus.FAILED),
            cancelled_step_ids=ids_for(StepStatus.CANCELLED),
            blocked_step_ids=ids_for(StepStatus.BLOCKED),
            budget_snapshot=budget_snapshot,
            cleanup_error_codes=tuple(cleanup_error_codes),
            error_code=self.agent_state.error_code,
            safe_message=decision.safe_message,
        )

    def _event_time(self) -> datetime:
        return max(datetime.now(UTC), self.agent_state.updated_at)
