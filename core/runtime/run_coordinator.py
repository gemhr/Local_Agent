#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单次 Run 的 Parent Runtime 与统一生命周期所有者。"""

from __future__ import annotations

import asyncio
import inspect
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from core.runtime.activity import RuntimeActivityProvider, RuntimeActivityTracker
from core.runtime.budget import BudgetExceededError, BudgetLedger, BudgetSnapshot
from core.runtime.cancellation import CancellationReason, RunCancelledError
from core.runtime.checkpoint import CheckpointCoordinator, default_runtime_metadata
from core.runtime.checkpoint_contract import (
    CheckpointKind,
    CheckpointMode,
    CheckpointResult,
)
from core.runtime.context import RunContext, RunDeadlineExceededError
from core.runtime.parallel_execution import (
    ParallelExecutionInfrastructureError,
    ParallelExecutionReport,
    ParallelExecutionPolicy,
    ParallelExecutor,
    StepConcurrencySpec,
    StepExecutionDriver,
    StepExecutionMode,
)
from core.runtime.step_completion import StepResultCommitter
from core.runtime.step_result_store import StepResultStore
from core.runtime.output_gate import OutputGate
from core.runtime.final_memory_writer import RunFinalMemoryWriter
from core.runtime.multi_agent_driver import MultiAgentDriver
from core.runtime.plan_graph import PlanGraphValidationError, PlanGraphValidator
from core.runtime.plan_fingerprint import PlanFingerprinter
from core.runtime.planning import ExecutionKind, OutputPolicy, Plan, compute_plan_shape
from core.runtime.multi_agent_planning import (
    PLANNER_SCHEMA_VERSION,
    PlanResolver,
    PlanningError,
    PlanningErrorCode,
    PlanningRequest,
    ResolvedPlan,
)
from core.runtime.agent_registry import AgentRegistryError
from core.runtime.plan_compiler import PlanCompileError
from core.runtime.run_registry import ActiveRunControlHandle, RunHandle, RunRegistry
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
    PlanCreatedPayload,
    PlanningStartedPayload,
    RunCompletedPayload,
    RunStartedPayload,
    RuntimeEventType,
    StepCompletedPayload,
    TimeoutPayload,
)
from core.runtime.tracing import (NoopSpanRecorder, activate_span,
                                  install_span_recorder, install_trace_context,
                                  reset_span_recorder, reset_trace_context,
                                  start_span_safely)
from core.runtime.trace_contract import (
    RUNTIME_PLANNING_SPAN,
    RUNTIME_RUN_SPAN,
    set_span_attributes,
)
from core.runtime.fault_injection import (
    FaultInjectionController,
    evaluate_sync_fault,
)
from core.runtime.fault_injection_contract import (
    FaultPoint,
    InjectedFaultError,
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
    StopReason.PLANNING_FAILED: "运行规划失败",
}


class DynamicPlanState(str, Enum):
    UNRESOLVED = "UNRESOLVED"
    RESOLVING = "RESOLVING"
    FROZEN = "FROZEN"
    FAILED = "FAILED"


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
    plan_id: str | None
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
    """Single-use Parent Runtime for either a trusted or dynamically frozen Plan."""

    def __init__(
        self,
        *,
        run_context: RunContext,
        plan: Plan,
        agent_state: AgentState,
        budget_ledger: BudgetLedger,
        run_handle: ActiveRunControlHandle,
        scheduler: SerialScheduler,
        executor: ParallelExecutor,
        run_registry: RunRegistry,
        policy: ParallelExecutionPolicy,
        state_machine: AgentStateMachine | None = None,
        event_emitter: RunEventEmitter | None = None,
        span_recorder=None,
        snapshot_store=None,
        runtime_metadata=None,
        metrics_recorder=None,
    ) -> None:
        self._initialize_base(
            run_context=run_context,
            agent_state=agent_state,
            budget_ledger=budget_ledger,
            run_handle=run_handle,
            run_registry=run_registry,
            policy=policy,
            state_machine=state_machine,
            event_emitter=event_emitter,
            span_recorder=span_recorder,
            snapshot_store=snapshot_store,
            runtime_metadata=runtime_metadata,
            metrics_recorder=metrics_recorder,
        )
        self._bind_static_plan(plan, scheduler, executor)

    @classmethod
    def for_static_plan(cls, **kwargs) -> "RunCoordinator":
        """显式构造已经拥有可信 Plan 的兼容 Runtime。"""
        return cls(**kwargs)

    @classmethod
    def for_dynamic_resolver(
        cls,
        *,
        run_context: RunContext,
        plan_resolver: PlanResolver,
        planning_request: PlanningRequest,
        execution_factory: Callable[[], tuple[SerialScheduler, ParallelExecutor]],
        agent_state: AgentState,
        budget_ledger: BudgetLedger,
        run_handle: ActiveRunControlHandle,
        run_registry: RunRegistry,
        policy: ParallelExecutionPolicy,
        planning_timeout_seconds: float = 15.0,
        state_machine: AgentStateMachine | None = None,
        event_emitter: RunEventEmitter | None = None,
        span_recorder=None,
        snapshot_store=None,
        runtime_metadata=None,
        metrics_recorder=None,
        multi_agent_driver: MultiAgentDriver | None = None,
        persist: bool = True,
        step_result_per_result_chars: int = 20_000,
        step_result_run_total_chars: int = 60_000,
        step_result_max_entries: int = 16,
        fault_controller: FaultInjectionController | None = None,
    ) -> "RunCoordinator":
        """构造尚无 Plan/Scheduler/Checkpoint 的动态规划 Runtime。"""
        if not isinstance(plan_resolver, PlanResolver):
            raise TypeError("plan_resolver 必须是 PlanResolver")
        if not isinstance(planning_request, PlanningRequest):
            raise TypeError("planning_request 必须是 PlanningRequest")
        if not callable(execution_factory):
            raise TypeError("execution_factory 必须可调用")
        if fault_controller is not None and not isinstance(
            fault_controller, FaultInjectionController
        ):
            raise TypeError("fault_controller 必须是 FaultInjectionController 或 None")
        if (
            isinstance(planning_timeout_seconds, bool)
            or not isinstance(planning_timeout_seconds, (int, float))
            or not math.isfinite(float(planning_timeout_seconds))
            or planning_timeout_seconds <= 0
        ):
            raise ValueError("planning_timeout_seconds 必须是正数")
        self = cls.__new__(cls)
        self._initialize_base(
            run_context=run_context,
            agent_state=agent_state,
            budget_ledger=budget_ledger,
            run_handle=run_handle,
            run_registry=run_registry,
            policy=policy,
            state_machine=state_machine,
            event_emitter=event_emitter,
            span_recorder=span_recorder,
            snapshot_store=snapshot_store,
            runtime_metadata=runtime_metadata,
            metrics_recorder=metrics_recorder,
        )
        self._dynamic = True
        self._dynamic_plan_state = DynamicPlanState.UNRESOLVED
        self._plan_resolver = plan_resolver
        self._planning_request = planning_request
        self._execution_factory = execution_factory
        self._planning_timeout_seconds = float(planning_timeout_seconds)
        if multi_agent_driver is not None and not isinstance(
            multi_agent_driver, MultiAgentDriver
        ):
            raise TypeError("multi_agent_driver 必须是 MultiAgentDriver")
        for value, name in (
            (step_result_per_result_chars, "step_result_per_result_chars"),
            (step_result_run_total_chars, "step_result_run_total_chars"),
            (step_result_max_entries, "step_result_max_entries"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ValueError(f"{name} 必须是正整数")
        if step_result_run_total_chars < step_result_per_result_chars:
            raise ValueError(
                "step_result_run_total_chars 不得小于 step_result_per_result_chars"
            )
        self._multi_agent_driver = multi_agent_driver
        if type(persist) is not bool:
            raise TypeError("persist 必须是 bool")
        self._persist = persist
        self._step_result_store: StepResultStore | None = None
        self._step_completion_owner: StepResultCommitter | None = None
        self._output_gate: OutputGate | None = None
        self._typed_multi_step_plan = False
        self._step_result_per_result_chars = step_result_per_result_chars
        self._step_result_run_total_chars = step_result_run_total_chars
        self._step_result_max_entries = step_result_max_entries
        self._fault_controller = fault_controller
        return self

    def _initialize_base(
        self,
        *,
        run_context: RunContext,
        agent_state: AgentState,
        budget_ledger: BudgetLedger,
        run_handle: ActiveRunControlHandle,
        run_registry: RunRegistry,
        policy: ParallelExecutionPolicy,
        state_machine: AgentStateMachine | None,
        event_emitter: RunEventEmitter | None,
        span_recorder,
        snapshot_store,
        runtime_metadata,
        metrics_recorder,
    ) -> None:
        self.run_context = run_context
        self._plan: Plan | None = None
        self._invocation_bindings = None
        self.agent_state = agent_state
        self.budget_ledger = budget_ledger
        self.run_handle = run_handle
        self.scheduler: SerialScheduler | None = None
        self.executor: ParallelExecutor | None = None
        self.run_registry = run_registry
        self.policy = policy
        self.state_machine = state_machine or AgentStateMachine()
        self.event_emitter = event_emitter
        self.span_recorder = span_recorder or NoopSpanRecorder()
        tracker = run_context.activity_tracker
        if tracker is None:
            tracker = RuntimeActivityTracker(run_context.run_id)
            run_context.attach_activity_tracker(tracker)
        self.activity_tracker = tracker
        self.checkpoint_coordinator = None
        self._snapshot_store = snapshot_store
        self._runtime_metadata = runtime_metadata
        self._metrics_recorder = metrics_recorder
        self._dynamic = False
        self._dynamic_plan_state = DynamicPlanState.FROZEN
        self._multi_agent_driver = None
        self._persist = True
        self._step_result_store = None
        self._step_completion_owner = None
        self._output_gate = None
        self._typed_multi_step_plan = False
        self._step_result_per_result_chars = 20_000
        self._step_result_run_total_chars = 60_000
        self._step_result_max_entries = 16
        self._plan_resolver = None
        self._planning_request = None
        self._execution_factory = None
        self._planning_timeout_seconds = 0.0
        self._fault_controller = None

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

    @property
    def plan_frozen(self) -> bool:
        return self._plan is not None and self._dynamic_plan_state is DynamicPlanState.FROZEN

    @property
    def plan(self) -> Plan | None:
        return self._plan

    @property
    def invocation_bindings(self):
        return self._invocation_bindings

    @property
    def step_result_store(self) -> StepResultStore | None:
        """Run-scoped Store; only the completion owner may write it."""
        return self._step_result_store

    @property
    def step_completion_owner(self) -> StepResultCommitter | None:
        return self._step_completion_owner

    @property
    def output_gate(self) -> OutputGate | None:
        return self._output_gate

    @property
    def user_request(self) -> str | None:
        if self._planning_request is None:
            return None
        return self._planning_request.user_request

    @property
    def dynamic_plan_state(self) -> DynamicPlanState:
        return self._dynamic_plan_state

    def attach_multi_agent_runtime(self, driver: MultiAgentDriver) -> None:
        """WP3 typed runtime injection; must happen before execution."""
        if not isinstance(driver, MultiAgentDriver):
            raise TypeError("driver 必须是 MultiAgentDriver")
        with self._start_lock:
            if self._started:
                raise RunCoordinatorError(
                    "COORDINATOR_ALREADY_STARTED",
                    "启动后不能再注入多 Agent Runtime",
                )
            self._multi_agent_driver = driver

    def _is_typed_multi_step_plan(self) -> bool:
        """WP4: every dynamic Coordinated plan uses the typed pipeline.

        The dynamic single-step forms (Core direct, explicit entry, delegated
        knowledge direct) are migrated from the legacy string-output path to
        the typed completion pipeline + OutputGate contract.
        """
        plan = self.plan
        if plan is None:
            return False
        return self._dynamic

    def _typed_multi_step_enabled(self) -> bool:
        return (
            self._typed_multi_step_plan
            and self._multi_agent_driver is not None
            and self._step_result_store is not None
            and self._step_completion_owner is not None
        )

    def _initialize_typed_runtime(self) -> None:
        if not self._is_typed_multi_step_plan():
            return
        if self._multi_agent_driver is None:
            raise RunCoordinatorError(
                "MULTI_AGENT_RUNTIME_NOT_INJECTED",
                "多 Step Plan 需要 MultiAgentDriver 注入",
            )
        plan = self.plan
        assert plan is not None
        store = StepResultStore(
            plan,
            run_id=self.run_context.run_id,
            per_result_chars=self._step_result_per_result_chars,
            run_total_chars=self._step_result_run_total_chars,
            max_entries=self._step_result_max_entries,
            fault_controller=self._fault_controller,
        )
        gate = OutputGate(
            plan=plan,
            store=store,
            event_emitter=self.event_emitter,
            state_getter=lambda: self.agent_state,
            run_active=lambda: self.agent_state.status
            in {RunStatus.CREATED, RunStatus.RUNNING},
            span_recorder=self.span_recorder,
            metrics_recorder=self._metrics_recorder,
            fault_controller=self._fault_controller,
        )
        memory_writer: RunFinalMemoryWriter | None = None
        if self._planning_request is not None:
            memory_writer = RunFinalMemoryWriter(
                self._multi_agent_driver._router,
                entry_agent_id=self._planning_request.selected_agent_id,
                user_request=self._planning_request.user_request,
                persist=self._persist,
                run_id=self.run_context.run_id,
                span_recorder=self.span_recorder,
                metrics_recorder=self._metrics_recorder,
            )
        committer = StepResultCommitter(
            store=store,
            state_machine=self.state_machine,
            event_emitter=self.event_emitter,
            plan=plan,
            output_gate=gate,
            final_memory_writer=memory_writer,
        )
        self._step_result_store = store
        self._step_completion_owner = committer
        self._output_gate = gate
        self._typed_multi_step_plan = True

    def _bind_static_plan(
        self,
        plan: Plan,
        scheduler: SerialScheduler,
        executor: ParallelExecutor,
    ) -> None:
        if not isinstance(plan, Plan):
            raise TypeError("static coordinator 必须提供 Plan")
        self._plan = plan
        self.scheduler = scheduler
        self.executor = executor
        self._register_plan_steps_and_checkpoint()

    def _freeze_dynamic_plan(self, resolved: ResolvedPlan) -> None:
        if not self._dynamic or self._dynamic_plan_state is not DynamicPlanState.RESOLVING:
            raise RunCoordinatorError("DYNAMIC_PLAN_FREEZE_INVALID", "动态 Plan 只能冻结一次")
        scheduler, executor = self._execution_factory()
        if not isinstance(scheduler, SerialScheduler) or not isinstance(executor, ParallelExecutor):
            raise RunCoordinatorError("DYNAMIC_EXECUTION_FACTORY_INVALID", "动态执行组件构造失败")
        self._plan = resolved.plan
        self._invocation_bindings = resolved.invocation_bindings
        self.scheduler = scheduler
        self.executor = executor
        self._register_plan_steps_and_checkpoint()
        self._initialize_typed_runtime()
        self._dynamic_plan_state = DynamicPlanState.FROZEN

    def _register_plan_steps_and_checkpoint(self) -> None:
        plan = self.plan
        scheduler = self.scheduler
        if plan is None or scheduler is None:
            raise RunCoordinatorError("PLAN_NOT_FROZEN", "执行组件只能在 Plan 冻结后初始化")
        PlanGraphValidator.validate(plan)
        with self.agent_state.runtime_lock:
            for step in plan.steps:
                existing = self.agent_state.steps.get(step.step_id)
                if existing is not None and existing.name != step.title:
                    raise ValueError("Plan and AgentState step definitions differ")
            for step in plan.steps:
                if step.step_id not in self.agent_state.steps:
                    self.state_machine.register_plan_step(
                        self.agent_state, step_id=step.step_id, name=step.title
                    )
        if self._snapshot_store is None:
            return
        if self.event_emitter is None:
            raise ValueError("snapshot_store requires an event-backed coordinator")
        provider = RuntimeActivityProvider(
            run_id=self.run_context.run_id,
            tracker=self.activity_tracker,
            claim_gate=scheduler.claim_gate,
            agent_state=self.agent_state,
            budget_ledger=self.budget_ledger,
            event_channel=self.event_emitter.channel,
        )
        self.checkpoint_coordinator = CheckpointCoordinator(
            run_context=self.run_context,
            plan=plan,
            agent_state=self.agent_state,
            budget_ledger=self.budget_ledger,
            event_channel=self.event_emitter.channel,
            snapshot_store=self._snapshot_store,
            claim_gate=scheduler.claim_gate,
            activity_provider=provider,
            runtime_metadata=self._runtime_metadata or default_runtime_metadata(),
        )

    async def _prepare_dynamic_execution(self) -> RunFinalizationDecision | None:
        if self._dynamic_plan_state is not DynamicPlanState.UNRESOLVED:
            raise RunCoordinatorError(
                "DYNAMIC_PLAN_STATE_INVALID", "动态规划生命周期状态无效"
            )
        self._dynamic_plan_state = DynamicPlanState.RESOLVING
        await self._emit_planning_started()
        self.run_context.raise_if_inactive()
        remaining = self.run_context.remaining_seconds()
        effective_timeout = self._planning_timeout_seconds
        limited_by_run_deadline = (
            remaining is not None
            and remaining <= self._planning_timeout_seconds
        )
        if remaining is not None:
            effective_timeout = min(effective_timeout, remaining)
        if effective_timeout <= 0:
            raise RunDeadlineExceededError("run deadline exceeded")
        planning_started = time.monotonic()
        try:
            try:
                evaluate_sync_fault(
                    self._fault_controller,
                    point=FaultPoint.PLANNING_BEFORE_RESOLVE,
                    component="run_coordinator",
                    run_id=self.run_context.run_id,
                    operation_kind="PLANNING_RESOLVE",
                )
                resolved = await asyncio.wait_for(
                    self._plan_resolver.resolve(
                        self._planning_request,
                        self.run_context,
                    ),
                    timeout=effective_timeout,
                )
            except InjectedFaultError:
                raise PlanningError(
                    PlanningErrorCode.PLANNING_MODEL_FAILED,
                    "Planner 模型调用失败",
                ) from None
            except TimeoutError:
                if limited_by_run_deadline:
                    raise RunDeadlineExceededError("run deadline exceeded") from None
                remaining_after = self.run_context.remaining_seconds()
                if remaining_after is not None and remaining_after <= 0:
                    raise RunDeadlineExceededError("run deadline exceeded") from None
                raise PlanningError(
                    self._planner_timeout_code(), "Planner 独立超时"
                ) from None
        except BaseException:
            self._record_planning_metrics(
                planning_source="unknown",
                status="FAILED",
                duration_seconds=time.monotonic() - planning_started,
            )
            raise
        self._record_planning_metrics(
            planning_source=resolved.planning_source.value,
            status="SUCCEEDED",
            duration_seconds=time.monotonic() - planning_started,
        )
        self.run_context.raise_if_inactive()
        self._freeze_dynamic_plan(resolved)
        evaluate_sync_fault(
            self._fault_controller,
            point=FaultPoint.PLANNING_BEFORE_PLAN_CREATED,
            component="run_coordinator",
            run_id=self.run_context.run_id,
            operation_kind="PLAN_CREATED_EVENT",
        )
        await self._emit_plan_created(resolved)
        if self.checkpoint_coordinator is not None:
            checkpoint = await self.create_checkpoint(
                mode=CheckpointMode.REQUIRE_QUIESCENT,
                checkpoint_kind=CheckpointKind.POST_PLAN_PRE_EXECUTION,
                timeout=self.run_context.remaining_seconds(),
                cancellation_token=self.run_context.cancellation_token,
                fault_controller=self._fault_controller,
            )
            if not checkpoint.persisted:
                self.run_context.raise_if_inactive()
                return self._infrastructure_decision(
                    "POST_PLAN_PRE_EXECUTION_CHECKPOINT_FAILED"
                )
        return None

    @staticmethod
    def _planner_timeout_code():
        from core.runtime.multi_agent_planning import PlanningErrorCode

        return PlanningErrorCode.PLANNER_TIMEOUT

    def _record_planning_metrics(
        self,
        *,
        planning_source: str,
        status: str,
        duration_seconds: float,
    ) -> None:
        recorder = self._metrics_recorder
        if recorder is None:
            return
        labels = {"planning_source": planning_source, "status": status}
        try:
            recorder.increment_counter(
                "runtime_planning_total", labels=labels
            )
            recorder.observe_histogram(
                "runtime_planning_duration_seconds",
                max(0.0, duration_seconds),
                labels=labels,
            )
        except Exception:
            return

    async def _emit_planning_started(self) -> None:
        if self.event_emitter is None:
            return
        await self.event_emitter.emit(
            RuntimeEventType.PLANNING_STARTED,
            PlanningStartedPayload(
                planner_schema_version=PLANNER_SCHEMA_VERSION,
                configured_timeout_ms=max(
                    0, int(self._planning_timeout_seconds * 1000)
                ),
            ),
            component="run_coordinator",
        )

    async def _emit_plan_created(self, resolved: ResolvedPlan) -> None:
        if self.event_emitter is None:
            return
        await self.event_emitter.emit(
            RuntimeEventType.PLAN_CREATED,
            PlanCreatedPayload(
                plan_id=resolved.plan.plan_id,
                plan_version=resolved.plan.version,
                fingerprint=PlanFingerprinter.fingerprint(resolved.plan),
                step_count=len(resolved.plan.steps),
                planning_source=resolved.planning_source.value,
                shape=compute_plan_shape(resolved.plan),
            ),
            component="run_coordinator",
        )

    def _layered_terminal_facts(
        self, decision: RunFinalizationDecision
    ) -> dict[str, object]:
        """推导 STEP/Delivery/Memory/Run 四层终态事实（不虚构）。

        只在可证明的边界填写：
        - Run SUCCEEDED 仅当 OutputGate 报告 DELIVERED，因此 delivery=DELIVERED；
        - FINAL_OUTPUT_MEMORY_COMMIT_FAILED 表示 delivered=true、memory=false；
        - memory 只有在 delivery=DELIVERED 后才被尝试。
        """
        final_step_status = None
        plan = self.plan
        final_step_id = None
        if plan is not None:
            finals = tuple(
                step
                for step in plan.steps
                if step.output_policy is not OutputPolicy.INTERNAL
            )
            if len(finals) == 1:
                final_step_id = finals[0].step_id
        if final_step_id is not None:
            step = self.agent_state.steps.get(final_step_id)
            if step is not None:
                final_step_status = step.status.value

        error_code = decision.error_code
        delivery_status = None
        memory_commit_status = None
        if decision.status is RunStatus.SUCCEEDED:
            delivery_status = "DELIVERED"
            memory_commit_status = (
                "SUCCEEDED" if self._persist else "NOT_ATTEMPTED"
            )
        elif error_code == "FINAL_OUTPUT_MEMORY_COMMIT_FAILED":
            delivery_status = "DELIVERED"
            memory_commit_status = "FAILED"
        elif error_code == "FINAL_OUTPUT_DELIVERY_FAILED":
            delivery_status = "FAILED"
            memory_commit_status = "NOT_ATTEMPTED"
        elif error_code == "FINAL_OUTPUT_DELIVERY_UNKNOWN":
            delivery_status = "OUTCOME_UNKNOWN"
            memory_commit_status = "NOT_ATTEMPTED"
        elif delivery_status is None and final_step_status == "SUCCEEDED":
            # Final Step 成功但 Run 未成功且无专门 delivery 错误码：交付结果
            # 无法从已提交事实推导，保持未知，不虚构。
            delivery_status = "OUTCOME_UNKNOWN"

        return {
            "delivery_status": delivery_status,
            "final_step_status": final_step_status,
            "memory_commit_status": memory_commit_status,
            "safe_error_code": error_code,
            "shape": compute_plan_shape(plan) if plan is not None else None,
        }

    def _clear_invocation_bindings(
        self, cleanup_error_codes: list[str]
    ) -> None:
        bindings = self._invocation_bindings
        self._invocation_bindings = None
        if bindings is None:
            return
        try:
            bindings.close_and_clear()
        except Exception:
            cleanup_error_codes.append("INVOCATION_BINDINGS_CLEANUP_FAILED")

    def _seal_step_result_store(
        self, cleanup_error_codes: list[str]
    ) -> None:
        """Seal immediately at Run terminal so detached workers/late results
        are rejected before any cleanup proceeds."""
        store = self._step_result_store
        if store is None:
            return
        try:
            store.seal()
        except Exception:
            cleanup_error_codes.append("STEP_RESULT_STORE_SEAL_FAILED")

    def _clear_step_result_store(
        self, cleanup_error_codes: list[str]
    ) -> None:
        """Clear at a safe lifecycle point (no live workers) and release raw
        content; idempotent."""
        store = self._step_result_store
        if store is None:
            return
        try:
            store.clear()
        except Exception:
            cleanup_error_codes.append("STEP_RESULT_STORE_CLEAR_FAILED")

    async def create_checkpoint(
        self,
        *,
        mode: CheckpointMode,
        checkpoint_kind: CheckpointKind,
        timeout: float | None,
        cancellation_token=None,
        shutdown_token=None,
        fault_controller=None,
    ) -> CheckpointResult:
        """Explicit opt-in entry; this does not schedule automatic checkpoints."""
        if self.checkpoint_coordinator is None:
            raise RunCoordinatorError(
                "CHECKPOINT_NOT_CONFIGURED",
                "Coordinator 未注入 SnapshotStore",
            )
        return await self.checkpoint_coordinator.capture(
            mode=mode,
            checkpoint_kind=checkpoint_kind,
            timeout=timeout,
            cancellation_token=cancellation_token,
            shutdown_token=shutdown_token,
            fault_controller=fault_controller,
        )

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

        run_span = start_span_safely(self.span_recorder,
            trace_id=self.run_context.trace_id, run_id=self.run_context.run_id,
            component="runtime", operation=RUNTIME_RUN_SPAN
        )
        trace_token = install_trace_context(run_span.context)
        recorder_token = install_span_recorder(self.span_recorder)

        registered = False
        task_cancelled = False
        terminal_publication_failed = False
        cleanup_error_codes: list[str] = []
        decision: RunFinalizationDecision | None = None
        try:
            try:
                existing_handle = self.run_registry.get(self.run_context.run_id)
                if existing_handle is None:
                    self.run_registry.register(self.run_handle)
                elif existing_handle is not self.run_handle:
                    raise ValueError("different active handle already registered")
            except Exception as exc:
                raise RunCoordinatorError(
                    "COORDINATOR_REGISTRATION_FAILED",
                    "RunHandle 注册失败",
                ) from exc
            registered = True

            with self.activity_tracker.track(
                "state_event_transitions_in_flight"
            ):
                self.state_machine.apply_run_event(
                    self.agent_state, RunStateEvent(RunEventType.STARTED)
                )
                await self._emit_run_started()
            self._start_deadline_watcher()
            planner_span = start_span_safely(self.span_recorder,
                trace_id=self.run_context.trace_id, run_id=self.run_context.run_id,
                component="planner", operation=RUNTIME_PLANNING_SPAN
            )
            with activate_span(planner_span):
                if self._dynamic:
                    decision = await self._prepare_dynamic_execution()
                else:
                    assert self.plan is not None and self.scheduler is not None
                    PlanGraphValidator.validate(self.plan)
                self._attach_planning_span_attributes(planner_span)
            if decision is None:
                assert self.plan is not None and self.scheduler is not None
                self.scheduler.prepare(
                    self.plan, self.agent_state, self._event_time(),
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
        except (PlanningError, AgentRegistryError, PlanCompileError) as exc:
            self._dynamic_plan_state = DynamicPlanState.FAILED
            code = getattr(exc, "error_code", "PLANNING_FAILED")
            decision = self._planning_failure_decision(
                code.value if isinstance(code, Enum) else str(code)
            )
        except asyncio.CancelledError:
            task_cancelled = True
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
            self._seal_step_result_store(cleanup_error_codes)
            if decision is None:
                decision = self._infrastructure_decision(
                    "COORDINATOR_FINALIZATION_REQUIRED"
                )
            if self._dynamic and self._dynamic_plan_state in {
                DynamicPlanState.UNRESOLVED,
                DynamicPlanState.RESOLVING,
            }:
                self._dynamic_plan_state = DynamicPlanState.FAILED
            await self._settle_active_steps(decision, cleanup_error_codes)
            with self.activity_tracker.track(
                "state_event_transitions_in_flight"
            ):
                decision = self._finalize_once(decision)
                try:
                    await self._emit_terminal_events(decision)
                except Exception:
                    terminal_publication_failed = True
                    cleanup_error_codes.append(
                        "RUNTIME_TERMINAL_PUBLICATION_FAILED"
                    )
            self._stop_deadline_watcher(cleanup_error_codes)
            self._clear_step_result_store(cleanup_error_codes)
            self._clear_invocation_bindings(cleanup_error_codes)
            await self._run_cleanup_callbacks(cleanup_error_codes)
            budget_snapshot = self._snapshot_budget(cleanup_error_codes)
            if registered:
                self._unregister(cleanup_error_codes)

        result = self._build_result(
            decision=decision,
            budget_snapshot=budget_snapshot,
            cleanup_error_codes=cleanup_error_codes,
        )
        self._attach_run_span_attributes(run_span, result)
        if terminal_publication_failed:
            run_span.end_error("RUNTIME_TERMINAL_PUBLICATION_FAILED")
        elif result.status is RunStatus.SUCCEEDED: run_span.end_ok()
        elif result.status is RunStatus.CANCELLED: run_span.end_cancelled(result.error_code or "CANCELLED")
        else: run_span.end_error(result.error_code or "RUN_FAILED")
        reset_trace_context(trace_token)
        reset_span_recorder(recorder_token)
        if task_cancelled:
            raise asyncio.CancelledError()
        if terminal_publication_failed:
            raise RunCoordinatorError(
                "RUNTIME_TERMINAL_PUBLICATION_FAILED",
                "Runtime terminal publication failed",
            ) from None
        return result

    def _attach_planning_span_attributes(self, planner_span) -> None:
        """只写 Trace Contract v1 允许的规划安全属性，不记录 raw 内容。"""
        plan = self.plan
        if plan is None:
            return
        internals = tuple(
            step
            for step in plan.steps
            if step.output_policy is OutputPolicy.INTERNAL
        )
        set_span_attributes(
            planner_span,
            planning_source=plan.source.value,
            schema_version=PLANNER_SCHEMA_VERSION,
            planner_model_invoked=(
                plan.source.value == "model_generated"
            ),
            compiled_shape=compute_plan_shape(plan),
            specialist_count=len(internals),
            synthesis_required=any(
                step.execution_kind is ExecutionKind.SYNTHESIS
                for step in plan.steps
            ),
        )

    def _attach_run_span_attributes(self, run_span, result) -> None:
        """Run root span 的安全归因属性；缺失版本一律不虚构。"""
        plan = self.plan
        selected_agent_id = None
        if self._planning_request is not None:
            selected_agent_id = self._planning_request.selected_agent_id
        runtime_mode = None
        metadata = self._runtime_metadata
        if metadata is not None:
            runtime_mode = getattr(metadata, "runtime_mode", None)
        set_span_attributes(
            run_span,
            plan_id=plan.plan_id if plan is not None else None,
            plan_version=plan.version if plan is not None else None,
            plan_fingerprint=(
                PlanFingerprinter.fingerprint(plan) if plan is not None else None
            ),
            planning_source=(
                plan.source.value if plan is not None else "unknown"
            ),
            step_count=len(plan.steps) if plan is not None else None,
            selected_entry_agent_id=selected_agent_id,
            runtime_mode=runtime_mode,
            runtime_version="not_configured",
            prompt_version="not_configured",
            model_config_hash="not_configured",
            toolset_hash="not_configured",
            kb_version="not_configured",
            final_status=result.status.value,
            stop_reason=result.stop_reason.value,
            shape=compute_plan_shape(plan) if plan is not None else None,
        )

    async def _execute_batches(
        self,
        *,
        driver: StepExecutionDriver,
        execution_mode: StepExecutionMode,
        concurrency_specs: dict[str, StepConcurrencySpec] | None,
    ) -> RunFinalizationDecision:
        if self.plan is None or self.scheduler is None or self.executor is None:
            raise RunCoordinatorError(
                "EXECUTION_BEFORE_PLAN_FROZEN",
                "Plan 冻结前不得进入执行阶段",
            )
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

            if self._typed_multi_step_plan and self._multi_agent_driver is None:
                raise RunCoordinatorError(
                    "MULTI_AGENT_RUNTIME_NOT_INJECTED",
                    "多 Step Plan 需要 MultiAgentDriver 注入",
                )
            typed = self._typed_multi_step_enabled()
            effective_driver = self._multi_agent_driver if typed else driver
            completion_owner = self._step_completion_owner if typed else None
            effective_specs = concurrency_specs
            if typed:
                # Independent specialists must really overlap: give every Step
                # its own resource key so the default shared key (limit 1)
                # cannot serialize them. Global max concurrency still bounds
                # the batch.
                effective_specs = dict(concurrency_specs or {})
                for step in self.plan.steps:
                    if step.step_id not in effective_specs:
                        effective_specs[step.step_id] = StepConcurrencySpec(
                            resource_key=f"step:{step.step_id}",
                            resource_limit=1,
                        )
            self._executor_task = asyncio.create_task(
                self.executor.execute_ready(
                    scheduler=self.scheduler,
                    plan=self.plan,
                    state=self.agent_state,
                    occurred_at=self._event_time(),
                    run_context=self.run_context,
                    driver=effective_driver,
                    policy=self.policy,
                    execution_mode=execution_mode,
                    concurrency_specs=effective_specs,
                    completion_owner=completion_owner,
                )
            )
            try:
                report = await self._executor_task
            finally:
                if self._executor_task.done():
                    self._executor_task = None
            # WP3: consume safe completion failures before the next Scheduler
            # success decision; raw StepResult never enters this report.
            decision = self._decision_from_batch_report(report)
            if decision is not None:
                return decision

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
        if self._typed_multi_step_enabled() and snapshot.blocked_step_ids:
            return RunFinalizationDecision(
                RunStatus.FAILED,
                StopReason.UNHANDLED_ERROR,
                "REQUIRED_DEPENDENCY_FAILED",
                "required dependency 失败导致 synthesis 被阻塞",
            )
        if any(
            step.status == StepStatus.FAILED
            for step in self.agent_state.steps.values()
        ):
            error_code = "STEP_EXECUTION_FAILED"
            if self._typed_multi_step_enabled():
                error_code = self._typed_failure_code()
            return RunFinalizationDecision(
                RunStatus.FAILED,
                StopReason.UNHANDLED_ERROR,
                error_code,
                "一个或多个步骤执行失败",
            )
        if snapshot.has_unresolved_pending or snapshot.blocked_step_ids:
            return self._no_action_decision()
        return None

    def _decision_from_batch_report(
        self,
        report: ParallelExecutionReport,
    ) -> RunFinalizationDecision | None:
        """Consume safe completion failures before the next Scheduler
        success judgment. Producer driver failures are left for the next
        snapshot so required-dependency BLOCKED propagation decides the Run."""
        if not self._typed_multi_step_enabled():
            return None
        for completion in report.completion_results:
            if completion is not None and completion.error_code is not None:
                error_code = completion.error_code
                if error_code in {
                    "OUTPUT_GATE_DUPLICATE_ATTEMPT",
                    "FINAL_OUTPUT_DELIVERY_FAILED",
                    "FINAL_OUTPUT_DELIVERY_UNKNOWN",
                    "FINAL_OUTPUT_MEMORY_COMMIT_FAILED",
                }:
                    error_code = completion.error_code
                return RunFinalizationDecision(
                    RunStatus.FAILED,
                    StopReason.UNHANDLED_ERROR,
                    error_code,
                    _SAFE_MESSAGES[StopReason.UNHANDLED_ERROR],
                )
        return None

    def _typed_failure_code(self) -> str:
        plan = self.plan
        for step_id, step in self.agent_state.steps.items():
            if step.status is not StepStatus.FAILED:
                continue
            if plan is None:
                return "AGENT_STEP_FAILED"
            for plan_step in plan.steps:
                if plan_step.step_id == step_id:
                    if plan_step.execution_kind is ExecutionKind.SYNTHESIS:
                        return "SYNTHESIS_FAILED"
                    return "AGENT_STEP_FAILED"
        return "AGENT_STEP_FAILED"

    def _final_result_ready(self) -> bool:
        store = self._step_result_store
        plan = self.plan
        if store is None or plan is None:
            return False
        finals = tuple(
            step
            for step in plan.steps
            if step.output_policy is not OutputPolicy.INTERNAL
        )
        if len(finals) != 1:
            return False
        return store.has_readable(finals[0].step_id)

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
        handle_state = getattr(self.run_handle, "agent_state", self.agent_state)
        if handle_state is not self.agent_state:
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
        if parsed in {
            CancellationReason.DEADLINE_EXCEEDED,
            CancellationReason.REQUEST_DEADLINE_EXCEEDED,
        }:
            return RunFinalizationDecision(
                RunStatus.FAILED,
                StopReason.DEADLINE_EXCEEDED,
                "DEADLINE_EXCEEDED",
                _SAFE_MESSAGES[StopReason.DEADLINE_EXCEEDED],
            )
        canonical_stop_reason = {
            CancellationReason.REQUEST_CANCELLED: StopReason.USER_CANCELLED,
            CancellationReason.SERVER_SHUTDOWN: StopReason.SYSTEM_SHUTDOWN,
            CancellationReason.STREAM_ENCODING_FAILED: StopReason.USER_CANCELLED,
        }.get(parsed)
        stop_reason = canonical_stop_reason or StopReason(parsed.value)
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
    def _planning_failure_decision(error_code: str) -> RunFinalizationDecision:
        return RunFinalizationDecision(
            RunStatus.FAILED,
            StopReason.PLANNING_FAILED,
            error_code,
            _SAFE_MESSAGES[StopReason.PLANNING_FAILED],
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
                with self.activity_tracker.track(
                    "state_event_transitions_in_flight"
                ):
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
                facts = self._layered_terminal_facts(decision)
                await self.event_emitter.emit(
                    RuntimeEventType.ERROR,
                    ErrorPayload(
                        safe_error_code=decision.error_code
                        or "COORDINATOR_FAILED",
                        safe_message=decision.safe_message,
                        component="run_coordinator",
                        fatal=True,
                        delivery_status=facts["delivery_status"],
                        final_step_status=facts["final_step_status"],
                        memory_commit_status=facts["memory_commit_status"],
                    ),
                    component="run_coordinator",
                    ignore_run_cancellation=True,
                )
            facts = self._layered_terminal_facts(decision)
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
                    safe_error_code=facts["safe_error_code"],
                    delivery_status=facts["delivery_status"],
                    final_step_status=facts["final_step_status"],
                    memory_commit_status=facts["memory_commit_status"],
                    shape=facts["shape"],
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
            args=(CancellationReason.REQUEST_DEADLINE_EXCEEDED,),
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
        plan = self.plan
        ordered_ids = tuple(step.step_id for step in plan.steps) if plan else ()

        def ids_for(status: StepStatus) -> tuple[str, ...]:
            return tuple(
                step_id
                for step_id in ordered_ids
                if self.agent_state.steps.get(step_id) is not None
                and self.agent_state.steps[step_id].status == status
            )

        return RunCoordinatorResult(
            run_id=self.run_context.run_id,
            plan_id=plan.plan_id if plan is not None else None,
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
