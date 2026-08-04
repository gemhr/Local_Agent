#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 typed PlanningDecision 编译为四种合法、安全的 Runtime Plan。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
import hashlib
import json
import re

from core.runtime.agent_registry import (
    AgentRegistration,
    AgentRegistry,
    AgentRegistryError,
    AgentRegistryErrorCode,
)
from core.runtime.invocation_bindings import AgentInvocationSpec, StepInvocationBindings
from core.runtime.multi_agent_planning import (
    DelegatedPlanDecision,
    DelegatedTaskDecision,
    DirectAnswerDecision,
    PlanningDecision,
    PlanningSource,
    ResolvedPlan,
)
from core.runtime.plan_graph import PlanGraphValidationError, PlanGraphValidator
from core.runtime.planning import (
    ExecutionKind,
    OutputPolicy,
    Plan,
    PlanSource,
    PlanStep,
    RiskLevel,
    TaskCapabilityRequirements,
)

_TASK_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class PlanCompileErrorCode(str, Enum):
    UNKNOWN_AGENT = "UNKNOWN_AGENT"
    AGENT_DISABLED = "AGENT_DISABLED"
    ENTRY_AGENT_NOT_ALLOWED = "ENTRY_AGENT_NOT_ALLOWED"
    DELEGATED_AGENT_NOT_ALLOWED = "DELEGATED_AGENT_NOT_ALLOWED"
    SYNTHESIS_ENTRY_FORBIDDEN = "SYNTHESIS_ENTRY_FORBIDDEN"
    MODEL_DIRECT_AGENT_NOT_ALLOWED = "MODEL_DIRECT_AGENT_NOT_ALLOWED"
    EMPTY_TASKS = "EMPTY_TASKS"
    DUPLICATE_TASK_ID = "DUPLICATE_TASK_ID"
    INVALID_TASK_ID = "INVALID_TASK_ID"
    EMPTY_INSTRUCTION = "EMPTY_INSTRUCTION"
    INSTRUCTION_LIMIT_EXCEEDED = "INSTRUCTION_LIMIT_EXCEEDED"
    PLAN_INSTRUCTION_LIMIT_EXCEEDED = "PLAN_INSTRUCTION_LIMIT_EXCEEDED"
    PLAN_LIMIT_EXCEEDED = "PLAN_LIMIT_EXCEEDED"
    INVALID_CAPABILITY = "INVALID_CAPABILITY"
    INVALID_INPUT_TYPE = "INVALID_INPUT_TYPE"
    INVALID_GRAPH_SHAPE = "INVALID_GRAPH_SHAPE"
    DIRECT_DELEGATION_NOT_ALLOWED = "DIRECT_DELEGATION_NOT_ALLOWED"
    DUPLICATE_STEP_ID = "DUPLICATE_STEP_ID"
    MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
    SELF_DEPENDENCY = "SELF_DEPENDENCY"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    MULTIPLE_FINAL_STEPS = "MULTIPLE_FINAL_STEPS"
    NO_FINAL_STEP = "NO_FINAL_STEP"
    FINAL_POLICY_NOT_ALLOWED = "FINAL_POLICY_NOT_ALLOWED"
    BINDING_MISMATCH = "BINDING_MISMATCH"


class PlanCompileError(ValueError):
    """不包含 instruction、路径或模型原文的稳定编译错误。"""

    def __init__(self, error_code: PlanCompileErrorCode, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code.value})")


@dataclass(frozen=True, slots=True)
class PlanCompileConfig:
    max_agents: int = 8
    max_steps: int = 9
    max_instruction_chars: int = 8_000
    max_total_instruction_chars: int = 24_000

    def __post_init__(self) -> None:
        for value in (
            self.max_agents,
            self.max_steps,
            self.max_instruction_chars,
            self.max_total_instruction_chars,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("PlanCompileConfig 上限必须是正整数")
        if self.max_steps < 1 or self.max_total_instruction_chars < self.max_instruction_chars:
            raise ValueError("PlanCompileConfig 上限关系无效")


class PlanCompiler:
    def __init__(self, registry: AgentRegistry, config: PlanCompileConfig | None = None) -> None:
        if not isinstance(registry, AgentRegistry):
            raise TypeError("registry 必须是 AgentRegistry")
        self._registry = registry
        self._config = config or PlanCompileConfig()

    def compile(self, decision: PlanningDecision, *, planning_source: PlanningSource) -> ResolvedPlan:
        if not isinstance(planning_source, PlanningSource):
            raise TypeError("planning_source 必须合法")
        if isinstance(decision, DirectAnswerDecision):
            return self._compile_direct(decision, planning_source)
        if isinstance(decision, DelegatedPlanDecision):
            return self._compile_delegated(decision, planning_source)
        raise TypeError("decision 必须是合法 PlanningDecision")

    def validate_plan(self, plan: Plan) -> None:
        """对 Compiler 产物做防御性 Registry、DAG 和四形态校验。"""
        try:
            graph = PlanGraphValidator.validate(plan)
        except PlanGraphValidationError as exc:
            mapping = {
                "DUPLICATE_STEP_ID": PlanCompileErrorCode.DUPLICATE_STEP_ID,
                "MISSING_DEPENDENCY": PlanCompileErrorCode.MISSING_DEPENDENCY,
                "SELF_DEPENDENCY": PlanCompileErrorCode.SELF_DEPENDENCY,
                "DEPENDENCY_CYCLE": PlanCompileErrorCode.DEPENDENCY_CYCLE,
                "DUPLICATE_DEPENDENCY": PlanCompileErrorCode.INVALID_GRAPH_SHAPE,
            }
            raise PlanCompileError(
                mapping.get(exc.error_code, PlanCompileErrorCode.INVALID_GRAPH_SHAPE),
                "Plan DAG 校验失败",
            ) from None
        except ValueError:
            raise PlanCompileError(
                PlanCompileErrorCode.INVALID_GRAPH_SHAPE,
                "Plan 基础合同校验失败",
            ) from None
        if len(plan.steps) > self._config.max_steps:
            self._fail(PlanCompileErrorCode.PLAN_LIMIT_EXCEEDED, "Plan Step 数超过上限")
        registrations: dict[str, AgentRegistration] = {}
        for step in plan.steps:
            registrations[step.step_id] = self._resolve_registration(step.preferred_agent)
            registration = registrations[step.step_id]
            if step.execution_kind is not registration.execution_kind:
                self._fail(PlanCompileErrorCode.INVALID_GRAPH_SHAPE, "Step execution kind 未获 Registry 授权")
            if step.output_policy is OutputPolicy.FINAL_SYNTHESIS and not registration.synthesis_only:
                self._fail(PlanCompileErrorCode.FINAL_POLICY_NOT_ALLOWED, "非 synthesis Agent 不得产生 synthesis final")
            if registration.synthesis_only and step.output_policy is not OutputPolicy.FINAL_SYNTHESIS:
                self._fail(PlanCompileErrorCode.FINAL_POLICY_NOT_ALLOWED, "synthesis Agent 的 output policy 无效")
        finals = tuple(step for step in plan.steps if step.output_policy is not OutputPolicy.INTERNAL)
        if not finals:
            self._fail(PlanCompileErrorCode.NO_FINAL_STEP, "Plan 必须有唯一 final Step")
        if len(finals) > 1:
            self._fail(PlanCompileErrorCode.MULTIPLE_FINAL_STEPS, "Plan 不允许多个 final Step")
        final = finals[0]
        if final.step_id not in graph.leaf_step_ids or graph.leaf_step_ids != (final.step_id,):
            self._fail(PlanCompileErrorCode.INVALID_GRAPH_SHAPE, "final Step 必须是唯一 sink")
        if len(plan.steps) == 1:
            step = plan.steps[0]
            if step.depends_on or step.execution_kind is not ExecutionKind.AGENT or step.output_policy is not OutputPolicy.FINAL_PASSTHROUGH:
                self._fail(PlanCompileErrorCode.INVALID_GRAPH_SHAPE, "单 Step Plan 不属于合法 direct 形态")
            if not registrations[step.step_id].entry_allowed:
                self._fail(PlanCompileErrorCode.ENTRY_AGENT_NOT_ALLOWED, "direct Agent 不允许作为 entry")
            return
        synthesis_steps = tuple(step for step in plan.steps if step.execution_kind is ExecutionKind.SYNTHESIS)
        if len(synthesis_steps) != 1 or synthesis_steps[0] is not final:
            self._fail(PlanCompileErrorCode.INVALID_GRAPH_SHAPE, "多 Step Plan 必须有唯一 final synthesis")
        specialists = tuple(step for step in plan.steps if step is not final)
        if len(specialists) > self._config.max_agents:
            self._fail(PlanCompileErrorCode.PLAN_LIMIT_EXCEEDED, "specialist 数超过上限")
        if any(not registrations[step.step_id].delegation_allowed for step in specialists):
            self._fail(PlanCompileErrorCode.DELEGATED_AGENT_NOT_ALLOWED, "Plan 包含未获 delegated 权限的 Agent")
        if len(specialists) > 1 and any(
            not registrations[step.step_id].supports_parallel for step in specialists
        ):
            self._fail(PlanCompileErrorCode.INVALID_GRAPH_SHAPE, "Agent 不允许参与并行 fan-out")
        if any(
            step.depends_on
            or step.execution_kind is not ExecutionKind.AGENT
            or step.output_policy is not OutputPolicy.INTERNAL
            or registrations[step.step_id].synthesis_only
            for step in specialists
        ):
            self._fail(PlanCompileErrorCode.INVALID_GRAPH_SHAPE, "specialist Step 结构无效")
        for step in plan.steps:
            registration = registrations[step.step_id]
            requirements = step.capability_requirements
            required = {
                name
                for enabled, name in (
                    (requirements.requires_rag, "rag"),
                    (requirements.requires_code_reasoning, "code_reasoning"),
                    (requirements.requires_structured_output, "structured_output"),
                )
                if enabled
            }
            if not required <= registration.capabilities:
                self._fail(PlanCompileErrorCode.INVALID_CAPABILITY, "Plan capability 未获 Registry 支持")
        if final.depends_on != tuple(step.step_id for step in specialists):
            self._fail(PlanCompileErrorCode.INVALID_GRAPH_SHAPE, "synthesis 必须依赖全部且仅依赖 specialist")

    def _compile_direct(self, decision: DirectAnswerDecision, source: PlanningSource) -> ResolvedPlan:
        self._validate_instruction(decision.instruction)
        registration = self._resolve_registration(decision.agent_id)
        if registration.synthesis_only:
            self._fail(PlanCompileErrorCode.SYNTHESIS_ENTRY_FORBIDDEN, "synthesis Agent 不允许作为 entry")
        if not registration.entry_allowed:
            self._fail(PlanCompileErrorCode.ENTRY_AGENT_NOT_ALLOWED, "Agent 不允许作为 entry")
        if source is PlanningSource.MODEL and not registration.model_direct_allowed:
            self._fail(PlanCompileErrorCode.MODEL_DIRECT_AGENT_NOT_ALLOWED, "Planner 不得授予 specialist direct 权限")
        step = self._step(
            "answer", registration, (), OutputPolicy.FINAL_PASSTHROUGH,
            multi_agent=False,
        )
        return self._resolved(
            (step,),
            (AgentInvocationSpec("answer", registration.agent_id, decision.instruction),),
            source,
            safe_identity=("direct", registration.agent_id),
        )

    def _compile_delegated(self, decision: DelegatedPlanDecision, source: PlanningSource) -> ResolvedPlan:
        if not decision.tasks:
            self._fail(PlanCompileErrorCode.EMPTY_TASKS, "Delegated Plan 至少需要一个 task")
        if len(decision.tasks) > self._config.max_agents:
            self._fail(PlanCompileErrorCode.PLAN_LIMIT_EXCEEDED, "specialist 数超过上限")
        seen: set[str] = set()
        total_chars = 0
        checked: list[tuple[DelegatedTaskDecision, AgentRegistration]] = []
        for task in decision.tasks:
            if _TASK_ID.fullmatch(task.task_id) is None or task.task_id == "synthesis":
                self._fail(PlanCompileErrorCode.INVALID_TASK_ID, "task_id 不符合稳定 ID 合同")
            if task.task_id in seen:
                self._fail(PlanCompileErrorCode.DUPLICATE_TASK_ID, "task_id 不允许重复")
            seen.add(task.task_id)
            self._validate_instruction(task.instruction)
            total_chars += len(task.instruction)
            registration = self._require_delegated(task.agent_id)
            if task.input_type not in registration.accepted_input_types:
                self._fail(PlanCompileErrorCode.INVALID_INPUT_TYPE, "Agent 不接受该 input type")
            if any(_CAPABILITY.fullmatch(item) is None for item in task.required_capabilities):
                self._fail(PlanCompileErrorCode.INVALID_CAPABILITY, "capability 标识无效")
            if not task.required_capabilities <= registration.capabilities:
                self._fail(PlanCompileErrorCode.INVALID_CAPABILITY, "Agent 不具备所需 capability")
            checked.append((task, registration))
        if total_chars > self._config.max_total_instruction_chars:
            self._fail(PlanCompileErrorCode.PLAN_INSTRUCTION_LIMIT_EXCEEDED, "Plan instruction 总长度超过上限")
        checked.sort(key=lambda item: item[0].task_id)
        if len(checked) > 1 and any(not registration.supports_parallel for _, registration in checked):
            self._fail(PlanCompileErrorCode.INVALID_GRAPH_SHAPE, "Agent 不允许参与并行 fan-out")
        if len(checked) > 1 and not decision.synthesis_required:
            self._fail(PlanCompileErrorCode.INVALID_GRAPH_SHAPE, "多 specialist 必须 synthesis")
        if len(checked) == 1 and not decision.synthesis_required:
            task, registration = checked[0]
            if not registration.allows_single_delegated_passthrough:
                self._fail(PlanCompileErrorCode.DIRECT_DELEGATION_NOT_ALLOWED, "该 specialist 不允许 delegated direct")
            step_id = self._specialist_step_id(task.task_id)
            step = self._step(
                step_id, registration, (), OutputPolicy.FINAL_PASSTHROUGH,
                multi_agent=False, requested=task.required_capabilities,
            )
            return self._resolved(
                (step,),
                (AgentInvocationSpec(step_id, registration.agent_id, task.instruction, task.input_type),),
                source,
                safe_identity=("delegated-direct", task.task_id, registration.agent_id),
            )
        synthesis = self._registry.synthesis_registration()
        specialist_steps: list[PlanStep] = []
        bindings: list[AgentInvocationSpec] = []
        for task, registration in checked:
            step_id = self._specialist_step_id(task.task_id)
            specialist_steps.append(
                self._step(
                    step_id, registration, (), OutputPolicy.INTERNAL,
                    multi_agent=True, requested=task.required_capabilities,
                )
            )
            bindings.append(AgentInvocationSpec(step_id, registration.agent_id, task.instruction, task.input_type))
        synthesis_step = self._step(
            "synthesis", synthesis,
            tuple(step.step_id for step in specialist_steps),
            OutputPolicy.FINAL_SYNTHESIS,
            multi_agent=True,
        )
        specialist_steps.append(synthesis_step)
        bindings.append(
            AgentInvocationSpec(
                "synthesis",
                synthesis.agent_id,
                "Synthesize all explicitly required specialist results for the user request.",
            )
        )
        if len(specialist_steps) > self._config.max_steps:
            self._fail(PlanCompileErrorCode.PLAN_LIMIT_EXCEEDED, "Plan Step 数超过上限")
        identity = tuple(
            value
            for task, registration in checked
            for value in (task.task_id, registration.agent_id)
        )
        return self._resolved(
            tuple(specialist_steps), tuple(bindings), source,
            safe_identity=("synthesis", *identity),
        )

    def _resolved(
        self,
        steps: tuple[PlanStep, ...],
        bindings: tuple[AgentInvocationSpec, ...],
        source: PlanningSource,
        *,
        safe_identity: tuple[str, ...],
    ) -> ResolvedPlan:
        plan_source = PlanSource.MODEL_GENERATED if source is PlanningSource.MODEL else PlanSource.DETERMINISTIC
        safe_payload = json.dumps(safe_identity, ensure_ascii=True, separators=(",", ":"))
        suffix = hashlib.sha256(safe_payload.encode("ascii")).hexdigest()[:12]
        plan = Plan(
            plan_id=f"stage2-5-{suffix}",
            version=1,
            task_summary="执行已校验的 Stage 2.5 计划。",
            steps=steps,
            created_at=datetime.now(UTC),
            source=plan_source,
        )
        self.validate_plan(plan)
        try:
            return ResolvedPlan(plan, StepInvocationBindings(bindings), source)
        except ValueError:
            self._fail(PlanCompileErrorCode.BINDING_MISMATCH, "Plan 与 Bindings 不一致")
        raise AssertionError("unreachable")

    def _step(
        self,
        step_id: str,
        registration: AgentRegistration,
        depends_on: tuple[str, ...],
        output_policy: OutputPolicy,
        *,
        multi_agent: bool,
        requested: frozenset[str] = frozenset(),
    ) -> PlanStep:
        capabilities = registration.capabilities | requested
        requirements = TaskCapabilityRequirements(
            requires_planning=multi_agent,
            requires_rag="rag" in capabilities,
            requires_multi_agent=multi_agent,
            requires_code_reasoning="code_reasoning" in capabilities,
            requires_structured_output="structured_output" in capabilities,
            risk_level=RiskLevel.LOW,
            estimated_steps=max(1, len(depends_on) + 1),
        )
        if registration.synthesis_only:
            title, description, completion = (
                "汇总专业结果", "汇总显式依赖中的专业结果。", "已生成唯一最终回答。"
            )
        else:
            title, description, completion = (
                "执行专业任务", "由已授权智能体执行当前绑定任务。", "已生成类型合法的任务结果。"
            )
        return PlanStep(
            step_id, title, description, depends_on, completion,
            registration.agent_id, requirements,
            execution_kind=registration.execution_kind,
            output_policy=output_policy,
        )

    def _resolve_registration(self, agent_id: str) -> AgentRegistration:
        try:
            return self._registry.resolve(agent_id)
        except AgentRegistryError as exc:
            mapping = {
                AgentRegistryErrorCode.UNKNOWN_AGENT: PlanCompileErrorCode.UNKNOWN_AGENT,
                AgentRegistryErrorCode.AGENT_DISABLED: PlanCompileErrorCode.AGENT_DISABLED,
            }
            self._fail(mapping.get(exc.error_code, PlanCompileErrorCode.UNKNOWN_AGENT), exc.safe_message)
        raise AssertionError("unreachable")

    def _require_delegated(self, agent_id: str) -> AgentRegistration:
        try:
            return self._registry.require_delegated(agent_id)
        except AgentRegistryError as exc:
            mapping = {
                AgentRegistryErrorCode.UNKNOWN_AGENT: PlanCompileErrorCode.UNKNOWN_AGENT,
                AgentRegistryErrorCode.AGENT_DISABLED: PlanCompileErrorCode.AGENT_DISABLED,
                AgentRegistryErrorCode.DELEGATED_AGENT_NOT_ALLOWED: PlanCompileErrorCode.DELEGATED_AGENT_NOT_ALLOWED,
            }
            self._fail(mapping.get(exc.error_code, PlanCompileErrorCode.DELEGATED_AGENT_NOT_ALLOWED), exc.safe_message)
        raise AssertionError("unreachable")

    def _validate_instruction(self, instruction: str) -> None:
        if not isinstance(instruction, str) or not instruction.strip():
            self._fail(PlanCompileErrorCode.EMPTY_INSTRUCTION, "instruction 不能为空")
        if len(instruction) > self._config.max_instruction_chars:
            self._fail(PlanCompileErrorCode.INSTRUCTION_LIMIT_EXCEEDED, "instruction 长度超过上限")

    @staticmethod
    def _specialist_step_id(task_id: str) -> str:
        return f"task-{task_id}"

    @staticmethod
    def _fail(error_code: PlanCompileErrorCode, safe_message: str):
        raise PlanCompileError(error_code, safe_message)


__all__ = [
    "PlanCompileConfig",
    "PlanCompileError",
    "PlanCompileErrorCode",
    "PlanCompiler",
]
