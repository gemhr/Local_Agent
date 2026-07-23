#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模型候选链与 Fallback 的纯策略层。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from core.runtime.budget import BudgetPolicy, BudgetSnapshot, BudgetUsage
from core.runtime.circuit_breaker import (
    ModelCircuitBreakerSnapshot,
    ModelCircuitState,
)
from core.runtime.model_context import ModelContextRequirements
from core.runtime.model_selection import (
    ModelPreference,
    ModelProfile,
    ModelProfileId,
    ModelSelectionDecision,
    ModelSelectionPolicy,
)
from core.runtime.planning import TaskCapabilityRequirements


class ModelFailureCategory(str, Enum):
    TRANSIENT_PROVIDER_FAILURE = "TRANSIENT_PROVIDER_FAILURE"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_CONFIGURATION_ERROR = "PROVIDER_CONFIGURATION_ERROR"
    CONTEXT_LIMIT_EXCEEDED = "CONTEXT_LIMIT_EXCEEDED"
    INVALID_REQUEST = "INVALID_REQUEST"
    OUTPUT_VALIDATION_FAILED = "OUTPUT_VALIDATION_FAILED"
    SAFETY_REFUSAL = "SAFETY_REFUSAL"
    BUSINESS_FAILURE = "BUSINESS_FAILURE"
    CANCELLED = "CANCELLED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


class RoutingAdjustment(str, Enum):
    NONE = "NONE"
    ESCALATE_TO_REMOTE = "ESCALATE_TO_REMOTE"
    DOWNGRADE_TO_LOCAL = "DOWNGRADE_TO_LOCAL"
    SWITCH_SAME_TIER = "SWITCH_SAME_TIER"
    REJECT = "REJECT"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"


@dataclass(frozen=True, slots=True)
class ModelRoutingCandidate:
    profile: ModelProfile
    breaker_key: str
    adjustment: RoutingAdjustment
    reason_code: str
    breaker_open_at_decision: bool = False

    @property
    def profile_id(self) -> ModelProfileId:
        return self.profile.profile_id


@dataclass(frozen=True, slots=True)
class ModelRoutingDecision:
    capability_preferred_profile_id: ModelProfileId | None
    initial_selected_profile_id: ModelProfileId | None
    candidates: tuple[ModelRoutingCandidate, ...]
    required_context_window: int
    quality_tradeoff_disclosed: bool = False
    confirmation_required: bool = False
    reason_code: str = "ROUTING_READY"


class ModelRoutingError(ValueError):
    """不携带业务正文的路由策略错误。"""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"模型路由失败：{reason_code}")


_DEFAULT_FALLBACK = frozenset(
    {
        ModelFailureCategory.TRANSIENT_PROVIDER_FAILURE,
        ModelFailureCategory.PROVIDER_TIMEOUT,
        ModelFailureCategory.RATE_LIMITED,
    }
)


class ModelRoutingPolicy:
    """只读输入并产生稳定、去重的 Profile 候选链。"""

    def __init__(
        self,
        *,
        allow_escalation: bool = True,
        allow_downgrade: bool = True,
        require_confirmation_for_downgrade: bool = False,
        context_window_safety_ratio: float = 1.1,
    ) -> None:
        self.allow_escalation = allow_escalation
        self.allow_downgrade = allow_downgrade
        self.require_confirmation_for_downgrade = (
            require_confirmation_for_downgrade
        )
        self._selection_policy = ModelSelectionPolicy(
            context_window_safety_ratio=context_window_safety_ratio
        )

    def route(
        self,
        *,
        selection_decision: ModelSelectionDecision,
        capability_requirements: TaskCapabilityRequirements,
        context_requirements: ModelContextRequirements,
        profiles: tuple[ModelProfile, ...],
        preference: ModelPreference,
        budget_snapshot: BudgetSnapshot | None = None,
        breaker_snapshots: Mapping[str, ModelCircuitBreakerSnapshot] | None = None,
    ) -> ModelRoutingDecision:
        initial = selection_decision.selected_profile_id
        if selection_decision.confirmation_required or initial is None:
            return ModelRoutingDecision(
                selection_decision.capability_preferred_profile_id,
                None,
                (),
                self._selection_policy.required_context_window(
                    context_requirements.minimum_context_window
                ),
                confirmation_required=True,
                reason_code="ROUTING_CONFIRMATION_REQUIRED",
            )
        profile_map = {profile.profile_id: profile for profile in profiles}
        if len(profile_map) != len(profiles):
            raise ModelRoutingError("DUPLICATE_PROFILE_ID")
        initial_profile = profile_map.get(initial)
        if initial_profile is None:
            raise ModelRoutingError("INITIAL_PROFILE_UNAVAILABLE")
        required_context = self._selection_policy.required_context_window(
            context_requirements.minimum_context_window
        )
        ordered = (initial_profile,) + tuple(
            profile for profile in profiles if profile.profile_id != initial
        )
        candidates: list[ModelRoutingCandidate] = []
        downgrade_seen = False
        snapshots = breaker_snapshots or {}
        for profile in ordered:
            if not self._preference_allows(preference, profile):
                continue
            if self._missing(profile, capability_requirements, required_context):
                continue
            adjustment = self._adjustment(initial_profile, profile)
            if (
                adjustment == RoutingAdjustment.ESCALATE_TO_REMOTE
                and not self.allow_escalation
            ):
                continue
            if (
                adjustment == RoutingAdjustment.DOWNGRADE_TO_LOCAL
                and not self.allow_downgrade
            ):
                continue
            if budget_snapshot is not None and not self._budget_feasible(
                budget_snapshot, profile, context_requirements.estimated_input_tokens
            ):
                continue
            if adjustment == RoutingAdjustment.DOWNGRADE_TO_LOCAL:
                downgrade_seen = True
            breaker_key = profile.effective_breaker_key
            snapshot = snapshots.get(breaker_key)
            candidates.append(
                ModelRoutingCandidate(
                    profile,
                    breaker_key,
                    adjustment,
                    self._reason_code(adjustment),
                    snapshot is not None
                    and snapshot.state == ModelCircuitState.OPEN,
                )
            )
        if not candidates:
            raise ModelRoutingError("NO_ROUTING_CANDIDATE")
        if downgrade_seen and self.require_confirmation_for_downgrade:
            return ModelRoutingDecision(
                selection_decision.capability_preferred_profile_id,
                initial,
                (),
                required_context,
                quality_tradeoff_disclosed=True,
                confirmation_required=True,
                reason_code="DOWNGRADE_CONFIRMATION_REQUIRED",
            )
        return ModelRoutingDecision(
            selection_decision.capability_preferred_profile_id,
            initial,
            tuple(candidates),
            required_context,
            quality_tradeoff_disclosed=(
                selection_decision.quality_tradeoff_disclosed or downgrade_seen
            ),
        )

    @staticmethod
    def can_fallback(
        category: ModelFailureCategory,
        *,
        failed_profile: ModelProfile,
        next_profile: ModelProfile,
        output_started: bool,
    ) -> bool:
        if output_started:
            return False
        if category in _DEFAULT_FALLBACK:
            return True
        if category == ModelFailureCategory.CIRCUIT_OPEN:
            return True
        if category == ModelFailureCategory.CONTEXT_LIMIT_EXCEEDED:
            return next_profile.context_window > failed_profile.context_window
        return False

    @staticmethod
    def _preference_allows(
        preference: ModelPreference, profile: ModelProfile
    ) -> bool:
        if preference == ModelPreference.FORCE_LOCAL:
            return not profile.effective_is_remote
        if preference == ModelPreference.FORCE_REMOTE:
            return profile.effective_is_remote
        return True

    @staticmethod
    def _adjustment(
        initial: ModelProfile, candidate: ModelProfile
    ) -> RoutingAdjustment:
        if candidate.profile_id == initial.profile_id:
            return RoutingAdjustment.NONE
        if not initial.effective_is_remote and candidate.effective_is_remote:
            return RoutingAdjustment.ESCALATE_TO_REMOTE
        if initial.effective_is_remote and not candidate.effective_is_remote:
            return RoutingAdjustment.DOWNGRADE_TO_LOCAL
        return RoutingAdjustment.SWITCH_SAME_TIER

    @staticmethod
    def _reason_code(adjustment: RoutingAdjustment) -> str:
        return {
            RoutingAdjustment.NONE: "INITIAL_SELECTION",
            RoutingAdjustment.ESCALATE_TO_REMOTE: "PROVIDER_FAILURE_ESCALATION",
            RoutingAdjustment.DOWNGRADE_TO_LOCAL: "PROVIDER_FAILURE_DOWNGRADE",
            RoutingAdjustment.SWITCH_SAME_TIER: "PROVIDER_FAILURE_SAME_TIER_SWITCH",
        }[adjustment]

    @staticmethod
    def _missing(
        profile: ModelProfile,
        requirements: TaskCapabilityRequirements,
        required_context: int,
    ) -> tuple[str, ...]:
        missing = []
        if profile.context_window < required_context:
            missing.append("context_window")
        pairs = (
            ("requires_tools", "supports_tools"),
            ("requires_structured_output", "supports_structured_output"),
            ("requires_code_reasoning", "supports_code_reasoning"),
            ("requires_long_reasoning", "supports_long_reasoning"),
        )
        for required_name, supported_name in pairs:
            if getattr(requirements, required_name) and not getattr(
                profile, supported_name
            ):
                missing.append(required_name)
        return tuple(missing)

    @staticmethod
    def _budget_feasible(
        snapshot: BudgetSnapshot, profile: ModelProfile, input_tokens: int
    ) -> bool:
        metadata = profile.cost_profile
        if metadata is None and (
            snapshot.run_budget.max_cost_units is not None
            or snapshot.run_budget.max_remote_model_calls is not None
        ):
            return False
        output_tokens = profile.max_output_tokens
        cost_units = 1
        if metadata is not None:
            cost_units = (
                metadata.fixed_call_cost_units
                + (input_tokens * metadata.input_cost_units_per_1k_tokens + 999)
                // 1000
                + (
                    output_tokens * metadata.output_cost_units_per_1k_tokens + 999
                )
                // 1000
            )
        return BudgetPolicy.feasible(
            snapshot,
            BudgetUsage(
                model_calls=1,
                remote_model_calls=int(profile.effective_is_remote),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                cost_units=cost_units,
            ),
        )
