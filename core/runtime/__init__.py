#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LocalAgent 的最小运行时上下文基础组件。"""

from core.runtime.cancellation import CancellationReason, CancellationSource, CancellationToken, RunCancelledError
from core.runtime.run_registry import RunHandle, RunRegistry, process_run_registry
from core.runtime.timeout import OperationTimeoutError, OperationType, effective_timeout_seconds

from core.runtime.state import (
    AGENT_STATE_SCHEMA_VERSION,
    AgentState,
    AgentStateValidationError,
    RunStatus,
    StepState,
    StepStatus,
    StopReason,
    UnsupportedStateVersionError,
)
from core.runtime.context import (
    Clock,
    Deadline,
    LEGACY_DEFAULT_SESSION_ID,
    RunContext,
    RunContextData,
    RunDeadlineExceededError,
    RunIdentifiers,
    SystemClock,
    create_run_context,
)
from core.runtime.agent_loop import (
    ActionOutcome,
    AgentAction,
    AgentLoop,
    AgentLoopDriver,
    AgentLoopPolicy,
    AgentObservation,
    LEGACY_AGENT_ROUTER_STEP_ID,
    LEGACY_AGENT_ROUTER_STEP_NAME,
    LegacyAgentRouterDriver,
)
from core.runtime.model_context import (
    ContextBuilder, ContextBuildRequest, ContextBuildResult, ContextBudgetExceededError, ContextDropRecord,
    ContextItem, ContextSourceType, ContextStats, ContextTrustLevel, DeterministicTokenEstimator,
    ModelContextRequirements, TokenEstimator,
)
from core.runtime.planning import Plan, PlanSource, PlanStep, PlanValidator, RiskLevel, TaskCapabilityRequirements, create_single_step_plan
from core.runtime.plan_graph import PlanGraph, PlanGraphValidationError, PlanGraphValidator
from core.runtime.model_selection import (ModelCostProfile, ModelPreference, ModelProfile, ModelProfileId, ModelResolver, ModelSelectionDecision, ModelSelectionError, ModelSelectionObjective, ModelSelectionPolicy, ModelSelectionReason, ModelSelectionRequest)
from core.runtime.state_machine import (
    AgentStateMachine,
    InvalidStateTransitionError,
    RunEventType,
    RunStateEvent,
    StepEventType,
    StepStateEvent,
)
from core.runtime.scheduler import (
    SchedulerClaimError,
    SchedulerError,
    SchedulerPlanStateMismatchError,
    SchedulerSnapshot,
    SerialScheduler,
    StepClaim,
)
from core.runtime.run_coordinator import (
    RunCoordinator,
    RunCoordinatorError,
    RunCoordinatorResult,
    RunFinalizationDecision,
)

__all__ = [
    "AGENT_STATE_SCHEMA_VERSION",
    "ActionOutcome",
    "AgentAction",
    "AgentLoop",
    "AgentLoopDriver",
    "AgentLoopPolicy",
    "AgentObservation",
    "AgentState",
    "AgentStateMachine",
    "AgentStateValidationError",
    "CancellationSource",
    "CancellationReason", "RunHandle", "RunRegistry", "process_run_registry",
    "OperationTimeoutError", "OperationType", "effective_timeout_seconds",
    "ContextBuilder", "ContextBuildRequest", "ContextBuildResult", "ContextBudgetExceededError",
    "ContextDropRecord", "ContextItem", "ContextSourceType", "ContextStats", "ContextTrustLevel",
    "DeterministicTokenEstimator", "ModelContextRequirements", "TokenEstimator",
    "Plan", "PlanSource", "PlanStep", "PlanValidator", "RiskLevel", "TaskCapabilityRequirements", "create_single_step_plan",
    "PlanGraph", "PlanGraphValidationError", "PlanGraphValidator",
    "ModelCostProfile", "ModelPreference", "ModelProfile", "ModelProfileId", "ModelResolver", "ModelSelectionDecision", "ModelSelectionError", "ModelSelectionObjective", "ModelSelectionPolicy", "ModelSelectionReason", "ModelSelectionRequest",
    "CancellationToken",
    "Clock",
    "Deadline",
    "LEGACY_DEFAULT_SESSION_ID",
    "LEGACY_AGENT_ROUTER_STEP_ID",
    "LEGACY_AGENT_ROUTER_STEP_NAME",
    "LegacyAgentRouterDriver",
    "InvalidStateTransitionError",
    "RunCancelledError",
    "RunContext",
    "RunCoordinator",
    "RunCoordinatorError",
    "RunCoordinatorResult",
    "RunContextData",
    "RunDeadlineExceededError",
    "RunIdentifiers",
    "RunStatus",
    "RunEventType",
    "RunStateEvent",
    "RunFinalizationDecision",
    "SchedulerClaimError",
    "SchedulerError",
    "SchedulerPlanStateMismatchError",
    "SchedulerSnapshot",
    "SerialScheduler",
    "StepClaim",
    "StepEventType",
    "StepState",
    "StepStateEvent",
    "StepStatus",
    "StopReason",
    "UnsupportedStateVersionError",
    "SystemClock",
    "create_run_context",
]

from core.runtime.parallel_execution import (
    ParallelExecutionInfrastructureError, ParallelExecutionReport, ParallelExecutor,
    ParallelFailureMode, ParallelExecutionPolicy, StepConcurrencySpec, StepExecutionDriver, StepExecutionMode,
    StepExecutionOutcome,
)

__all__ += [
    "ParallelExecutionInfrastructureError", "ParallelExecutionReport", "ParallelExecutor",
    "ParallelFailureMode", "ParallelExecutionPolicy", "StepConcurrencySpec", "StepExecutionDriver", "StepExecutionMode",
    "StepExecutionOutcome",
]
from core.runtime.budget import BudgetedModelStream, BudgetExceededError, BudgetLedger, BudgetPolicy, BudgetReservation, BudgetReservationError, BudgetSnapshot, BudgetUsage, RunBudget, UsageSource

__all__ += [
    "BudgetedModelStream",
    "BudgetExceededError",
    "BudgetLedger",
    "BudgetPolicy",
    "BudgetReservation",
    "BudgetReservationError",
    "BudgetSnapshot",
    "BudgetUsage",
    "RunBudget",
    "UsageSource",
]

from core.runtime.circuit_breaker import (
    CircuitOpenError,
    CircuitPermit,
    CircuitPermitStateError,
    ModelCircuitBreaker,
    ModelCircuitBreakerConfig,
    ModelCircuitBreakerRegistry,
    ModelCircuitBreakerSnapshot,
    ModelCircuitState,
)
from core.runtime.model_routing import (
    ModelFailureCategory,
    ModelRoutingCandidate,
    ModelRoutingDecision,
    ModelRoutingError,
    ModelRoutingPolicy,
    RoutingAdjustment,
)
from core.runtime.model_invocation import (
    CircuitHealthOutcome,
    GeneratorModelAdapter,
    ModelAdapter,
    ModelAdapterInvocationError,
    ModelAdapterResolutionError,
    ModelAdapterResolver,
    ModelAdapterResponse,
    ModelInvocationAttempt,
    ModelInvocationChainError,
    ModelInvocationConfirmationRequired,
    ModelInvocationFailure,
    ModelInvocationResult,
    ModelInvocationRouter,
    ModelUsageSource,
    classify_model_failure,
)

__all__ += [
    "CircuitHealthOutcome",
    "CircuitOpenError",
    "CircuitPermit",
    "CircuitPermitStateError",
    "GeneratorModelAdapter",
    "ModelAdapter",
    "ModelAdapterInvocationError",
    "ModelAdapterResolutionError",
    "ModelAdapterResolver",
    "ModelAdapterResponse",
    "ModelCircuitBreaker",
    "ModelCircuitBreakerConfig",
    "ModelCircuitBreakerRegistry",
    "ModelCircuitBreakerSnapshot",
    "ModelCircuitState",
    "ModelFailureCategory",
    "ModelInvocationAttempt",
    "ModelInvocationChainError",
    "ModelInvocationConfirmationRequired",
    "ModelInvocationFailure",
    "ModelInvocationResult",
    "ModelInvocationRouter",
    "ModelRoutingCandidate",
    "ModelRoutingDecision",
    "ModelRoutingError",
    "ModelRoutingPolicy",
    "ModelUsageSource",
    "RoutingAdjustment",
    "classify_model_failure",
]
