#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""独立于 Legacy AgentLoop 的最小并行 Step 执行边界。"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Mapping, Protocol

from core.runtime.budget import BudgetExceededError
from core.runtime.context import RunContext
from core.runtime.context import RunDeadlineExceededError
from core.runtime.cancellation import RunCancelledError
from core.runtime.blocking_executor import (
    BlockingTaskKind,
    BoundedBlockingExecutor,
)
from core.runtime.scheduler import SerialScheduler, StepClaim
from core.runtime.planning import ExecutionKind, OutputPolicy, Plan
from core.runtime.state import AgentState, StepStatus
from core.runtime.state_machine import AgentStateMachine, StepEventType, StepStateEvent
from core.runtime.event_channel import EventChannelClosedError
from core.runtime.event_emitter import RunEventEmitter, StepEventEmitter
from core.runtime.events import (
    OutputDeltaPayload,
    RuntimeEventType,
    StepCompletedPayload,
    StepStartedPayload,
)
from core.runtime.approval import ToolApprovalRejectedError
from core.runtime.step_completion import StepCompletionResult, StepResultCommitter
from core.runtime.fault_injection import (
    FaultInjectionController,
    evaluate_sync_fault,
)
from core.runtime.fault_injection_contract import FaultPoint
from core.runtime.trace_contract import (
    RUNTIME_STEP_SPAN,
    set_span_attributes,
)


class ParallelFailureMode(str, Enum):
    FAIL_FAST = "FAIL_FAST"
    BEST_EFFORT = "BEST_EFFORT"


class StepExecutionMode(str, Enum):
    ASYNC = "ASYNC"
    SYNC_BLOCKING = "SYNC_BLOCKING"


@dataclass(frozen=True, slots=True)
class ParallelExecutionPolicy:
    """协调 Scheduler 与 Executor 的不可变并发策略。"""
    max_concurrency: int
    failure_mode: ParallelFailureMode = ParallelFailureMode.FAIL_FAST

    def __post_init__(self) -> None:
        if isinstance(self.max_concurrency, bool) or not isinstance(self.max_concurrency, int) or self.max_concurrency <= 0:
            raise ValueError("max_concurrency 必须是正整数")
        if not isinstance(self.failure_mode, ParallelFailureMode):
            raise ValueError("failure_mode 必须是 ParallelFailureMode")


@dataclass(frozen=True, slots=True)
class StepConcurrencySpec:
    """单个 Step 使用的进程内资源并发约束。"""
    resource_key: str = "default"
    resource_limit: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.resource_key, str) or not self.resource_key.strip():
            raise ValueError("resource_key 必须是非空字符串")
        if isinstance(self.resource_limit, bool) or not isinstance(self.resource_limit, int) or self.resource_limit <= 0:
            raise ValueError("resource_limit 必须是正整数")


@dataclass(frozen=True, slots=True)
class StepExecutionOutcome:
    """一个已 Claim Step 的调用期终态结果，不持久化 result。"""
    step_id: str
    status: StepStatus
    started_at: datetime
    ended_at: datetime
    result: Any = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.CANCELLED}:
            raise ValueError("Outcome status 必须是 Step 终态")
        for value in (self.started_at, self.ended_at):
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError("Outcome 时间必须是带时区的 UTC 时间")
        if self.ended_at < self.started_at:
            raise ValueError("Outcome ended_at 不得早于 started_at")
        if self.status == StepStatus.SUCCEEDED and (self.error_code or self.error_message):
            raise ValueError("成功 Outcome 不得携带错误信息")


@dataclass(frozen=True, slots=True)
class ParallelExecutionReport:
    """按输入 Claim 顺序聚合的批次执行报告。"""
    failure_mode: ParallelFailureMode
    outcomes: tuple[StepExecutionOutcome, ...]
    succeeded_step_ids: tuple[str, ...]
    failed_step_ids: tuple[str, ...]
    cancelled_step_ids: tuple[str, ...]
    was_cancelled: bool
    completion_results: tuple[StepCompletionResult, ...] = ()


class StepExecutionDriver(Protocol):
    """只执行业务；不得修改 AgentState、发送 STARTED 或决定调度。"""
    def execute(self, claim: StepClaim, run_context: RunContext) -> Any: ...


class ParallelExecutionInfrastructureError(RuntimeError):
    """状态机或执行器自身不变量失败时的安全异常。"""
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        self.safe_message = message
        super().__init__(f"{message} (error_code={error_code})")


class _FailFastSignal(Exception):
    pass


class ParallelExecutor:
    """以 TaskGroup 受管并发执行已被 Scheduler Claim 的 Step。"""
    def __init__(
        self,
        state_machine: AgentStateMachine | None = None,
        *,
        max_concurrency: int = 1,
        event_emitter: RunEventEmitter | None = None,
        span_recorder=None,
        blocking_executor: BoundedBlockingExecutor | None = None,
        completion_owner: StepResultCommitter | None = None,
        fault_controller: FaultInjectionController | None = None,
    ) -> None:
        self._validate_positive(max_concurrency, "max_concurrency")
        self._state_machine = state_machine or AgentStateMachine()
        self._max_concurrency = max_concurrency
        self._event_emitter = event_emitter
        self._span_recorder = span_recorder
        self._blocking_executor = blocking_executor
        self._completion_owner = completion_owner
        if fault_controller is not None and not isinstance(
            fault_controller, FaultInjectionController
        ):
            raise TypeError("fault_controller 必须是 FaultInjectionController 或 None")
        self._fault_controller = fault_controller

    async def execute_ready(self, *, scheduler: SerialScheduler, plan: Plan, state: AgentState, occurred_at: datetime, run_context: RunContext, driver: StepExecutionDriver, policy: ParallelExecutionPolicy, execution_mode: StepExecutionMode = StepExecutionMode.ASYNC, concurrency_specs: Mapping[str, StepConcurrencySpec] | None = None, completion_owner: StepResultCommitter | None = None) -> ParallelExecutionReport:
        """标准安全入口：同一 Policy 同时约束 Claim 和 Executor 容量。"""
        ledger = run_context.budget_ledger
        effective_concurrency = min(policy.max_concurrency, ledger.budget.max_concurrency) if ledger is not None and ledger.budget.max_concurrency is not None else policy.max_concurrency
        claims = scheduler.claim_ready(plan, state, effective_concurrency, occurred_at, budget_ledger=ledger)
        return await self.execute(
            claims=claims,
            plan=plan,
            state=state,
            run_context=run_context,
            driver=driver,
            policy=policy,
            execution_mode=execution_mode,
            concurrency_specs=concurrency_specs,
            completion_owner=(
                completion_owner
                if completion_owner is not None
                else self._completion_owner
            ),
        )

    async def execute(self, *, claims: tuple[StepClaim, ...], state: AgentState, run_context: RunContext, driver: StepExecutionDriver, failure_mode: ParallelFailureMode = ParallelFailureMode.FAIL_FAST, execution_mode: StepExecutionMode = StepExecutionMode.ASYNC, concurrency_specs: Mapping[str, StepConcurrencySpec] | None = None, policy: ParallelExecutionPolicy | None = None, plan: Plan | None = None, completion_owner: StepResultCommitter | None = None) -> ParallelExecutionReport:
        """低层执行入口；提供 Policy 时其容量和失败策略优先。"""
        if policy is not None:
            failure_mode = policy.failure_mode
            max_concurrency = policy.max_concurrency
        else:
            max_concurrency = self._max_concurrency
        effective_completion_owner = (
            completion_owner
            if completion_owner is not None
            else self._completion_owner
        )
        if not isinstance(failure_mode, ParallelFailureMode) or not isinstance(execution_mode, StepExecutionMode):
            raise ValueError("failure_mode 和 execution_mode 必须是合法枚举")
        if len({item.step_id for item in claims}) != len(claims):
            raise ValueError("claims 不允许包含重复 step_id")
        specs = concurrency_specs or {}
        try:
            self._preflight(driver, execution_mode, specs, claims)
            run_context.raise_if_inactive()
        except RunCancelledError:
            await self._cancel_claims(
                claims, state, "RUN_CANCELLED", "运行已请求取消"
            )
            raise
        except ParallelExecutionInfrastructureError:
            await self._cancel_claims(
                claims,
                state,
                "EXECUTION_PREFLIGHT_FAILED",
                "执行前置校验失败",
            )
            raise
        resource_semaphores = {key: asyncio.Semaphore(limit) for key, limit in self._resource_limits(specs, claims).items()}
        global_semaphore = asyncio.Semaphore(max_concurrency)
        outcomes: dict[str, StepExecutionOutcome] = {}
        completions: dict[str, StepCompletionResult] = {}
        was_token_cancelled = False
        fail_fast_triggered = asyncio.Event()

        async def worker(claim: StepClaim) -> None:
            nonlocal was_token_cancelled
            from core.runtime.tracing import (
                NoopSpanRecorder,
                activate_span,
                current_span_recorder,
                current_trace_context,
                start_span_safely,
            )
            parent_context = current_trace_context()
            recorder = current_span_recorder() or self._span_recorder or NoopSpanRecorder()
            step_span = start_span_safely(recorder,
                trace_id=run_context.trace_id, run_id=run_context.run_id,
                component="step", operation=RUNTIME_STEP_SPAN, step_id=claim.step_id,
                parent_context=parent_context,
            )
            plan_step = None
            if plan is not None:
                for candidate in plan.steps:
                    if candidate.step_id == claim.step_id:
                        plan_step = candidate
                        break
            if plan_step is not None:
                set_span_attributes(
                    step_span,
                    preferred_agent=plan_step.preferred_agent,
                    execution_kind=plan_step.execution_kind.value,
                    output_policy=plan_step.output_policy.value,
                    dependency_count=len(plan_step.depends_on),
                )
            activity_tracker = run_context.activity_tracker
            if activity_tracker is not None:
                activity_tracker.increment("step_workers_active")
            try:  # 覆盖等待全局/资源许可、Driver、to_thread 等待及终态提交前的取消。
                with activate_span(step_span):
                    await self._emit_step_started(claim, plan=plan)
                    run_context.raise_if_inactive()
                    spec = specs.get(claim.step_id, StepConcurrencySpec())
                    async with global_semaphore, resource_semaphores[spec.resource_key]:
                        run_context.raise_if_inactive()
                        if fail_fast_triggered.is_set():
                            raise asyncio.CancelledError()
                        try:
                            result = await self._invoke(driver, claim, run_context, execution_mode)
                        except (
                            asyncio.CancelledError, RunCancelledError,
                            RunDeadlineExceededError, BudgetExceededError,
                            ParallelExecutionInfrastructureError,
                        ):
                            raise
                        except ToolApprovalRejectedError:
                            self._approval_rejected_terminal(
                                state, claim, outcomes
                            )
                            await self._emit_step_completed(
                                claim,
                                state,
                                StepStatus.FAILED,
                                "TOOL_APPROVAL_REJECTED",
                                plan=plan,
                            )
                            if failure_mode == ParallelFailureMode.FAIL_FAST:
                                fail_fast_triggered.set()
                                raise _FailFastSignal() from None
                            return
                        except Exception:
                            code = (
                                self._driver_failure_code(plan, claim)
                                if effective_completion_owner is not None
                                else "STEP_EXECUTION_FAILED"
                            )
                            outcomes[claim.step_id] = self._terminal(state, claim, StepStatus.FAILED, error_code=code, error_message="步骤业务执行失败")
                            await self._emit_step_completed(
                                claim,
                                state,
                                StepStatus.FAILED,
                                code,
                                plan=plan,
                            )
                            if failure_mode == ParallelFailureMode.FAIL_FAST:
                                fail_fast_triggered.set(); raise _FailFastSignal() from None
                            return
                    if effective_completion_owner is not None:
                        # Typed mode: the completion owner is the only writer of
                        # Store/Step state and emits STEP_COMPLETED. No
                        # OUTPUT_DELTA is ever produced for multi-step Steps.
                        completion = await effective_completion_owner.commit(claim, result, state)
                        completions[claim.step_id] = completion
                        set_span_attributes(
                            step_span,
                            state=(
                                completion.error_code
                                if completion.error_code is not None
                                else "SUCCEEDED"
                            ),
                            result_char_count=completion.char_count,
                            delivery_status=completion.delivery_status.value,
                        )
                        current = state.steps.get(claim.step_id)
                        if completion.error_code is not None:
                            status = (
                                current.status
                                if current is not None
                                else StepStatus.FAILED
                            )
                            outcomes[claim.step_id] = self._outcome_from_state(
                                claim, state, status
                            )
                            if failure_mode == ParallelFailureMode.FAIL_FAST:
                                fail_fast_triggered.set()
                                raise _FailFastSignal() from None
                            return
                        outcomes[claim.step_id] = self._outcome_from_state(
                            claim, state, StepStatus.SUCCEEDED
                        )
                    else:
                        if (
                            self._event_emitter is not None
                            and getattr(driver, "emits_user_output", False)
                            and isinstance(result, str)
                        ):
                            await self._event_emitter.for_step(claim.step_id).emit(
                                RuntimeEventType.OUTPUT_DELTA,
                                OutputDeltaPayload(result),
                                component="parallel_executor",
                            )
                        outcomes[claim.step_id] = self._terminal(state, claim, StepStatus.SUCCEEDED, result=result)
                        await self._emit_step_completed(
                            claim,
                            state,
                            StepStatus.SUCCEEDED,
                            None,
                            plan=plan,
                        )
            except asyncio.CancelledError:
                outcomes[claim.step_id] = self._settle_outcome(
                    state, claim, StepStatus.CANCELLED,
                    error_code="STEP_CANCELLED", error_message="步骤执行已取消",
                )
                await self._emit_step_completed(
                    claim,
                    state,
                    StepStatus.CANCELLED,
                    "STEP_CANCELLED",
                    plan=plan,
                )
                raise
            except RunCancelledError:
                was_token_cancelled = True
                outcomes[claim.step_id] = self._settle_outcome(
                    state, claim, StepStatus.CANCELLED,
                    error_code="RUN_CANCELLED", error_message="运行已请求取消",
                )
                await self._emit_step_completed(
                    claim,
                    state,
                    StepStatus.CANCELLED,
                    "RUN_CANCELLED",
                    plan=plan,
                )
            except (BudgetExceededError, RunDeadlineExceededError):
                raise
            except (ParallelExecutionInfrastructureError, _FailFastSignal):
                raise
            except ToolApprovalRejectedError:
                self._approval_rejected_terminal(state, claim, outcomes)
                await self._emit_step_completed(
                    claim,
                    state,
                    StepStatus.FAILED,
                    "TOOL_APPROVAL_REJECTED",
                    plan=plan,
                )
                if failure_mode == ParallelFailureMode.FAIL_FAST:
                    raise _FailFastSignal() from None
            except Exception:
                code = (
                    self._driver_failure_code(plan, claim)
                    if effective_completion_owner is not None
                    else "STEP_EXECUTION_FAILED"
                )
                outcomes[claim.step_id] = self._terminal(state, claim, StepStatus.FAILED, error_code=code, error_message="步骤业务执行失败")
                await self._emit_step_completed(
                    claim,
                    state,
                    StepStatus.FAILED,
                    code,
                    plan=plan,
                )
                if failure_mode == ParallelFailureMode.FAIL_FAST:
                    raise _FailFastSignal() from None
            finally:
                if activity_tracker is not None:
                    activity_tracker.decrement("step_workers_active")

        budget_error: BudgetExceededError | None = None
        deadline_error: RunDeadlineExceededError | None = None
        try:
            try:
                async with asyncio.TaskGroup() as group:
                    for claim in claims:
                        group.create_task(worker(claim))
            except* _FailFastSignal:
                pass
            except* BudgetExceededError as errors:
                budget_error = errors.exceptions[0]
            except* RunDeadlineExceededError as errors:
                deadline_error = errors.exceptions[0]
            if budget_error is not None:
                raise budget_error
            if deadline_error is not None:
                raise deadline_error
        except asyncio.CancelledError:
            await self._cancel_unfinished(claims, outcomes, state)
            raise
        except (BudgetExceededError, RunDeadlineExceededError):
            await self._cancel_unfinished(claims, outcomes, state)
            raise
        except Exception as exc:
            await self._cancel_unfinished(claims, outcomes, state)
            raise ParallelExecutionInfrastructureError("EXECUTION_INFRASTRUCTURE_ERROR", "并行执行基础设施异常") from exc
        await self._cancel_unfinished(claims, outcomes, state)
        ordered = tuple(outcomes[claim.step_id] for claim in claims)
        ordered_completions = tuple(
            completions[claim.step_id]
            for claim in claims
            if claim.step_id in completions
        )
        return ParallelExecutionReport(
            failure_mode,
            ordered,
            tuple(item.step_id for item in ordered if item.status == StepStatus.SUCCEEDED),
            tuple(item.step_id for item in ordered if item.status == StepStatus.FAILED),
            tuple(item.step_id for item in ordered if item.status == StepStatus.CANCELLED),
            was_token_cancelled,
            ordered_completions,
        )

    async def _emit_step_started(
        self, claim: StepClaim, *, plan: Plan | None = None
    ) -> None:
        """Scheduler 已成功写入 RUNNING 后再发布事实。"""
        if self._event_emitter is None:
            return
        plan_step = None
        if plan is not None:
            for candidate in plan.steps:
                if candidate.step_id == claim.step_id:
                    plan_step = candidate
                    break
        try:
            await self._event_emitter.for_step(claim.step_id).emit(
                RuntimeEventType.STEP_STARTED,
                StepStartedPayload(
                    StepStatus.RUNNING.value,
                    agent_id=claim.preferred_agent,
                    execution_kind=(
                        plan_step.execution_kind.value
                        if plan_step is not None
                        else None
                    ),
                    output_policy=(
                        plan_step.output_policy.value
                        if plan_step is not None
                        else None
                    ),
                    dependency_count=(
                        len(plan_step.depends_on)
                        if plan_step is not None
                        else None
                    ),
                ),
                component="scheduler",
            )
        except (EventChannelClosedError, RuntimeError):
            return

    async def _emit_step_completed(
        self,
        claim: StepClaim,
        state: AgentState,
        status: StepStatus,
        safe_error_code: str | None,
        *,
        result_char_count: int = 0,
        delivery_status: str | None = None,
        delivery_duration_ms: int = 0,
        plan: Plan | None = None,
    ) -> None:
        """State Machine 已提交终态后发布并关闭该 StepEmitter。"""
        if self._event_emitter is None:
            return
        emitter: StepEventEmitter = self._event_emitter.for_step(claim.step_id)
        if emitter.is_closed:
            return
        step = state.steps.get(claim.step_id)
        duration_ms = 0
        if (
            step is not None
            and step.started_at is not None
            and step.ended_at is not None
        ):
            duration_ms = max(
                0, int((step.ended_at - step.started_at).total_seconds() * 1000)
            )
        plan_step = None
        if plan is not None:
            for candidate in plan.steps:
                if candidate.step_id == claim.step_id:
                    plan_step = candidate
                    break
        try:
            await emitter.emit(
                RuntimeEventType.STEP_COMPLETED,
                StepCompletedPayload(
                    status.value,
                    safe_error_code,
                    duration_ms=duration_ms,
                    result_char_count=result_char_count,
                    delivery_status=delivery_status,
                    delivery_duration_ms=delivery_duration_ms,
                    execution_kind=(
                        plan_step.execution_kind.value
                        if plan_step is not None
                        else None
                    ),
                    output_policy=(
                        plan_step.output_policy.value
                        if plan_step is not None
                        else None
                    ),
                ),
                component="parallel_executor",
                close=True,
                ignore_run_cancellation=True,
            )
        except (EventChannelClosedError, RuntimeError):
            return

    def _preflight(self, driver: StepExecutionDriver, mode: StepExecutionMode, specs: Mapping[str, StepConcurrencySpec], claims: tuple[StepClaim, ...]) -> None:
        is_async = inspect.iscoroutinefunction(driver.execute)
        if (mode == StepExecutionMode.ASYNC and not is_async) or (mode == StepExecutionMode.SYNC_BLOCKING and is_async):
            raise ParallelExecutionInfrastructureError("DRIVER_MODE_MISMATCH", "Driver 与执行模式不匹配")
        self._resource_limits(specs, claims)

    @staticmethod
    def _resource_limits(specs: Mapping[str, StepConcurrencySpec], claims: tuple[StepClaim, ...]) -> dict[str, int]:
        limits: dict[str, int] = {}
        for claim in claims:
            spec = specs.get(claim.step_id, StepConcurrencySpec())
            current = limits.setdefault(spec.resource_key, spec.resource_limit)
            if current != spec.resource_limit:
                raise ParallelExecutionInfrastructureError("RESOURCE_LIMIT_CONFLICT", f"资源 {spec.resource_key} 的并发限制冲突: {current} 与 {spec.resource_limit}")
        return limits

    async def _invoke(self, driver: StepExecutionDriver, claim: StepClaim, context: RunContext, mode: StepExecutionMode) -> Any:
        if mode == StepExecutionMode.SYNC_BLOCKING:
            evaluate_sync_fault(
                self._fault_controller,
                point=FaultPoint.EXECUTOR_BEFORE_SUBMIT,
                component="parallel_executor",
                run_id=context.run_id,
                step_id=claim.step_id,
                operation_kind="EXECUTOR_SUBMIT",
            )
            if self._blocking_executor is not None:
                def remaining_seconds() -> float:
                    remaining = context.remaining_seconds()
                    return 86400.0 if remaining is None else remaining

                handle = self._blocking_executor.submit(
                    lambda: driver.execute(claim, context),
                    kind=BlockingTaskKind.RUNTIME_STEP,
                    run_id=context.run_id,
                    operation_id=claim.step_id,
                    cancellation_check=context.raise_if_inactive,
                    remaining_seconds=remaining_seconds,
                )
                try:
                    return await asyncio.to_thread(handle.result)
                except asyncio.CancelledError:
                    handle.cancel_or_detach()
                    raise
            return await asyncio.to_thread(driver.execute, claim, context)
        return await driver.execute(claim, context)

    def _terminal(self, state: AgentState, claim: StepClaim, status: StepStatus, *, result: Any = None, error_code: str | None = None, error_message: str | None = None) -> StepExecutionOutcome:
        current = state.steps.get(claim.step_id)
        if current is None:
            raise ParallelExecutionInfrastructureError("STEP_MISSING", "已 Claim 步骤在状态机中缺失")
        if current.status not in {StepStatus.RUNNING, StepStatus.WAITING_FOR_APPROVAL}:
            raise ParallelExecutionInfrastructureError("STEP_NOT_RUNNING", "已 Claim 步骤未处于 RUNNING/WAITING 状态")
        if (
            current.status is StepStatus.WAITING_FOR_APPROVAL
            and status is not StepStatus.CANCELLED
        ):
            # WAITING 只允许 cancellation 直接由 executor 收口；REJECT 走
            # APPROVAL_REJECTED 专用 transition（由 controller bridge 完成）。
            raise ParallelExecutionInfrastructureError("STEP_NOT_RUNNING", "WAITING Step 只能被取消收口")
        event_type = {StepStatus.SUCCEEDED: StepEventType.SUCCEEDED, StepStatus.FAILED: StepEventType.FAILED, StepStatus.CANCELLED: StepEventType.CANCELLED}[status]
        ended_at = max(datetime.now(UTC), state.updated_at, current.started_at or claim.claimed_at)
        self._state_machine.apply_step_event(state, StepStateEvent(event_type, claim.step_id, occurred_at=ended_at, error_code=error_code, error_message=error_message))
        return StepExecutionOutcome(claim.step_id, status, current.started_at or claim.claimed_at, ended_at, result, error_code, error_message)

    @staticmethod
    def _outcome_from_state(
        claim: StepClaim,
        state: AgentState,
        status: StepStatus,
    ) -> StepExecutionOutcome:
        current = state.steps.get(claim.step_id)
        if current is None:
            raise ParallelExecutionInfrastructureError(
                "STEP_MISSING",
                "已提交 Step 在状态机中缺失",
            )
        if status not in {
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.CANCELLED,
        }:
            raise ParallelExecutionInfrastructureError(
                "STEP_NOT_TERMINAL",
                "Outcome 只能使用 Step 终态",
            )
        ended_at = current.ended_at or max(
            datetime.now(UTC), current.started_at or claim.claimed_at
        )
        return StepExecutionOutcome(
            claim.step_id,
            status,
            current.started_at or claim.claimed_at,
            ended_at,
            None,
            current.error_code,
            current.error_message,
        )

    def _settle_outcome(
        self,
        state: AgentState,
        claim: StepClaim,
        status: StepStatus,
        *,
        error_code: str | None,
        error_message: str | None,
    ) -> StepExecutionOutcome:
        current = state.steps.get(claim.step_id)
        if current is None:
            raise ParallelExecutionInfrastructureError(
                "STEP_MISSING",
                "已 Claim Step 在状态机中缺失",
            )
        if current.status is StepStatus.WAITING_FOR_APPROVAL:
            # WAITING Step 是 active：cancel 时经状态机 WAITING->CANCELLED。
            return self._terminal(
                state,
                claim,
                StepStatus.CANCELLED,
                error_code=error_code or "RUN_CANCELLED",
                error_message=error_message,
            )
        if current.status is not StepStatus.RUNNING:
            return self._outcome_from_state(claim, state, current.status)
        return self._terminal(
            state,
            claim,
            status,
            error_code=error_code,
            error_message=error_message,
        )

    def _approval_rejected_terminal(
        self,
        state: AgentState,
        claim: StepClaim,
        outcomes: dict[str, StepExecutionOutcome],
    ) -> StepExecutionOutcome:
        """REJECT 专用收口：WAITING -> FAILED(TOOL_APPROVAL_REJECTED)。

        只有 AgentStateMachine 写 Step status；这里通过 APPROVAL_REJECTED
        StepEvent 转移。Step 可能已被 coordinator settle 成终态（幂等返回）。
        """
        current = state.steps.get(claim.step_id)
        if current is None:
            raise ParallelExecutionInfrastructureError(
                "STEP_MISSING",
                "已 Claim Step 在状态机中缺失",
            )
        if current.status in {
            StepStatus.FAILED,
            StepStatus.CANCELLED,
        }:
            outcome = self._outcome_from_state(claim, state, current.status)
            outcomes[claim.step_id] = outcome
            return outcome
        if current.status is not StepStatus.WAITING_FOR_APPROVAL:
            raise ParallelExecutionInfrastructureError(
                "STEP_NOT_WAITING",
                "Approval Reject 只能收口 WAITING_FOR_APPROVAL Step",
            )
        ended_at = max(datetime.now(UTC), state.updated_at, current.started_at or claim.claimed_at)
        self._state_machine.apply_step_event(
            state,
            StepStateEvent(
                StepEventType.APPROVAL_REJECTED,
                claim.step_id,
                occurred_at=ended_at,
                error_code="TOOL_APPROVAL_REJECTED",
                error_message="Tool 调用已被拒绝审批",
            ),
        )
        outcome = StepExecutionOutcome(
            claim.step_id,
            StepStatus.FAILED,
            current.started_at or claim.claimed_at,
            ended_at,
            None,
            "TOOL_APPROVAL_REJECTED",
            "Tool 调用已被拒绝审批",
        )
        outcomes[claim.step_id] = outcome
        return outcome

    @staticmethod
    def _driver_failure_code(plan: Plan | None, claim: StepClaim) -> str:
        if plan is not None:
            for step in plan.steps:
                if step.step_id == claim.step_id:
                    if step.execution_kind is ExecutionKind.SYNTHESIS:
                        return "SYNTHESIS_FAILED"
                    return "AGENT_STEP_FAILED"
        return "AGENT_STEP_FAILED"

    async def _cancel_claims(self, claims: tuple[StepClaim, ...], state: AgentState, error_code: str, message: str) -> None:
        for claim in claims:
            step = state.steps.get(claim.step_id)
            if step is not None and step.status in {
                StepStatus.RUNNING,
                StepStatus.WAITING_FOR_APPROVAL,
            }:
                self._terminal(state, claim, StepStatus.CANCELLED, error_code=error_code, error_message=message)
                await self._emit_step_completed(
                    claim, state, StepStatus.CANCELLED, error_code
                )

    async def _cancel_unfinished(self, claims: tuple[StepClaim, ...], outcomes: dict[str, StepExecutionOutcome], state: AgentState) -> None:
        for claim in claims:
            current = state.steps.get(claim.step_id)
            if current is not None and current.status not in {
                StepStatus.RUNNING,
                StepStatus.WAITING_FOR_APPROVAL,
            }:
                continue
            if claim.step_id not in outcomes:
                outcomes[claim.step_id] = self._terminal(state, claim, StepStatus.CANCELLED, error_code="STEP_CANCELLED", error_message="步骤执行已取消")
                await self._emit_step_completed(
                    claim, state, StepStatus.CANCELLED, "STEP_CANCELLED"
                )

    @staticmethod
    def _validate_positive(value: int, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} 必须是正整数")
