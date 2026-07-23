#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""确定性模型 Profile 选择与无副作用 Resolver。"""

from __future__ import annotations
from dataclasses import dataclass, replace
from enum import Enum
from math import ceil, floor
from typing import Mapping, Protocol

from core.runtime.model_context import ModelContextRequirements
from core.runtime.budget import BudgetPolicy, BudgetSnapshot, BudgetUsage
from core.runtime.planning import RiskLevel, TaskCapabilityRequirements


class ModelProfileId(str, Enum):
    LOCAL_FAST = "local_fast"
    REMOTE_ADVANCED = "remote_advanced"

class ModelPreference(str, Enum):
    AUTO = "auto"
    FORCE_LOCAL = "force_local"
    FORCE_REMOTE = "force_remote"

class ModelSelectionObjective(str, Enum):
    SPEED_FIRST = "speed_first"
    QUALITY_FIRST = "quality_first"
    COST_FIRST = "cost_first"

class BudgetInsufficientAction(str, Enum):
    DEGRADE = "degrade"
    REJECT = "reject"
    REQUIRE_CONFIRMATION = "require_confirmation"

class BudgetAdjustment(str, Enum):
    NONE = "none"
    UPGRADE_TO_REMOTE = "upgrade_to_remote"
    DOWNGRADE_TO_LOCAL = "downgrade_to_local"
    REJECT = "reject"
    REQUIRE_CONFIRMATION = "require_confirmation"

@dataclass(frozen=True, slots=True)
class ModelCostProfile:
    """由 Settings/Profile 装配的整数成本配置，非真实货币价格。"""
    profile_id: ModelProfileId; is_remote: bool; fixed_call_cost_units: int = 0; input_cost_units_per_1k_tokens: int = 0; output_cost_units_per_1k_tokens: int = 0; estimated_latency_ms: int = 0
    def __post_init__(self):
        for name in ("fixed_call_cost_units", "input_cost_units_per_1k_tokens", "output_cost_units_per_1k_tokens", "estimated_latency_ms"):
            value=getattr(self,name)
            if isinstance(value,bool) or not isinstance(value,int) or value<0: raise ValueError(f"{name} 必须是非负整数")

class ModelSelectionReason(str, Enum):
    USER_FORCED_LOCAL = "user_forced_local"; USER_FORCED_REMOTE = "user_forced_remote"; LOCAL_SUFFICIENT = "local_sufficient"
    CONTEXT_WINDOW_REQUIRED = "context_window_required"; TOOL_CAPABILITY_REQUIRED = "tool_capability_required"; STRUCTURED_OUTPUT_REQUIRED = "structured_output_required"
    CODE_REASONING_REQUIRED = "code_reasoning_required"; LONG_REASONING_REQUIRED = "long_reasoning_required"; MULTI_STEP_PLAN = "multi_step_plan"
    MULTI_AGENT_REQUIRED = "multi_agent_required"; HIGH_RISK_TASK = "high_risk_task"

@dataclass(frozen=True, slots=True)
class ModelProfile:
    profile_id: ModelProfileId; context_window: int; max_output_tokens: int
    supports_tools: bool; supports_structured_output: bool; supports_code_reasoning: bool; supports_long_reasoning: bool
    quality_tier: int; latency_tier: int
    cost_profile: ModelCostProfile | None = None
    def __post_init__(self) -> None:
        for name in ("context_window", "max_output_tokens", "quality_tier", "latency_tier"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0: raise ValueError(f"{name} 必须是正整数")
        if self.cost_profile is not None and self.cost_profile.profile_id != self.profile_id: raise ValueError("cost_profile.profile_id 必须匹配 profile_id")
        for name in ("supports_tools", "supports_structured_output", "supports_code_reasoning", "supports_long_reasoning"):
            if type(getattr(self, name)) is not bool: raise ValueError(f"{name} 必须是 bool")

@dataclass(frozen=True, slots=True)
class ModelSelectionRequest:
    agent_id: str; capability_requirements: TaskCapabilityRequirements; context_requirements: ModelContextRequirements
    preference: ModelPreference; available_profiles: tuple[ModelProfile, ...]
    budget_snapshot: BudgetSnapshot | None = None
    selection_objective: ModelSelectionObjective = ModelSelectionObjective.QUALITY_FIRST
    budget_insufficient_action: BudgetInsufficientAction = BudgetInsufficientAction.REJECT

@dataclass(frozen=True, slots=True)
class ModelSelectionDecision:
    selected_profile: ModelProfileId; reason_code: ModelSelectionReason; reason_text: str; matched_rules: tuple[str, ...]; fallback_allowed: bool = False
    capability_preferred_profile_id: ModelProfileId | None = None
    selected_profile_id: ModelProfileId | None = None
    selection_objective: ModelSelectionObjective = ModelSelectionObjective.QUALITY_FIRST
    budget_adjustment: BudgetAdjustment = BudgetAdjustment.NONE
    budget_reason_code: str | None = None
    estimated_cost_units: int = 0
    estimated_latency_ms: int = 0
    quality_tradeoff_disclosed: bool = False
    confirmation_required: bool = False

class ModelSelectionError(ValueError):
    """只携带安全的选择失败元数据。"""
    def __init__(self, reason_code: str, *, requested_profile: ModelProfileId | None = None, missing_capabilities: tuple[str, ...] = (), required_context_window: int = 0, available_context_window: int = 0) -> None:
        self.reason_code, self.requested_profile, self.missing_capabilities = reason_code, requested_profile, missing_capabilities
        self.required_context_window, self.available_context_window = required_context_window, available_context_window
        super().__init__(f"模型选择失败：{reason_code}")

class ModelSelectionPolicy:
    """按固定优先级选择一个模型，绝不执行 fallback。"""
    def __init__(self, *, context_window_safety_ratio: float = 1.1, multi_step_threshold: int = 3) -> None:
        if not isinstance(context_window_safety_ratio, (int, float)) or isinstance(context_window_safety_ratio, bool) or context_window_safety_ratio < 1: raise ValueError("context_window_safety_ratio 必须不小于 1")
        self.context_window_safety_ratio, self.multi_step_threshold = float(context_window_safety_ratio), multi_step_threshold

    def select(self, request: ModelSelectionRequest) -> ModelSelectionDecision:
        profiles = self._validate_profiles(request.available_profiles)
        final_required = self.required_context_window(request.context_requirements.minimum_context_window)
        raw_minimum = request.context_requirements.raw_minimum_context_window or request.context_requirements.minimum_context_window
        raw_required = self.required_context_window(raw_minimum)
        if request.preference != ModelPreference.AUTO:
            profile_id = ModelProfileId.LOCAL_FAST if request.preference == ModelPreference.FORCE_LOCAL else ModelProfileId.REMOTE_ADVANCED
            profile = profiles.get(profile_id)
            if profile is None: raise ModelSelectionError("requested_profile_unavailable", requested_profile=profile_id)
            missing = self._missing(profile, request.capability_requirements, final_required)
            if missing: raise ModelSelectionError("forced_profile_constraints_unmet", requested_profile=profile_id, missing_capabilities=tuple(missing), required_context_window=final_required, available_context_window=profile.context_window)
            self._require_metadata_if_needed(request, profile)
            if request.budget_snapshot is not None and not BudgetPolicy.feasible(request.budget_snapshot, self._estimated_usage(profile, request.context_requirements.minimum_context_window)):
                raise ModelSelectionError("forced_remote_budget_unavailable" if profile_id == ModelProfileId.REMOTE_ADVANCED else "forced_profile_budget_unavailable", requested_profile=profile_id)
            reason = ModelSelectionReason.USER_FORCED_LOCAL if profile_id == ModelProfileId.LOCAL_FAST else ModelSelectionReason.USER_FORCED_REMOTE
            return self._decision(profile_id, reason, request.context_requirements.was_truncated)
        eligible = [p for p in profiles.values() if not self._missing(p, request.capability_requirements, final_required)]
        if request.selection_objective == ModelSelectionObjective.COST_FIRST:
            for profile in eligible: self._require_metadata_if_needed(request, profile)
        if request.budget_snapshot is not None:
            for profile in eligible: self._require_metadata_if_needed(request, profile)
            feasible = [p for p in eligible if BudgetPolicy.feasible(request.budget_snapshot, self._estimated_usage(p, request.context_requirements.minimum_context_window))]
            preferred_remote = profiles.get(ModelProfileId.REMOTE_ADVANCED) in eligible and self._requires_remote(request.capability_requirements, profiles.get(ModelProfileId.LOCAL_FAST), raw_required)
            if not feasible:
                raise ModelSelectionError("budget_unavailable")
            if preferred_remote and profiles.get(ModelProfileId.REMOTE_ADVANCED) not in feasible:
                local = profiles.get(ModelProfileId.LOCAL_FAST)
                if local in feasible and request.budget_insufficient_action == BudgetInsufficientAction.DEGRADE:
                    return self._budget_decision(local, ModelProfileId.REMOTE_ADVANCED, request, BudgetAdjustment.DOWNGRADE_TO_LOCAL, True)
                if request.budget_insufficient_action == BudgetInsufficientAction.REQUIRE_CONFIRMATION:
                    return ModelSelectionDecision(None, ModelSelectionReason.MULTI_AGENT_REQUIRED, "预算不足，需要确认。", ("budget_confirmation_required",), False, ModelProfileId.REMOTE_ADVANCED, None, request.selection_objective, BudgetAdjustment.REQUIRE_CONFIRMATION, "REMOTE_BUDGET_INSUFFICIENT", 0, 0, False, True)
                raise ModelSelectionError("budget_unavailable")
            eligible = feasible
            if request.selection_objective == ModelSelectionObjective.SPEED_FIRST: eligible.sort(key=lambda p: self._latency(p))
            elif request.selection_objective == ModelSelectionObjective.COST_FIRST: eligible.sort(key=lambda p: self._cost(p, request.context_requirements.minimum_context_window))
            else: eligible.sort(key=lambda p: -p.quality_tier)
        if not eligible: raise ModelSelectionError("no_profile_satisfies_constraints", required_context_window=final_required, available_context_window=max(p.context_window for p in profiles.values()))
        local = profiles.get(ModelProfileId.LOCAL_FAST); remote = profiles.get(ModelProfileId.REMOTE_ADVANCED)
        if local not in eligible and remote in eligible:
            return self._decision(remote.profile_id, self._first_missing_reason(local, request.capability_requirements, final_required), request.context_requirements.was_truncated)
        req = request.capability_requirements
        if remote in eligible:
            if local in eligible and local.context_window < raw_required:
                return self._decision(remote.profile_id, ModelSelectionReason.CONTEXT_WINDOW_REQUIRED, request.context_requirements.was_truncated)
            if req.requires_multi_agent: return self._decision(remote.profile_id, ModelSelectionReason.MULTI_AGENT_REQUIRED)
            if req.requires_long_reasoning: return self._decision(remote.profile_id, ModelSelectionReason.LONG_REASONING_REQUIRED)
            if req.estimated_steps >= self.multi_step_threshold: return self._decision(remote.profile_id, ModelSelectionReason.MULTI_STEP_PLAN)
            if req.risk_level == RiskLevel.HIGH: return self._decision(remote.profile_id, ModelSelectionReason.HIGH_RISK_TASK)
        if local in eligible: return self._decision(local.profile_id, ModelSelectionReason.LOCAL_SUFFICIENT, request.context_requirements.was_truncated)
        return self._decision(eligible[0].profile_id, ModelSelectionReason.LOCAL_SUFFICIENT, request.context_requirements.was_truncated)

    @staticmethod
    def _requires_remote(req: TaskCapabilityRequirements, local: ModelProfile | None, raw_required: int) -> bool:
        return local is None or local.context_window < raw_required or req.requires_multi_agent or req.requires_long_reasoning or req.estimated_steps >= 3 or req.risk_level == RiskLevel.HIGH

    @staticmethod
    def _require_metadata_if_needed(request: ModelSelectionRequest, profile: ModelProfile) -> None:
        snapshot = request.budget_snapshot
        needed = request.selection_objective == ModelSelectionObjective.COST_FIRST or (snapshot is not None and (snapshot.run_budget.max_remote_model_calls is not None or snapshot.run_budget.max_cost_units is not None))
        if needed and profile.cost_profile is None:
            raise ModelSelectionError("MODEL_BUDGET_METADATA_MISSING", requested_profile=profile.profile_id)

    def _budget_decision(self, selected: ModelProfile, preferred: ModelProfileId, request: ModelSelectionRequest, adjustment: BudgetAdjustment, disclosed: bool) -> ModelSelectionDecision:
        return ModelSelectionDecision(selected.profile_id, ModelSelectionReason.MULTI_AGENT_REQUIRED, "远程模型预算不足，已选择满足硬能力的本地模型。", ("budget_downgrade",), False, preferred, selected.profile_id, request.selection_objective, adjustment, "REMOTE_BUDGET_INSUFFICIENT", self._cost(selected, request.context_requirements.minimum_context_window), self._latency(selected), disclosed, False)

    @staticmethod
    def _cost(profile: ModelProfile, input_tokens: int) -> int:
        cost = profile.cost_profile
        return 0 if cost is None else cost.fixed_call_cost_units + (input_tokens * cost.input_cost_units_per_1k_tokens + 999) // 1000

    @staticmethod
    def _latency(profile: ModelProfile) -> int:
        return profile.cost_profile.estimated_latency_ms if profile.cost_profile else profile.latency_tier

    def _estimated_usage(self, profile: ModelProfile, input_tokens: int) -> BudgetUsage:
        output = profile.max_output_tokens
        return BudgetUsage(model_calls=1, remote_model_calls=int(profile.cost_profile.is_remote) if profile.cost_profile else 0, input_tokens=input_tokens, output_tokens=output, total_tokens=input_tokens + output, cost_units=self._cost(profile, input_tokens))

    def _validate_profiles(self, profiles: tuple[ModelProfile, ...]) -> dict[ModelProfileId, ModelProfile]:
        if not profiles: raise ModelSelectionError("no_available_profiles")
        mapped = {p.profile_id: p for p in profiles}
        if len(mapped) != len(profiles): raise ModelSelectionError("duplicate_profile_id")
        return mapped
    def required_context_window(self, minimum_context_window: int) -> int:
        """计算已含输出预算的安全窗口需求。"""
        return ceil(minimum_context_window * self.context_window_safety_ratio)
    def maximum_safe_context_window(self, context_window: int) -> int:
        """计算指定 Profile 可安全承载的最大总窗口。"""
        return floor(context_window / self.context_window_safety_ratio)
    @staticmethod
    def _missing(profile: ModelProfile, req: TaskCapabilityRequirements, required_context: int) -> list[str]:
        missing = []
        if profile.context_window < required_context: missing.append("context_window")
        for attr, name in (("requires_tools", "tools"), ("requires_structured_output", "structured_output"), ("requires_code_reasoning", "code_reasoning"), ("requires_long_reasoning", "long_reasoning")):
            if getattr(req, attr) and not getattr(profile, f"supports_{name}"): missing.append(name)
        return missing
    def _first_missing_reason(self, local: ModelProfile | None, req: TaskCapabilityRequirements, required_context: int) -> ModelSelectionReason:
        if local is None or local.context_window < required_context: return ModelSelectionReason.CONTEXT_WINDOW_REQUIRED
        if req.requires_tools and not local.supports_tools: return ModelSelectionReason.TOOL_CAPABILITY_REQUIRED
        if req.requires_structured_output and not local.supports_structured_output: return ModelSelectionReason.STRUCTURED_OUTPUT_REQUIRED
        if req.requires_code_reasoning and not local.supports_code_reasoning: return ModelSelectionReason.CODE_REASONING_REQUIRED
        return ModelSelectionReason.LONG_REASONING_REQUIRED
    @staticmethod
    def _decision(profile_id: ModelProfileId, reason: ModelSelectionReason, was_truncated: bool = False) -> ModelSelectionDecision:
        texts = {ModelSelectionReason.LOCAL_SUFFICIENT: "当前上下文和能力需求均满足，选择本地轻量模型。", ModelSelectionReason.CONTEXT_WINDOW_REQUIRED: "完整上下文需要更大窗口，因此选择远程高级模型。", ModelSelectionReason.TOOL_CAPABILITY_REQUIRED: "本地模型不满足工具能力，因此选择远程高级模型。", ModelSelectionReason.STRUCTURED_OUTPUT_REQUIRED: "本地模型不满足结构化输出能力，因此选择远程高级模型。", ModelSelectionReason.CODE_REASONING_REQUIRED: "本地模型不满足代码推理能力，因此选择远程高级模型。", ModelSelectionReason.LONG_REASONING_REQUIRED: "任务需要长推理，因此选择远程高级模型。", ModelSelectionReason.MULTI_STEP_PLAN: "任务步骤较多，因此选择远程高级模型。", ModelSelectionReason.MULTI_AGENT_REQUIRED: "任务需要多智能体协作，因此选择远程高级模型。", ModelSelectionReason.HIGH_RISK_TASK: "任务风险较高，因此选择远程高级模型。", ModelSelectionReason.USER_FORCED_LOCAL: "用户强制要求使用本地模型。", ModelSelectionReason.USER_FORCED_REMOTE: "用户强制要求使用远程模型。"}
        rules = (reason.value, "context_truncated") if was_truncated else (reason.value,)
        return ModelSelectionDecision(profile_id, reason, texts[reason], rules, False, profile_id, profile_id)

class ModelResolver:
    """只把 Profile 映射到已有模型对象，不执行策略或切换。"""
    def __init__(self, engines: Mapping[ModelProfileId, object]) -> None: self._engines = dict(engines)
    def resolve(self, profile_id: ModelProfileId) -> object:
        try: return self._engines[profile_id]
        except KeyError as exc: raise ModelSelectionError("resolved_profile_unavailable", requested_profile=profile_id) from exc
