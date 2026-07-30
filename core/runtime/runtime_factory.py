#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Per-request coordinated runtime assembly."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from core.runtime.application_services import (
    ApplicationRuntimeServices,
    RuntimeLifecycleState,
)
from core.runtime.budget import BudgetLedger, RunBudget
from core.runtime.context import LEGACY_DEFAULT_SESSION_ID, create_run_context
from core.runtime.event_channel import RuntimeEventChannel
from core.runtime.event_emitter import RunEventEmitter, StepEventEmitter
from core.runtime.model_invocation import ModelInvocationResult
from core.runtime.parallel_execution import (
    ParallelExecutionPolicy,
    ParallelExecutor,
    StepExecutionMode,
)
from core.runtime.run_coordinator import RunCoordinator, RunCoordinatorResult
from core.runtime.run_registry import RunHandle
from core.runtime.scheduler import SerialScheduler, StepClaim
from core.runtime.state import AgentState
from core.runtime.state_machine import AgentStateMachine


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
    ) -> None:
        self._router = router
        self._user_query = user_query
        self._agent_id = agent_id
        self._persist = persist
        self._event_emitter = event_emitter
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
        )
        self.invocation_result = (
            invocation_results[0] if invocation_results else None
        )
        return self.output


@dataclass(slots=True)
class CoordinatedRunScope:
    """The strong owner of every object created for one coordinated request."""

    run_context: object
    cancellation_source: object
    agent_state: AgentState
    budget_ledger: BudgetLedger
    plan: object
    state_machine: AgentStateMachine
    policy: ParallelExecutionPolicy
    scheduler: SerialScheduler
    executor: ParallelExecutor
    event_channel: RuntimeEventChannel
    event_emitter: RunEventEmitter
    coordinator: RunCoordinator
    driver: CoordinatedSingleAgentDriver
    gauge_provider: object | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _executed: bool = field(default=False, init=False, repr=False)
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
        return await self.coordinator.execute(
            driver=self.driver,
            execution_mode=StepExecutionMode.SYNC_BLOCKING,
        )

    async def close(self, *, abort: bool = False) -> None:
        """Idempotently release only request-scoped transport registrations."""
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                if abort:
                    await self.event_channel.abort()
                else:
                    await self.event_channel.close()
            finally:
                if self.gauge_provider is not None:
                    self.gauge_provider.unregister_channel(self.event_channel)

    async def abort(self) -> None:
        """Idempotently abort the request transport owned by this scope."""
        await self.close(abort=True)

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

    __slots__ = ("_router", "_services", "_event_channel_capacity")

    def __init__(
        self,
        router,
        services: ApplicationRuntimeServices,
        *,
        event_channel_capacity: int = 32,
    ) -> None:
        if not isinstance(services, ApplicationRuntimeServices):
            raise TypeError("services must be ApplicationRuntimeServices")
        if (
            isinstance(event_channel_capacity, bool)
            or not isinstance(event_channel_capacity, int)
            or event_channel_capacity <= 0
        ):
            raise ValueError("event_channel_capacity must be a positive integer")
        self._router = router
        self._services = services
        self._event_channel_capacity = event_channel_capacity

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
    ) -> CoordinatedRunScope:
        """Create one identity set and clean up any partially built transport."""
        if self._services.lifecycle_state is not RuntimeLifecycleState.READY:
            raise RuntimeError("application runtime services are not ready")
        channel: RuntimeEventChannel | None = None
        registered_channel = False
        gauge_provider = getattr(
            self._services.observability_dispatcher, "gauge_provider", None
        )
        try:
            run_context, cancellation_source = create_run_context(
                entry_agent_id=agent_id,
                session_id=session_id,
                trace_id=trace_id,
                run_id=run_id,
                timeout_seconds=timeout_seconds,
            )
            ledger = BudgetLedger(
                budget or RunBudget(),
                deadline_remaining=run_context.remaining_seconds,
            )
            run_context.attach_budget_ledger(ledger)
            tracker = self._services.new_activity_tracker(run_context.run_id)
            run_context.attach_activity_tracker(tracker)
            agent_state = AgentState.for_run_context(run_context.run_id)
            plan = self._router.build_single_agent_plan(agent_id, query)
            machine = AgentStateMachine()
            policy = ParallelExecutionPolicy(max_concurrency=1)
            channel = RuntimeEventChannel(
                self._event_channel_capacity,
                run_id=run_context.run_id,
                cancellation_token=run_context.cancellation_token,
                journal=self._services.event_journal,
                observability_dispatcher=self._services.observability_dispatcher,
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
            scheduler = SerialScheduler(machine)
            executor = ParallelExecutor(
                machine,
                max_concurrency=1,
                event_emitter=emitter,
            )
            coordinator = RunCoordinator(
                run_context=run_context,
                plan=plan,
                agent_state=agent_state,
                budget_ledger=ledger,
                run_handle=RunHandle(
                    run_context.run_id,
                    cancellation_source,
                    agent_state,
                    "coordinated_runtime_factory",
                ),
                scheduler=scheduler,
                executor=executor,
                run_registry=self._services.run_registry,
                policy=policy,
                state_machine=machine,
                event_emitter=emitter,
                span_recorder=self._services.span_recorder,
                snapshot_store=(
                    self._services.snapshot_store
                    if self._services.snapshot_enabled
                    else None
                ),
            )
            driver = CoordinatedSingleAgentDriver(
                self._router,
                user_query=query,
                agent_id=agent_id,
                persist=persist,
                event_emitter=emitter.for_step("answer"),
            )
            return CoordinatedRunScope(
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
                gauge_provider=gauge_provider if registered_channel else None,
            )
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
]
