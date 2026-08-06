#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WP3 MultiAgentDriver.

Real call chain:

    StepClaim
    -> resolve frozen PlanStep
    -> bindings.resolve_for_step(step_id, expected_agent_id)
    -> AgentRegistry.resolve(agent_id)
    -> execution_adapter_id
    -> AgentAdapterFactory.resolve(adapter_id)
    -> build AgentExecutionRequest
    -> if synthesis: attach dependency-scoped result view
    -> adapter.execute
    -> convert AgentAdapterResult to StepResult
    -> return StepResult

The Driver never writes Store, never changes AgentState, never publishes
STEP_STARTED/STEP_COMPLETED or user text, and never persists Memory.
"""

from __future__ import annotations

from enum import Enum

from core.runtime.agent_adapter_factory import (
    AgentAdapterError,
    AgentAdapterFactory,
    AgentAdapterResult,
    AgentExecutionAdapter,
    AgentExecutionRequest,
)
from core.runtime.agent_registry import (
    AgentRegistration,
    AgentRegistry,
    AgentRegistryError,
)
from core.runtime.invocation_bindings import InvocationBindingError
from core.runtime.planning import ExecutionKind, Plan, PlanStep
from core.runtime.scheduler import StepClaim
from core.runtime.step_result import ResultContentType, StepResult
from core.runtime.step_result_store import (
    DependencyResultView,
    StepResultStore,
    StepResultStoreError,
)
from core.runtime.trace_contract import (
    RUNTIME_SYNTHESIS_SPAN,
    set_span_attributes,
)
from core.runtime.tracing import (
    activate_span,
    current_trace_context,
    start_span_safely,
)


class MultiAgentDriverErrorCode(str, Enum):
    PLAN_NOT_FROZEN = "PLAN_NOT_FROZEN"
    STEP_NOT_IN_PLAN = "STEP_NOT_IN_PLAN"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    REGISTRY_MISMATCH = "REGISTRY_MISMATCH"
    ADAPTER_NOT_FOUND = "ADAPTER_NOT_FOUND"
    DRIVER_RESULT_MISMATCH = "DRIVER_RESULT_MISMATCH"
    DEPENDENCY_VIEW_FAILED = "DEPENDENCY_VIEW_FAILED"


class MultiAgentDriverError(RuntimeError):
    """Safe Driver error without raw instruction or result text."""

    def __init__(
        self,
        error_code: MultiAgentDriverErrorCode,
        safe_message: str,
    ) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code.value})")


class MultiAgentDriver:
    """Typed multi-step execution driver; sync because the router contract is
    sync and the ParallelExecutor runs it inside the bounded executor."""

    emits_user_output = False

    def __init__(
        self,
        *,
        router,
        coordinator,
        adapter_factory: AgentAdapterFactory,
        registry: AgentRegistry,
        fault_controller=None,
    ) -> None:
        if not isinstance(adapter_factory, AgentAdapterFactory):
            raise TypeError("MultiAgentDriver 需要 AgentAdapterFactory")
        if not isinstance(registry, AgentRegistry):
            raise TypeError("MultiAgentDriver 需要 AgentRegistry")
        self._router = router
        self._coordinator = coordinator
        self._adapter_factory = adapter_factory
        self._registry = registry
        self._fault_controller = fault_controller

    def execute(
        self,
        claim: StepClaim,
        run_context,
    ) -> StepResult:
        from core.runtime.fault_injection import evaluate_sync_fault
        from core.runtime.fault_injection_contract import FaultPoint

        evaluate_sync_fault(
            self._fault_controller,
            point=FaultPoint.STEP_BEFORE_DRIVER_EXECUTE,
            component="multi_agent_driver",
            run_id=getattr(run_context, "run_id", None),
            step_id=claim.step_id,
            operation_kind="DRIVER_EXECUTE",
        )
        plan = self._coordinator.plan
        bindings = self._coordinator.invocation_bindings
        if plan is None or bindings is None:
            raise MultiAgentDriverError(
                MultiAgentDriverErrorCode.PLAN_NOT_FROZEN,
                "Driver 只能在 Plan 冻结后执行",
            )
        plan_step = self._resolve_plan_step(plan, claim.step_id)
        try:
            binding = bindings.resolve_for_step(
                claim.step_id,
                expected_agent_id=claim.preferred_agent,
            )
        except InvocationBindingError as exc:
            raise MultiAgentDriverError(
                MultiAgentDriverErrorCode.BINDING_MISMATCH,
                "Binding 与 claim 不一致",
            ) from None
        try:
            registration = self._registry.resolve(claim.preferred_agent)
        except AgentRegistryError as exc:
            raise MultiAgentDriverError(
                MultiAgentDriverErrorCode.REGISTRY_MISMATCH,
                "Registry 无法解析 claim agent",
            ) from None
        self._verify_triple_identity(
            claim=claim,
            plan_step=plan_step,
            binding_agent_id=binding.agent_id,
            registration=registration,
        )
        adapter: AgentExecutionAdapter = self._adapter_factory.resolve(
            registration.execution_adapter_id
        )
        dependency_view: DependencyResultView | None = None
        if plan_step.execution_kind is ExecutionKind.SYNTHESIS:
            dependency_view = self._resolve_dependency_view(claim)
        instruction = self._build_instruction(plan_step, binding.instruction)
        request = AgentExecutionRequest(
            step_id=claim.step_id,
            agent_id=claim.preferred_agent,
            instruction=instruction,
            invocation_role=binding.role,
            history_policy=binding.history_policy,
            execution_kind=plan_step.execution_kind,
            input_type=binding.input_type,
            capability_requirements=plan_step.capability_requirements,
            content_type=self._runtime_content_type(registration),
            dependency_results=dependency_view,
            event_emitter=(
                self._coordinator.event_emitter.for_step(claim.step_id)
                if self._coordinator.event_emitter is not None
                else None
            ),
            fault_controller=self._fault_controller,
        )
        if (
            plan_step.execution_kind is ExecutionKind.SYNTHESIS
            and getattr(self._coordinator, "span_recorder", None) is not None
        ):
            synthesis_span = start_span_safely(
                self._coordinator.span_recorder,
                trace_id=run_context.trace_id,
                run_id=run_context.run_id,
                component="synthesis",
                operation=RUNTIME_SYNTHESIS_SPAN,
                step_id=claim.step_id,
                parent_context=current_trace_context(),
            )
            with activate_span(synthesis_span):
                adapter_result = adapter.execute(request, run_context)
                set_span_attributes(
                    synthesis_span,
                    state="SUCCEEDED",
                    execution_kind=ExecutionKind.SYNTHESIS.value,
                )
        else:
            adapter_result = adapter.execute(request, run_context)
        if not isinstance(adapter_result, AgentAdapterResult):
            raise MultiAgentDriverError(
                MultiAgentDriverErrorCode.DRIVER_RESULT_MISMATCH,
                "adapter 返回了非法结果对象",
            )
        return adapter_result.to_step_result(
            step_id=claim.step_id,
            producer_agent_id=claim.preferred_agent,
        )

    @staticmethod
    def _resolve_plan_step(plan: Plan, step_id: str) -> PlanStep:
        for step in plan.steps:
            if step.step_id == step_id:
                return step
        raise MultiAgentDriverError(
            MultiAgentDriverErrorCode.STEP_NOT_IN_PLAN,
            "claim step 不属于冻结 Plan",
        )

    @staticmethod
    def _verify_triple_identity(
        *,
        claim: StepClaim,
        plan_step: PlanStep,
        binding_agent_id: str,
        registration: AgentRegistration,
    ) -> None:
        if (
            claim.preferred_agent
            != plan_step.preferred_agent
            != binding_agent_id
            != registration.agent_id
        ):
            raise MultiAgentDriverError(
                MultiAgentDriverErrorCode.REGISTRY_MISMATCH,
                "PlanStep/Binding/Registry 三方 Agent 不一致",
            )

    def _resolve_dependency_view(self, claim: StepClaim) -> DependencyResultView:
        store: StepResultStore | None = self._coordinator.step_result_store
        if store is None:
            raise MultiAgentDriverError(
                MultiAgentDriverErrorCode.DEPENDENCY_VIEW_FAILED,
                "synthesis 需要 StepResultStore",
            )
        try:
            return store.dependency_view_for(
                claim,
                self._coordinator.agent_state,
            )
        except StepResultStoreError as exc:
            raise MultiAgentDriverError(
                MultiAgentDriverErrorCode.DEPENDENCY_VIEW_FAILED,
                "依赖结果视图构建失败",
            ) from None

    def _build_instruction(self, plan_step: PlanStep, binding_instruction: str) -> str:
        if plan_step.execution_kind is not ExecutionKind.SYNTHESIS:
            return binding_instruction
        user_request = self._coordinator.user_request
        if not user_request:
            return binding_instruction
        return f"{binding_instruction}\n\nUser request: {user_request}"

    @staticmethod
    def _runtime_content_type(registration: AgentRegistration) -> ResultContentType:
        from core.runtime.agent_registry import ResultContentType as RegistryContentType

        produced = registration.produced_result_types
        if RegistryContentType.STRUCTURED in produced:
            return ResultContentType.MARKDOWN
        return ResultContentType.TEXT


__all__ = [
    "MultiAgentDriver",
    "MultiAgentDriverError",
    "MultiAgentDriverErrorCode",
]
