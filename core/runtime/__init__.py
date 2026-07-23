#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LocalAgent 的最小运行时上下文基础组件。"""

from core.runtime.cancellation import CancellationSource, CancellationToken, RunCancelledError

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
from core.runtime.model_selection import (ModelPreference, ModelProfile, ModelProfileId, ModelResolver, ModelSelectionDecision, ModelSelectionError, ModelSelectionPolicy, ModelSelectionReason, ModelSelectionRequest)
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
    "ContextBuilder", "ContextBuildRequest", "ContextBuildResult", "ContextBudgetExceededError",
    "ContextDropRecord", "ContextItem", "ContextSourceType", "ContextStats", "ContextTrustLevel",
    "DeterministicTokenEstimator", "ModelContextRequirements", "TokenEstimator",
    "Plan", "PlanSource", "PlanStep", "PlanValidator", "RiskLevel", "TaskCapabilityRequirements", "create_single_step_plan",
    "PlanGraph", "PlanGraphValidationError", "PlanGraphValidator",
    "ModelPreference", "ModelProfile", "ModelProfileId", "ModelResolver", "ModelSelectionDecision", "ModelSelectionError", "ModelSelectionPolicy", "ModelSelectionReason", "ModelSelectionRequest",
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
    "RunContextData",
    "RunDeadlineExceededError",
    "RunIdentifiers",
    "RunStatus",
    "RunEventType",
    "RunStateEvent",
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
