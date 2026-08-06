#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段二 Planner 的不可变任务描述边界。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class PlanSource(str, Enum):
    DETERMINISTIC = "deterministic"
    LEGACY_ADAPTER = "legacy_adapter"
    MODEL_GENERATED = "model_generated"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExecutionKind(str, Enum):
    """Plan Step 的稳定执行分类；不表示任何运行状态。"""

    AGENT = "AGENT"
    SYNTHESIS = "SYNTHESIS"


class OutputPolicy(str, Enum):
    """由 Registry/Compiler 授权的静态输出策略。"""

    INTERNAL = "INTERNAL"
    FINAL_PASSTHROUGH = "FINAL_PASSTHROUGH"
    FINAL_SYNTHESIS = "FINAL_SYNTHESIS"


@dataclass(frozen=True, slots=True)
class TaskCapabilityRequirements:
    """描述任务所需能力，不保存用户正文或具体模型信息。"""
    requires_planning: bool = False
    requires_tools: bool = False
    requires_rag: bool = False
    requires_multi_agent: bool = False
    requires_code_reasoning: bool = False
    requires_structured_output: bool = False
    requires_long_reasoning: bool = False
    risk_level: RiskLevel = RiskLevel.LOW
    estimated_steps: int = 1

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if name.startswith("requires_") and type(value) is not bool:
                raise ValueError(f"{name} 必须是 bool")
        if not isinstance(self.risk_level, RiskLevel):
            raise ValueError("risk_level 必须是 RiskLevel")
        if isinstance(self.estimated_steps, bool) or not isinstance(self.estimated_steps, int) or self.estimated_steps <= 0:
            raise ValueError("estimated_steps 必须是正整数")


@dataclass(frozen=True, slots=True)
class PlanStep:
    """计划步骤的静态定义，不承载 Runtime 执行状态。"""
    step_id: str
    title: str
    description: str
    depends_on: tuple[str, ...]
    completion_criteria: str
    preferred_agent: str
    capability_requirements: TaskCapabilityRequirements
    execution_kind: ExecutionKind = ExecutionKind.AGENT
    output_policy: OutputPolicy = OutputPolicy.FINAL_PASSTHROUGH


@dataclass(frozen=True, slots=True)
class Plan:
    """可供后续 Scheduler 使用的不可变计划。"""
    plan_id: str
    version: int
    task_summary: str
    steps: tuple[PlanStep, ...]
    created_at: datetime
    source: PlanSource


class PlanValidator:
    """只校验静态 Plan 基本不变量，不进行 DAG 调度分析。"""

    @staticmethod
    def validate(plan: Plan) -> None:
        if not isinstance(plan.plan_id, str) or not plan.plan_id.strip():
            raise ValueError("plan_id 不能为空")
        if isinstance(plan.version, bool) or not isinstance(plan.version, int) or plan.version <= 0:
            raise ValueError("version 必须是正整数")
        if not isinstance(plan.created_at, datetime) or plan.created_at.tzinfo is None or plan.created_at.utcoffset() is None or plan.created_at.astimezone(timezone.utc) != plan.created_at:
            raise ValueError("created_at 必须是带时区的 UTC 时间")
        if not isinstance(plan.source, PlanSource) or not plan.steps:
            raise ValueError("Plan 必须包含至少一个合法步骤")
        for step in plan.steps:
            if not isinstance(step.step_id, str) or not step.step_id.strip():
                raise ValueError("step_id 不能为空")
            if not isinstance(step.title, str) or not step.title.strip():
                raise ValueError("title 不能为空")
            if not isinstance(step.depends_on, tuple):
                raise ValueError("depends_on 必须是 tuple")
            if not all(isinstance(dependency, str) and dependency.strip() for dependency in step.depends_on):
                raise ValueError("depends_on 只能包含非空步骤标识")
            if not isinstance(step.completion_criteria, str) or not step.completion_criteria.strip():
                raise ValueError("completion_criteria 不能为空")
            if not isinstance(step.preferred_agent, str) or not step.preferred_agent.strip():
                raise ValueError("preferred_agent 不能为空")
            if not isinstance(step.capability_requirements, TaskCapabilityRequirements):
                raise ValueError("capability_requirements 必须合法")
            if not isinstance(step.execution_kind, ExecutionKind):
                raise ValueError("execution_kind 必须合法")
            if not isinstance(step.output_policy, OutputPolicy):
                raise ValueError("output_policy 必须合法")


def create_single_step_plan(agent_id: str, requirements: TaskCapabilityRequirements) -> Plan:
    """为没有旧版显式计划的调用创建确定性单步骤 Plan。"""
    plan = Plan(
        plan_id=f"deterministic-{agent_id}-plan",
        version=1,
        task_summary="完成当前智能体请求。",
        steps=(PlanStep("answer", "生成回答", "基于已构建上下文生成当前请求的回答。", (), "已生成与当前请求相关的回答。", agent_id, requirements),),
        created_at=datetime.now(timezone.utc),
        source=PlanSource.DETERMINISTIC,
    )
    PlanValidator.validate(plan)
    return plan


def compute_plan_shape(plan: Plan) -> str:
    """返回四种合法执行图的稳定 shape（0/1/2/3）。

    - 0: Core direct（单 Step FINAL_PASSTHROUGH，agent 为 core_router）
    - 1: 单个获批 entry specialist 透传
    - 2: 单个 specialist -> synthesis
    - 3: N 个 specialist -> synthesis

    对不符合四种合法图的结构返回 ``unknown``，不虚构 shape。
    """
    if not isinstance(plan, Plan) or not plan.steps:
        return "unknown"
    finals = tuple(
        step
        for step in plan.steps
        if step.output_policy is not OutputPolicy.INTERNAL
    )
    if len(finals) != 1:
        return "unknown"
    final_step = finals[0]
    internals = tuple(
        step for step in plan.steps if step.output_policy is OutputPolicy.INTERNAL
    )
    if len(plan.steps) == 1:
        if final_step.output_policy is OutputPolicy.FINAL_PASSTHROUGH:
            if final_step.preferred_agent == "core_router":
                return "0"
            return "1"
        return "unknown"
    if (
        len(internals) == len(plan.steps) - 1
        and final_step.output_policy is OutputPolicy.FINAL_SYNTHESIS
    ):
        return "2" if len(internals) == 1 else "3"
    return "unknown"
