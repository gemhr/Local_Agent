#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stage 2.5 typed planning、严格解析与未接线 Resolver 合同。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
import json
import re
from typing import Mapping, Protocol, TypeAlias, runtime_checkable

from core.runtime.agent_registry import AgentRegistry, AgentRegistryError
from core.runtime.budget import BudgetExceededError
from core.runtime.cancellation import RunCancelledError
from core.runtime.context import RunContext
from core.runtime.context import RunDeadlineExceededError
from core.runtime.invocation_bindings import InvocationBindingError, StepInvocationBindings
from core.runtime.plan_graph import PlanGraphValidator
from core.runtime.planning import Plan

PLANNER_SCHEMA_VERSION = 1
_SAFE_REASON = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_DOCUMENT_REFERENCE = re.compile(
    r"(?:总结|讲讲|查找|查询|检索|搜索|阅读|分析).{0,120}\.(?:md|txt|pdf|docx?|csv|xlsx)\b",
    re.IGNORECASE,
)
_DELEGATION_VERB = re.compile(r"(?:调用|使用|请让|让|交给|委派|查询|查)")
_DIRECT_GREETING = re.compile(r"^(?:你好|您好|嗨|hi|hello)[!！,.，。\s]*$", re.IGNORECASE)


class PlanningSource(str, Enum):
    EXPLICIT_ENTRY = "EXPLICIT_ENTRY"
    DETERMINISTIC_RULE = "DETERMINISTIC_RULE"
    MODEL = "MODEL"


class PlanningErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    PLANNING_MODEL_REQUIRED = "PLANNING_MODEL_REQUIRED"
    PLANNING_MODEL_FAILED = "PLANNING_MODEL_FAILED"
    PLANNER_TIMEOUT = "PLANNER_TIMEOUT"
    PLANNER_SCHEMA_INVALID = "PLANNER_SCHEMA_INVALID"
    PLANNER_SCHEMA_VERSION_UNSUPPORTED = "PLANNER_SCHEMA_VERSION_UNSUPPORTED"
    PLANNER_DECISION_UNKNOWN = "PLANNER_DECISION_UNKNOWN"
    PLANNER_FIELD_FORBIDDEN = "PLANNER_FIELD_FORBIDDEN"
    OPTIONAL_DEPENDENCY_UNSUPPORTED = "OPTIONAL_DEPENDENCY_UNSUPPORTED"


class PlanningError(RuntimeError):
    """Resolver/Parser 的安全 typed 错误；不保存模型原文或用户正文。"""

    def __init__(self, error_code: PlanningErrorCode, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code.value})")


class PlanningRequest:
    """包含敏感 user_request 的不可变请求；repr/asdict 不暴露正文。"""

    __slots__ = ("_selected_agent_id", "_user_request", "_locked")

    def __init__(self, selected_agent_id: str, user_request: str) -> None:
        if not isinstance(selected_agent_id, str) or not selected_agent_id.strip():
            raise ValueError("selected_agent_id 不能为空")
        if not isinstance(user_request, str) or not user_request.strip():
            raise ValueError("user_request 不能为空")
        object.__setattr__(self, "_selected_agent_id", selected_agent_id.strip())
        object.__setattr__(self, "_user_request", user_request)
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name, value) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("PlanningRequest 是不可变对象")
        object.__setattr__(self, name, value)

    @property
    def selected_agent_id(self) -> str:
        return self._selected_agent_id

    @property
    def user_request(self) -> str:
        return self._user_request

    def __repr__(self) -> str:
        return f"PlanningRequest(selected_agent_id={self.selected_agent_id!r}, user_request=<redacted>)"


class DirectAnswerDecision:
    __slots__ = ("_agent_id", "_reason_code", "_locked")

    def __init__(self, agent_id: str, reason_code: str) -> None:
        _validate_direct_decision_text(agent_id, reason_code)
        object.__setattr__(self, "_agent_id", agent_id.strip())
        object.__setattr__(self, "_reason_code", reason_code)
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name, value) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("DirectAnswerDecision 是不可变对象")
        object.__setattr__(self, name, value)

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def reason_code(self) -> str:
        return self._reason_code

    def __repr__(self) -> str:
        return f"DirectAnswerDecision(agent_id={self.agent_id!r}, reason_code={self.reason_code!r})"


class DelegatedTaskDecision:
    __slots__ = ("_task_id", "_agent_id", "_instruction", "_input_type", "_required_capabilities", "_locked")

    def __init__(
        self,
        task_id: str,
        agent_id: str,
        instruction: str,
        *,
        input_type: str = "text",
        required_capabilities: frozenset[str] = frozenset(),
    ) -> None:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id 不能为空")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("agent_id 不能为空")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction 不能为空")
        if not isinstance(input_type, str) or not input_type.strip():
            raise ValueError("input_type 不能为空")
        if not isinstance(required_capabilities, frozenset) or any(not isinstance(item, str) for item in required_capabilities):
            raise ValueError("required_capabilities 必须是字符串 frozenset")
        object.__setattr__(self, "_task_id", task_id.strip())
        object.__setattr__(self, "_agent_id", agent_id.strip())
        object.__setattr__(self, "_instruction", instruction)
        object.__setattr__(self, "_input_type", input_type.strip())
        object.__setattr__(self, "_required_capabilities", required_capabilities)
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name, value) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("DelegatedTaskDecision 是不可变对象")
        object.__setattr__(self, name, value)

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def instruction(self) -> str:
        return self._instruction

    @property
    def input_type(self) -> str:
        return self._input_type

    @property
    def required_capabilities(self) -> frozenset[str]:
        return self._required_capabilities

    def __repr__(self) -> str:
        return (
            "DelegatedTaskDecision("
            f"task_id={self.task_id!r}, agent_id={self.agent_id!r}, instruction=<redacted>, "
            f"input_type={self.input_type!r}, required_capabilities={sorted(self.required_capabilities)!r})"
        )


@dataclass(frozen=True, slots=True)
class DelegatedPlanDecision:
    tasks: tuple[DelegatedTaskDecision, ...]
    synthesis_required: bool

    def __post_init__(self) -> None:
        if not isinstance(self.tasks, tuple) or any(not isinstance(task, DelegatedTaskDecision) for task in self.tasks):
            raise ValueError("tasks 必须是 DelegatedTaskDecision tuple")
        if type(self.synthesis_required) is not bool:
            raise ValueError("synthesis_required 必须是 bool")


PlanningDecision: TypeAlias = DirectAnswerDecision | DelegatedPlanDecision


@dataclass(frozen=True, slots=True)
class ResolvedPlan:
    plan: Plan
    invocation_bindings: StepInvocationBindings = field(repr=False)
    planning_source: PlanningSource

    def __post_init__(self) -> None:
        if not isinstance(self.plan, Plan):
            raise ValueError("ResolvedPlan.plan 必须是 Plan")
        if not isinstance(self.invocation_bindings, StepInvocationBindings):
            raise ValueError("ResolvedPlan.invocation_bindings 必须合法")
        if not isinstance(self.planning_source, PlanningSource):
            raise ValueError("ResolvedPlan.planning_source 必须合法")
        PlanGraphValidator.validate(self.plan)
        plan_ids = tuple(step.step_id for step in self.plan.steps)
        if set(plan_ids) != set(self.invocation_bindings.step_ids) or len(plan_ids) != len(self.invocation_bindings.step_ids):
            raise ValueError("Plan Step 与 Binding key 必须一一对应")
        for step in self.plan.steps:
            try:
                binding = self.invocation_bindings.resolve_for_step(
                    step.step_id,
                    expected_agent_id=step.preferred_agent,
                )
            except InvocationBindingError:
                raise ValueError("Plan Step 与 Binding Agent 必须一致") from None
            if binding.input_type not in {"text"}:
                raise ValueError("Binding input_type 与 WP1 合同不兼容")


@runtime_checkable
class PlanningModel(Protocol):
    async def generate_plan(
        self,
        request: PlanningRequest,
        run_context: RunContext,
        *,
        memory_context_bundle: object | None = None,
        memory_injection_report_out: list | None = None,
    ) -> str:
        """通过统一模型服务 adapter 返回严格 JSON；实现负责预算/取消/超时。

        ``memory_context_bundle`` 非 None 时（WP4-B 生产 hook 始终传入，
        可为空 bundle），实现必须将其 typed 交给 Planner 模型上下文构建，
        并把 ``MemoryInjectionReport`` 追加到 report out list。
        """


class PlanCompilerProtocol(Protocol):
    def compile(
        self,
        decision: PlanningDecision,
        *,
        planning_source: PlanningSource,
        direct_instruction: str | None = None,
    ) -> ResolvedPlan:
        """把 typed decision 编译为已校验 ResolvedPlan。"""


class StrictPlanningDecisionParser:
    """严格 v1 JSON parser；模型无权声明 policy、依赖图或 Runtime 字段。"""

    _FORBIDDEN_FIELDS = frozenset(
        {
            "output_policy", "execution_kind", "depends_on", "dependencies",
            "runtime_status", "callable", "driver", "provider", "model",
            "result_type", "output_type",
        }
    )

    @classmethod
    def parse(cls, raw_output: str) -> PlanningDecision:
        if not isinstance(raw_output, str):
            raise PlanningError(PlanningErrorCode.PLANNER_SCHEMA_INVALID, "Planner 输出必须是 JSON 文本")
        try:
            payload = json.loads(raw_output)
        except (json.JSONDecodeError, RecursionError):
            raise PlanningError(PlanningErrorCode.PLANNER_SCHEMA_INVALID, "Planner 输出不是合法 JSON") from None
        if not isinstance(payload, Mapping):
            raise PlanningError(PlanningErrorCode.PLANNER_SCHEMA_INVALID, "Planner 顶层必须是对象")
        cls._reject_forbidden(payload)
        if payload.get("schema_version") != PLANNER_SCHEMA_VERSION:
            raise PlanningError(
                PlanningErrorCode.PLANNER_SCHEMA_VERSION_UNSUPPORTED,
                "Planner schema version 不受支持",
            )
        decision = payload.get("decision")
        if decision == "DIRECT_ANSWER":
            if "instruction" in payload:
                raise PlanningError(
                    PlanningErrorCode.PLANNER_FIELD_FORBIDDEN,
                    "Planner direct decision 无权声明 instruction",
                )
            cls._require_exact_keys(
                payload,
                {"schema_version", "decision", "agent_id", "reason_code"},
            )
            try:
                return DirectAnswerDecision(
                    agent_id=payload["agent_id"],
                    reason_code=payload["reason_code"],
                )
            except (KeyError, TypeError, ValueError):
                raise PlanningError(PlanningErrorCode.PLANNER_SCHEMA_INVALID, "Planner direct decision 字段无效") from None
        if decision == "DELEGATE":
            cls._require_exact_keys(
                payload,
                {"schema_version", "decision", "tasks", "synthesis_required"},
            )
            tasks_payload = payload.get("tasks")
            if not isinstance(tasks_payload, list):
                raise PlanningError(PlanningErrorCode.PLANNER_SCHEMA_INVALID, "Planner tasks 必须是数组")
            tasks: list[DelegatedTaskDecision] = []
            for item in tasks_payload:
                if not isinstance(item, Mapping):
                    raise PlanningError(PlanningErrorCode.PLANNER_SCHEMA_INVALID, "Planner task 必须是对象")
                cls._reject_forbidden(item)
                cls._require_allowed_keys(
                    item,
                    required={"task_id", "agent_id", "instruction"},
                    optional={"input_type", "capabilities"},
                )
                capabilities = item.get("capabilities", [])
                if not isinstance(capabilities, list) or any(not isinstance(value, str) for value in capabilities):
                    raise PlanningError(PlanningErrorCode.PLANNER_SCHEMA_INVALID, "Planner capabilities 必须是字符串数组")
                try:
                    tasks.append(
                        DelegatedTaskDecision(
                            task_id=item["task_id"],
                            agent_id=item["agent_id"],
                            instruction=item["instruction"],
                            # Planner 输出不拥有 specialist 的 invocation
                            # contract。当前 Registry 的所有 delegated agent
                            # 均以文本接收 instruction；用户请求中包含 JSON
                            # 也不会改变该边界。保留该字段的 parser 兼容性，
                            # 但不允许模型把任意标签带入已编译 Plan。
                            input_type="text",
                            required_capabilities=frozenset(capabilities),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    raise PlanningError(PlanningErrorCode.PLANNER_SCHEMA_INVALID, "Planner task 字段无效") from None
            if type(payload.get("synthesis_required")) is not bool:
                raise PlanningError(PlanningErrorCode.PLANNER_SCHEMA_INVALID, "synthesis_required 必须是 bool")
            return DelegatedPlanDecision(tuple(tasks), payload["synthesis_required"])
        raise PlanningError(PlanningErrorCode.PLANNER_DECISION_UNKNOWN, "Planner decision enum 未知")

    @classmethod
    def _reject_forbidden(cls, payload: Mapping[object, object]) -> None:
        keys = {key for key in payload if isinstance(key, str)}
        if "optional_dependencies" in keys or "optional_depends_on" in keys:
            raise PlanningError(
                PlanningErrorCode.OPTIONAL_DEPENDENCY_UNSUPPORTED,
                "optional dependency 在 WP1 不受支持",
            )
        if keys & cls._FORBIDDEN_FIELDS:
            raise PlanningError(
                PlanningErrorCode.PLANNER_FIELD_FORBIDDEN,
                "Planner 包含无权声明的字段",
            )

    @staticmethod
    def _require_exact_keys(payload: Mapping[object, object], expected: set[str]) -> None:
        if set(payload) != expected:
            raise PlanningError(PlanningErrorCode.PLANNER_SCHEMA_INVALID, "Planner 字段集合不符合 schema")

    @staticmethod
    def _require_allowed_keys(
        payload: Mapping[object, object], *, required: set[str], optional: set[str]
    ) -> None:
        keys = set(payload)
        if not required <= keys or not keys <= required | optional:
            raise PlanningError(PlanningErrorCode.PLANNER_SCHEMA_INVALID, "Planner task 字段集合不符合 schema")


class PlanResolver:
    """Resolve a request into one validated Plan and run-scoped Bindings."""

    def __init__(
        self,
        registry: AgentRegistry,
        compiler: PlanCompilerProtocol,
        planning_model: PlanningModel | None = None,
    ) -> None:
        self._registry = registry
        self._compiler = compiler
        self._planning_model = planning_model

    async def resolve(
        self,
        request: PlanningRequest,
        run_context: RunContext,
        *,
        memory_context_bundle: object | None = None,
        memory_injection_report_out: list | None = None,
    ) -> ResolvedPlan:
        """Resolve a request into one validated Plan and run-scoped Bindings.

        ``memory_context_bundle`` 是 WP4-B run-scoped immutable Memory
        projection；只转发给 Planner 模型 invocation（PLANNER_MEMORY_VISIBILITY
        = YES），不参与 deterministic 规则决策，也不进入 Plan/Binding。
        """
        if not isinstance(request, PlanningRequest) or not isinstance(run_context, RunContext):
            raise PlanningError(PlanningErrorCode.INVALID_REQUEST, "PlanningRequest 或 RunContext 无效")
        run_context.raise_if_inactive()
        try:
            selected = self._registry.require_entry(request.selected_agent_id)
        except AgentRegistryError:
            raise
        if not selected.model_direct_allowed:
            decision = DirectAnswerDecision(
                selected.agent_id,
                "EXPLICIT_ENTRY_SELECTION",
            )
            return self._compiler.compile(
                decision,
                planning_source=PlanningSource.EXPLICIT_ENTRY,
                direct_instruction=request.user_request,
            )

        deterministic = self._deterministic_core_decision(
            request.user_request,
            core_agent_id=selected.agent_id,
        )
        if deterministic is not None:
            return self._compiler.compile(
                deterministic,
                planning_source=PlanningSource.DETERMINISTIC_RULE,
                direct_instruction=(
                    request.user_request
                    if isinstance(deterministic, DirectAnswerDecision)
                    else None
                ),
            )
        if self._planning_model is None:
            raise PlanningError(PlanningErrorCode.PLANNING_MODEL_REQUIRED, "请求需要 Planner model")
        try:
            if memory_context_bundle is None:
                raw_output = await self._planning_model.generate_plan(request, run_context)
            else:
                raw_output = await self._planning_model.generate_plan(
                    request,
                    run_context,
                    memory_context_bundle=memory_context_bundle,
                    memory_injection_report_out=memory_injection_report_out,
                )
        except BaseException as exc:
            if isinstance(
                exc,
                (
                    KeyboardInterrupt,
                    SystemExit,
                    RunCancelledError,
                    RunDeadlineExceededError,
                    BudgetExceededError,
                    asyncio.CancelledError,
                ),
            ):
                raise
            raise PlanningError(PlanningErrorCode.PLANNING_MODEL_FAILED, "Planner model 调用失败") from None
        run_context.raise_if_inactive()
        decision = StrictPlanningDecisionParser.parse(raw_output)
        return self._compiler.compile(
            decision,
            planning_source=PlanningSource.MODEL,
            direct_instruction=(
                request.user_request
                if isinstance(decision, DirectAnswerDecision)
                else None
            ),
        )

    def _deterministic_core_decision(
        self, user_request: str, *, core_agent_id: str
    ) -> PlanningDecision | None:
        if _DIRECT_GREETING.fullmatch(user_request.strip()):
            return DirectAnswerDecision(core_agent_id, "DETERMINISTIC_GREETING")
        selected: list[str] = []
        for agent_id in self._registry.delegated_specialist_ids():
            registration = self._registry.resolve(agent_id)
            if any(alias.casefold() in user_request.casefold() for alias in registration.deterministic_aliases):
                selected.append(agent_id)
        if not selected and _DOCUMENT_REFERENCE.search(user_request):
            rag_agents = tuple(
                agent_id
                for agent_id in self._registry.delegated_specialist_ids()
                if "rag" in self._registry.resolve(agent_id).capabilities
            )
            if len(rag_agents) == 1:
                selected.append(rag_agents[0])
        explicit = bool(selected) and (
            _DELEGATION_VERB.search(user_request) is not None
            or _DOCUMENT_REFERENCE.search(user_request) is not None
        )
        if not explicit:
            return None
        tasks = tuple(
            DelegatedTaskDecision(
                task_id=agent_id.removesuffix("_expert").removesuffix("_analyst"),
                agent_id=agent_id,
                instruction=user_request,
            )
            for agent_id in selected
        )
        direct_allowed = (
            len(tasks) == 1
            and self._registry.resolve(tasks[0].agent_id).allows_single_delegated_passthrough
        )
        return DelegatedPlanDecision(tasks, synthesis_required=not direct_allowed)


def _validate_direct_decision_text(agent_id: object, reason_code: object) -> None:
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ValueError("agent_id 不能为空")
    if not isinstance(reason_code, str) or _SAFE_REASON.fullmatch(reason_code) is None:
        raise ValueError("reason_code 必须是安全标识")


__all__ = [
    "DelegatedPlanDecision",
    "DelegatedTaskDecision",
    "DirectAnswerDecision",
    "PLANNER_SCHEMA_VERSION",
    "PlanCompilerProtocol",
    "PlanResolver",
    "PlanningDecision",
    "PlanningError",
    "PlanningErrorCode",
    "PlanningModel",
    "PlanningRequest",
    "PlanningSource",
    "ResolvedPlan",
    "StrictPlanningDecisionParser",
]
