#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Per-request coordinated runtime assembly."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
import time

from core.runtime.application_services import (
    ApplicationRuntimeServices,
    RuntimeLifecycleState,
)
from core.runtime.cancellation import CancellationReason
from core.runtime.budget import BudgetLedger, RunBudget
from core.runtime.context import LEGACY_DEFAULT_SESSION_ID, create_run_context
from core.runtime.project_memory import ProjectIdentity, ProjectMemoryGrant
from core.runtime.event_channel import RuntimeEventChannel
from core.runtime.event_emitter import RunEventEmitter, StepEventEmitter
from core.runtime.fault_injection import FaultInjectionController
from core.runtime.model_invocation import ModelInvocationResult
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from core.runtime.agent_adapter_factory import (
    AgentAdapterFactory,
    AgentRouterSingleAgentAdapter,
)
from core.runtime.multi_agent_planning import PlanResolver, PlanningRequest
from core.runtime.multi_agent_driver import MultiAgentDriver
from core.runtime.plan_compiler import PlanCompiler
from core.runtime.planning import ExecutionKind, OutputPolicy, Plan
from core.runtime.planning_model_adapter import UnifiedPlanningModelAdapter
from core.runtime.synthesis import SynthesisAgentAdapter
from core.runtime.parallel_execution import (
    ParallelExecutionPolicy,
    ParallelExecutor,
    StepExecutionMode,
)
from core.runtime.run_coordinator import RunCoordinator, RunCoordinatorResult
from core.runtime.run_registry import ActiveRunControlHandle
from core.runtime.state import RunStatus
from core.runtime.scheduler import SerialScheduler, StepClaim
from core.runtime.state import AgentState
from core.runtime.state_machine import AgentStateMachine
from core.runtime.tracing import OperationScopedSpanRecorder


class CoordinatedSingleAgentDriver:
    """Execute the adapter without taking state, registry, or sequence ownership."""

    def __init__(
        self,
        router,
        *,
        user_query: str,
        agent_id: str,
        persist: bool,
        event_emitter: StepEventEmitter | None = None,
        fault_controller: FaultInjectionController | None = None,
        approval_controller=None,
    ) -> None:
        self._router = router
        self._user_query = user_query
        self._agent_id = agent_id
        self._persist = persist
        self._event_emitter = event_emitter
        self._fault_controller = fault_controller
        self._approval_controller = approval_controller
        self.emits_user_output = True
        self.output: str | None = None
        self.invocation_result: ModelInvocationResult | None = None

    def execute(self, claim: StepClaim, run_context) -> str:
        if claim.step_id != "answer" or claim.preferred_agent != self._agent_id:
            raise RuntimeError("Coordinated single-agent claim does not match driver")
        invocation_results: list[ModelInvocationResult] = []
        self.output = self._router.complete_single_agent(
            self._agent_id,
            self._user_query,
            run_context=run_context,
            capability_requirements=claim.capability_requirements,
            persist=self._persist,
            invocation_result_out=invocation_results,
            event_emitter=self._event_emitter,
            fault_controller=self._fault_controller,
            approval_controller=self._approval_controller,
        )
        self.invocation_result = (
            invocation_results[0] if invocation_results else None
        )
        return self.output


class ResolvedSingleStepDriver:
    """WP2 compatibility driver consuming the frozen claim Binding."""

    def __init__(
        self,
        router,
        *,
        coordinator: RunCoordinator,
        persist: bool,
        event_emitter: RunEventEmitter,
        fault_controller: FaultInjectionController | None = None,
    ) -> None:
        self._router = router
        self._coordinator = coordinator
        self._persist = persist
        self._event_emitter = event_emitter
        self._fault_controller = fault_controller
        self.emits_user_output = True
        self.output: str | None = None
        self.invocation_result: ModelInvocationResult | None = None

    def execute(self, claim: StepClaim, run_context) -> str:
        bindings = self._coordinator.invocation_bindings
        plan = self._coordinator.plan
        if bindings is None or plan is None or len(plan.steps) != 1:
            raise RuntimeError("Resolved single-step driver requires one frozen Plan")
        binding = bindings.resolve_for_step(
            claim.step_id, expected_agent_id=claim.preferred_agent
        )
        # WP4-B direct entry bundle reuse：single-step plan 且 step agent 等于
        # entry agent 时复用同一 immutable bundle（每 Run 至多一次 retrieval，
        # 禁止再次查询 SQLite）。delegated specialist step 不满足 agent 匹配，
        # 天然 fail closed（SPECIALIST_MEMORY_VISIBILITY = NO）。
        memory_context_bundle = None
        coordinator_bundle = self._coordinator.memory_context_bundle
        if (
            coordinator_bundle is not None
            and coordinator_bundle.entry_agent_id == claim.preferred_agent
            and plan.steps[0].execution_kind is ExecutionKind.AGENT
        ):
            memory_context_bundle = coordinator_bundle
        invocation_results: list[ModelInvocationResult] = []
        self.output = self._router.complete_single_agent(
            claim.preferred_agent,
            binding.instruction,
            run_context=run_context,
            capability_requirements=claim.capability_requirements,
            persist=self._persist,
            invocation_result_out=invocation_results,
            event_emitter=self._event_emitter.for_step(claim.step_id),
            fault_controller=self._fault_controller,
            memory_context_bundle=memory_context_bundle,
            approval_controller=getattr(
                self._coordinator, "tool_approval_controller", None
            ),
        )
        self.invocation_result = invocation_results[0] if invocation_results else None
        return self.output


@dataclass(slots=True)
class CoordinatedRunScope:
    """The strong owner of every object created for one coordinated request."""

    run_context: object
    cancellation_source: object
    agent_state: AgentState
    budget_ledger: BudgetLedger
    plan: object | None
    state_machine: AgentStateMachine
    policy: ParallelExecutionPolicy
    scheduler: SerialScheduler | None
    executor: ParallelExecutor | None
    event_channel: RuntimeEventChannel
    event_emitter: RunEventEmitter
    coordinator: RunCoordinator
    driver: CoordinatedSingleAgentDriver | ResolvedSingleStepDriver
    run_registry: object
    run_handle: ActiveRunControlHandle
    fault_controller: FaultInjectionController | None = field(
        default=None,
        repr=False,
    )
    gauge_provider: object | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _executed: bool = field(default=False, init=False, repr=False)
    _producer_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _close_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )

    @property
    def run_id(self) -> str:
        return self.run_context.run_id

    @property
    def checkpoint_coordinator(self):
        return self.coordinator.checkpoint_coordinator

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def is_executed(self) -> bool:
        return self._executed

    async def execute(self) -> RunCoordinatorResult:
        if self._closed:
            raise RuntimeError("coordinated run scope is closed")
        if self._executed:
            raise RuntimeError("coordinated run scope is single-use")
        self._executed = True
        try:
            return await self.coordinator.execute(
                driver=self.driver,
                execution_mode=StepExecutionMode.SYNC_BLOCKING,
            )
        finally:
            self.plan = self.coordinator.plan
            self.scheduler = self.coordinator.scheduler
            self.executor = self.coordinator.executor

    def bind_producer_task(self, task: asyncio.Task) -> None:
        if self._producer_task is not None:
            raise RuntimeError("coordinated producer task is already bound")
        self._producer_task = task

    def request_cancel(self, reason: CancellationReason) -> bool:
        """Request cooperative cancellation without closing run resources."""
        if self.agent_state.status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return False
        return self.run_handle.request_cancel(reason)

    async def drain_and_close(self, timeout: float) -> bool:
        """Drain the bounded channel to discard and close normally."""
        if self._closed:
            return self.event_channel.state.value == "CLOSED"
        deadline = time.monotonic() + float(timeout)
        try:
            await asyncio.wait_for(
                self.event_channel.drain_to_discard(),
                timeout=max(0.0, deadline - time.monotonic()),
            )
            producer = self._producer_task
            if producer is not None:
                await asyncio.wait_for(
                    asyncio.shield(producer),
                    timeout=max(0.0, deadline - time.monotonic()),
                )
            await self.close()
            return True
        except TimeoutError:
            return False

    async def force_abort(self, reason: CancellationReason) -> None:
        """Abort request-owned tasks/channel; never close application services."""
        self.request_cancel(reason)
        producer = self._producer_task
        if producer is not None and not producer.done():
            producer.cancel()
        await self.event_channel.abort()
        if producer is not None:
            await asyncio.gather(producer, return_exceptions=True)
        if self.run_registry.get(self.run_id) is self.run_handle:
            self.run_registry.unregister(self.run_id)
        await self._finish_close()

    async def close(self, *, abort: bool = False) -> None:
        """Idempotently release only request-scoped transport registrations."""
        async with self._close_lock:
            if self._closed:
                return
            try:
                if abort:
                    await self.event_channel.abort()
                else:
                    await self.event_channel.close()
            finally:
                await self._finish_close()

    async def _finish_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.run_registry.get(self.run_id) is self.run_handle:
            self.run_registry.unregister(self.run_id)
        if self.gauge_provider is not None:
            self.gauge_provider.unregister_channel(self.event_channel)

    async def abort(self) -> None:
        """Idempotently abort the request transport owned by this scope."""
        await self.force_abort(CancellationReason.REQUEST_CANCELLED)

    async def __aenter__(self) -> "CoordinatedRunScope":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close(abort=exc is not None)

    def __repr__(self) -> str:
        return (
            "CoordinatedRunScope("
            "component='coordinated_run_scope', "
            f"closed={self._closed!r}, executed={self._executed!r})"
        )


class CoordinatedRuntimeFactory:
    """Application-scoped factory; it never caches a returned run scope."""

    __slots__ = (
        "_router",
        "_services",
        "_event_channel_capacity",
        "_planning_timeout_seconds",
        "_adapter_factory",
        "_max_concurrency",
        "_step_result_per_result_chars",
        "_step_result_run_total_chars",
        "_step_result_max_entries",
    )

    def __init__(
        self,
        router,
        services: ApplicationRuntimeServices,
        *,
        event_channel_capacity: int = 32,
        planning_timeout_seconds: float = 15.0,
        max_concurrency: int = 2,
        step_result_per_result_chars: int = 20_000,
        step_result_run_total_chars: int = 60_000,
        step_result_max_entries: int = 16,
    ) -> None:
        if not isinstance(services, ApplicationRuntimeServices):
            raise TypeError("services must be ApplicationRuntimeServices")
        if (
            isinstance(event_channel_capacity, bool)
            or not isinstance(event_channel_capacity, int)
            or event_channel_capacity <= 0
        ):
            raise ValueError("event_channel_capacity must be a positive integer")
        if (
            isinstance(planning_timeout_seconds, bool)
            or not isinstance(planning_timeout_seconds, (int, float))
            or not math.isfinite(float(planning_timeout_seconds))
            or planning_timeout_seconds <= 0
        ):
            raise ValueError("planning_timeout_seconds must be positive")
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency <= 0
        ):
            raise ValueError("max_concurrency must be a positive integer")
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
                raise ValueError(f"{name} must be a positive integer")
        if step_result_run_total_chars < step_result_per_result_chars:
            raise ValueError(
                "step_result_run_total_chars must be >= step_result_per_result_chars"
            )
        self._router = router
        self._services = services
        self._event_channel_capacity = event_channel_capacity
        self._planning_timeout_seconds = float(planning_timeout_seconds)
        self._max_concurrency = max_concurrency
        self._step_result_per_result_chars = step_result_per_result_chars
        self._step_result_run_total_chars = step_result_run_total_chars
        self._step_result_max_entries = step_result_max_entries
        self._adapter_factory = AgentAdapterFactory(
            DEFAULT_AGENT_REGISTRY,
            (
                ("core_router_adapter", AgentRouterSingleAgentAdapter(router)),
                ("data_analyst_adapter", AgentRouterSingleAgentAdapter(router)),
                ("code_expert_adapter", AgentRouterSingleAgentAdapter(router)),
                (
                    "knowledge_expert_adapter",
                    AgentRouterSingleAgentAdapter(router),
                ),
                ("synthesis_agent_adapter", SynthesisAgentAdapter(router)),
            ),
        )

    @property
    def services(self) -> ApplicationRuntimeServices:
        return self._services

    async def create_run_scope(
        self,
        agent_id: str,
        query: str,
        *,
        session_id: str = LEGACY_DEFAULT_SESSION_ID,
        run_id: str | None = None,
        trace_id: str | None = None,
        timeout_seconds: float | None = None,
        budget: RunBudget | None = None,
        persist: bool = True,
        fault_controller: FaultInjectionController | None = None,
        episodic_evaluation_observer=None,
        evaluation_plan_resolver=None,
        project_identity: ProjectIdentity | None = None,
        project_grants: tuple[ProjectMemoryGrant, ...] = (),
    ) -> CoordinatedRunScope:
        """默认 Coordinated 入口：始终经过动态 PlanResolver。"""
        return await self._create_run_scope(
            agent_id,
            query,
            session_id=session_id,
            run_id=run_id,
            trace_id=trace_id,
            timeout_seconds=timeout_seconds,
            budget=budget,
            persist=persist,
            fault_controller=fault_controller,
            episodic_evaluation_observer=episodic_evaluation_observer,
            evaluation_plan_resolver=evaluation_plan_resolver,
            project_identity=project_identity,
            project_grants=project_grants,
            static_plan=None,
        )

    async def create_static_run_scope(
        self,
        agent_id: str,
        query: str,
        *,
        trusted_plan: Plan | None = None,
        session_id: str = LEGACY_DEFAULT_SESSION_ID,
        run_id: str | None = None,
        trace_id: str | None = None,
        timeout_seconds: float | None = None,
        budget: RunBudget | None = None,
        persist: bool = True,
        fault_controller: FaultInjectionController | None = None,
        episodic_evaluation_observer=None,
        project_identity: ProjectIdentity | None = None,
        project_grants: tuple[ProjectMemoryGrant, ...] = (),
    ) -> CoordinatedRunScope:
        """仅供可信内部 Plan、测试和兼容路径使用。"""
        plan = trusted_plan or self._router.build_single_agent_plan(agent_id, query)
        if (
            len(plan.steps) != 1
            or plan.steps[0].step_id != "answer"
            or plan.steps[0].preferred_agent != agent_id
            or plan.steps[0].depends_on
            or plan.steps[0].execution_kind is not ExecutionKind.AGENT
            or plan.steps[0].output_policy is not OutputPolicy.FINAL_PASSTHROUGH
        ):
            raise ValueError(
                "static factory compatibility path requires one answer passthrough step"
            )
        return await self._create_run_scope(
            agent_id,
            query,
            session_id=session_id,
            run_id=run_id,
            trace_id=trace_id,
            timeout_seconds=timeout_seconds,
            budget=budget,
            persist=persist,
            fault_controller=fault_controller,
            episodic_evaluation_observer=episodic_evaluation_observer,
            project_identity=project_identity,
            project_grants=project_grants,
            static_plan=plan,
        )

    async def _create_run_scope(
        self,
        agent_id: str,
        query: str,
        *,
        session_id: str,
        run_id: str | None,
        trace_id: str | None,
        timeout_seconds: float | None,
        budget: RunBudget | None,
        persist: bool,
        fault_controller: FaultInjectionController | None,
        episodic_evaluation_observer=None,
        static_plan: Plan | None,
        evaluation_plan_resolver=None,
        project_identity: ProjectIdentity | None = None,
        project_grants: tuple[ProjectMemoryGrant, ...] = (),
    ) -> CoordinatedRunScope:
        """Create one identity set and clean up any partially built transport."""
        if self._services.lifecycle_state is not RuntimeLifecycleState.READY:
            raise RuntimeError("application runtime services are not ready")
        if fault_controller is not None and not isinstance(
            fault_controller,
            FaultInjectionController,
        ):
            raise TypeError("fault_controller must be FaultInjectionController or None")
        if episodic_evaluation_observer is not None and not (
            callable(getattr(episodic_evaluation_observer, "on_evidence", None))
            and callable(
                getattr(episodic_evaluation_observer, "on_formation", None)
            )
        ):
            raise TypeError(
                "episodic_evaluation_observer must implement on_evidence/on_formation"
            )
        channel: RuntimeEventChannel | None = None
        registered_channel = False
        gauge_provider = getattr(
            self._services.observability_dispatcher, "gauge_provider", None
        )
        try:
            self._services.admission_gate.acquire()
            admission_acquired = True
            run_context, cancellation_source = create_run_context(
                entry_agent_id=agent_id,
                session_id=session_id,
                trace_id=trace_id,
                run_id=run_id,
                timeout_seconds=timeout_seconds,
            )
            run_context.attach_project_memory_access(project_identity, project_grants)
            ledger = BudgetLedger(
                budget or RunBudget(),
                deadline_remaining=run_context.remaining_seconds,
            )
            run_context.attach_budget_ledger(ledger)
            tracker = self._services.new_activity_tracker(run_context.run_id)
            run_context.attach_activity_tracker(tracker)
            span_recorder = self._services.span_recorder
            if fault_controller is not None:
                span_recorder = OperationScopedSpanRecorder(
                    span_recorder,
                    fault_controller=fault_controller,
                    cancellation_token=run_context.cancellation_token,
                )
            agent_state = AgentState.for_run_context(run_context.run_id)
            machine = AgentStateMachine()
            policy = ParallelExecutionPolicy(max_concurrency=self._max_concurrency)
            channel = RuntimeEventChannel(
                self._event_channel_capacity,
                run_id=run_context.run_id,
                cancellation_token=run_context.cancellation_token,
                journal=self._services.event_journal,
                observability_dispatcher=self._services.observability_dispatcher,
                fault_controller=fault_controller,
            )
            if gauge_provider is not None and callable(
                getattr(gauge_provider, "register_channel", None)
            ):
                gauge_provider.register_channel(channel)
                registered_channel = True
            emitter = RunEventEmitter(
                run_id=run_context.run_id,
                trace_id=run_context.trace_id,
                channel=channel,
            )
            def execution_factory() -> tuple[SerialScheduler, ParallelExecutor]:
                return (
                    SerialScheduler(machine),
                    ParallelExecutor(
                        machine,
                        max_concurrency=1,
                        event_emitter=emitter,
                        span_recorder=span_recorder,
                        blocking_executor=self._services.coordinated_step_executor,
                        fault_controller=fault_controller,
                    ),
                )
            run_handle = ActiveRunControlHandle(
                run_id=run_context.run_id,
                runtime_mode="COORDINATED",
                cancellation_source=cancellation_source,
                owner="coordinated_runtime_factory",
                active_step_count=lambda: len(agent_state.active_step_ids),
            )
            snapshot_store = (
                self._services.snapshot_store
                if self._services.snapshot_enabled
                else None
            )
            if static_plan is not None:
                scheduler, executor = execution_factory()
                coordinator = RunCoordinator.for_static_plan(
                    run_context=run_context,
                    plan=static_plan,
                    agent_state=agent_state,
                    budget_ledger=ledger,
                    run_handle=run_handle,
                    scheduler=scheduler,
                    executor=executor,
                    run_registry=self._services.run_registry,
                    policy=policy,
                    state_machine=machine,
                    event_emitter=emitter,
                    span_recorder=span_recorder,
                    snapshot_store=snapshot_store,
                    metrics_recorder=self._services.runtime_metrics_recorder,
                )
                approval_controller = coordinator._ensure_tool_approval_controller()
                driver = CoordinatedSingleAgentDriver(
                    self._router,
                    user_query=query,
                    agent_id=agent_id,
                    persist=persist,
                    event_emitter=emitter.for_step("answer"),
                    fault_controller=fault_controller,
                    approval_controller=approval_controller,
                )
                plan = static_plan
            else:
                planning_model = UnifiedPlanningModelAdapter(
                    self._router,
                    blocking_executor=self._services.coordinated_step_executor,
                    event_emitter=emitter,
                    fault_controller=fault_controller,
                )
                resolver = evaluation_plan_resolver or PlanResolver(
                    DEFAULT_AGENT_REGISTRY,
                    PlanCompiler(DEFAULT_AGENT_REGISTRY),
                    planning_model,
                )
                coordinator = RunCoordinator.for_dynamic_resolver(
                    run_context=run_context,
                    plan_resolver=resolver,
                    planning_request=PlanningRequest(agent_id, query),
                    execution_factory=execution_factory,
                    agent_state=agent_state,
                    budget_ledger=ledger,
                    run_handle=run_handle,
                    run_registry=self._services.run_registry,
                    policy=policy,
                    state_machine=machine,
                    event_emitter=emitter,
                    span_recorder=span_recorder,
                    snapshot_store=snapshot_store,
                    planning_timeout_seconds=self._planning_timeout_seconds,
                    metrics_recorder=self._services.runtime_metrics_recorder,
                    persist=persist,
                    step_result_per_result_chars=self._step_result_per_result_chars,
                    step_result_run_total_chars=self._step_result_run_total_chars,
                    step_result_max_entries=self._step_result_max_entries,
                    fault_controller=fault_controller,
                    episodic_evaluation_observer=episodic_evaluation_observer,
                )
                multi_agent_driver = MultiAgentDriver(
                    router=self._router,
                    coordinator=coordinator,
                    adapter_factory=self._adapter_factory,
                    registry=DEFAULT_AGENT_REGISTRY,
                    fault_controller=fault_controller,
                )
                coordinator.attach_multi_agent_runtime(multi_agent_driver)
                driver = ResolvedSingleStepDriver(
                    self._router,
                    coordinator=coordinator,
                    persist=persist,
                    event_emitter=emitter,
                    fault_controller=fault_controller,
                )
                plan = None
                scheduler = None
                executor = None
            scope = CoordinatedRunScope(
                run_context=run_context,
                cancellation_source=cancellation_source,
                agent_state=agent_state,
                budget_ledger=ledger,
                plan=plan,
                state_machine=machine,
                policy=policy,
                scheduler=scheduler,
                executor=executor,
                event_channel=channel,
                event_emitter=emitter,
                coordinator=coordinator,
                driver=driver,
                run_registry=self._services.run_registry,
                run_handle=run_handle,
                fault_controller=fault_controller,
                gauge_provider=gauge_provider if registered_channel else None,
            )
            run_handle.bind_force_abort(scope.force_abort)
            self._services.run_registry.register(run_handle)
            return scope
        except BaseException:
            if registered_channel and channel is not None:
                try:
                    gauge_provider.unregister_channel(channel)
                except Exception:
                    pass
            if channel is not None:
                try:
                    await channel.abort()
                except Exception:
                    pass
            raise
        finally:
            if locals().get("admission_acquired", False):
                self._services.admission_gate.release()

    async def create(self, *args, **kwargs) -> CoordinatedRunScope:
        return await self.create_run_scope(*args, **kwargs)

    async def create_scope(self, *args, **kwargs) -> CoordinatedRunScope:
        return await self.create_run_scope(*args, **kwargs)

    def __repr__(self) -> str:
        return (
            "CoordinatedRuntimeFactory("
            "component='coordinated_runtime_factory', "
            f"event_channel_capacity={self._event_channel_capacity})"
        )


__all__ = [
    "CoordinatedRunScope",
    "CoordinatedRuntimeFactory",
    "CoordinatedSingleAgentDriver",
    "ResolvedSingleStepDriver",
]
