#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 的静态 DAG 构建、校验与稳定拓扑诊断。"""

from __future__ import annotations

from dataclasses import dataclass
import heapq
from types import MappingProxyType
from typing import Mapping

from core.runtime.planning import Plan, PlanValidator


class PlanGraphValidationError(ValueError):
    """图结构不变量被破坏时引发的安全异常，不携带任务正文。"""

    def __init__(
        self, *, error_code: str, message: str, plan: Plan,
        step_id: str | None = None, dependency_step_id: str | None = None,
        cycle_path: tuple[str, ...] = (), unresolved_step_ids: tuple[str, ...] = (),
    ) -> None:
        self.error_code = error_code
        self.plan_id = plan.plan_id
        self.plan_version = plan.version
        self.step_id = step_id
        self.dependency_step_id = dependency_step_id
        self.cycle_path = cycle_path
        self.unresolved_step_ids = unresolved_step_ids
        self.safe_message = message
        super().__init__(
            f"{message} (error_code={error_code}, plan_id={plan.plan_id}, "
            f"plan_version={plan.version}, step_id={step_id or '-'}, "
            f"dependency_step_id={dependency_step_id or '-'})"
        )


@dataclass(frozen=True, slots=True)
class PlanGraph:
    """不可变的 Plan 图视图，只保存结构 ID 与稳定派生顺序。"""

    plan_id: str
    plan_version: int
    step_ids: tuple[str, ...]
    topological_order: tuple[str, ...]
    root_step_ids: tuple[str, ...]
    leaf_step_ids: tuple[str, ...]
    dependencies: Mapping[str, tuple[str, ...]]
    dependents: Mapping[str, tuple[str, ...]]

    def dependencies_of(self, step_id: str) -> tuple[str, ...]:
        """返回步骤的前置步骤，未知步骤返回空 tuple。"""
        return self.dependencies.get(step_id, ())

    def dependents_of(self, step_id: str) -> tuple[str, ...]:
        """返回依赖该步骤的下游步骤，未知步骤返回空 tuple。"""
        return self.dependents.get(step_id, ())

    def contains_step(self, step_id: str) -> bool:
        """判断图中是否存在指定步骤。"""
        return step_id in self.dependencies


class PlanGraphValidator:
    """集中校验 Plan 的图结构，并构建稳定、不可变的 DAG 视图。"""

    @classmethod
    def validate(cls, plan: Plan) -> PlanGraph:
        """先校验基础字段，再按固定顺序校验并构建 DAG。"""
        PlanValidator.validate(plan)
        step_ids = tuple(step.step_id for step in plan.steps)
        index = {step_id: position for position, step_id in enumerate(step_ids)}
        if len(index) != len(step_ids):
            seen_step_ids: set[str] = set()
            duplicate = next(
                step_id
                for step_id in step_ids
                if step_id in seen_step_ids or seen_step_ids.add(step_id)
            )
            raise cls._error(plan, "DUPLICATE_STEP_ID", "步骤标识不允许重复", step_id=duplicate)

        dependencies: dict[str, tuple[str, ...]] = {}
        dependents: dict[str, list[str]] = {step_id: [] for step_id in step_ids}
        for step in plan.steps:
            seen: set[str] = set()
            for dependency_id in step.depends_on:
                if dependency_id not in index:
                    raise cls._error(plan, "MISSING_DEPENDENCY", "依赖的步骤不存在", step_id=step.step_id, dependency_step_id=dependency_id)
                if dependency_id == step.step_id:
                    raise cls._error(plan, "SELF_DEPENDENCY", "步骤不允许依赖自身", step_id=step.step_id, dependency_step_id=dependency_id)
                if dependency_id in seen:
                    raise cls._error(plan, "DUPLICATE_DEPENDENCY", "同一步骤不允许重复依赖", step_id=step.step_id, dependency_step_id=dependency_id)
                seen.add(dependency_id)
                dependents[dependency_id].append(step.step_id)
            dependencies[step.step_id] = step.depends_on

        ordered_dependents = {step_id: tuple(sorted(items, key=index.__getitem__)) for step_id, items in dependents.items()}
        topological_order, unresolved = cls._stable_kahn(step_ids, dependencies, ordered_dependents, index)
        if unresolved:
            cycle_path = cls._find_cycle(unresolved, ordered_dependents, index)
            raise cls._error(plan, "DEPENDENCY_CYCLE", "Plan 存在依赖环", cycle_path=cycle_path, unresolved_step_ids=unresolved)
        return PlanGraph(
            plan_id=plan.plan_id, plan_version=plan.version, step_ids=step_ids,
            topological_order=topological_order,
            root_step_ids=tuple(step_id for step_id in step_ids if not dependencies[step_id]),
            leaf_step_ids=tuple(step_id for step_id in step_ids if not ordered_dependents[step_id]),
            dependencies=MappingProxyType(dict(dependencies)),
            dependents=MappingProxyType(dict(ordered_dependents)),
        )

    @staticmethod
    def _stable_kahn(step_ids, dependencies, dependents, index):
        indegree = {step_id: len(dependencies[step_id]) for step_id in step_ids}
        ready = [(index[step_id], step_id) for step_id in step_ids if indegree[step_id] == 0]
        heapq.heapify(ready)
        result: list[str] = []
        while ready:
            _, step_id = heapq.heappop(ready)
            result.append(step_id)
            for dependent_id in dependents[step_id]:
                indegree[dependent_id] -= 1
                if indegree[dependent_id] == 0:
                    heapq.heappush(ready, (index[dependent_id], dependent_id))
        unresolved = tuple(step_id for step_id in step_ids if indegree[step_id] > 0)
        return tuple(result), unresolved

    @staticmethod
    def _find_cycle(unresolved, dependents, index):
        remaining = set(unresolved)
        visiting: list[str] = []
        positions: dict[str, int] = {}
        visited: set[str] = set()
        def visit(step_id: str) -> tuple[str, ...] | None:
            positions[step_id] = len(visiting)
            visiting.append(step_id)
            for child in dependents[step_id]:
                if child not in remaining:
                    continue
                if child in positions:
                    return tuple(visiting[positions[child]:] + [child])
                if child not in visited:
                    cycle = visit(child)
                    if cycle:
                        return cycle
            visiting.pop()
            positions.pop(step_id)
            visited.add(step_id)
            return None
        for step_id in sorted(remaining, key=index.__getitem__):
            if step_id not in visited:
                cycle = visit(step_id)
                if cycle:
                    return cycle
        raise AssertionError("Kahn 未完成时必须能找到依赖环")

    @staticmethod
    def _error(plan, error_code, message, **kwargs):
        return PlanGraphValidationError(error_code=error_code, message=message, plan=plan, **kwargs)
