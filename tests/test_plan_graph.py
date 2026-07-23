#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PlanGraph 静态 DAG 校验与稳定顺序测试。"""

from datetime import UTC, datetime

import pytest

from core.runtime import Plan, PlanGraphValidationError, PlanGraphValidator, PlanSource, PlanStep, TaskCapabilityRequirements


def make_step(step_id: str, depends_on: tuple[str, ...] = ()) -> PlanStep:
    return PlanStep(step_id, f"步骤 {step_id}", "静态说明", depends_on, "完成", "agent", TaskCapabilityRequirements())


def make_plan(*steps: PlanStep) -> Plan:
    return Plan("graph-plan", 1, "不得进入图错误", steps, datetime.now(UTC), PlanSource.DETERMINISTIC)


@pytest.mark.parametrize("steps, expected_order, roots, leaves", [
    ((make_step("a"),), ("a",), ("a",), ("a",)),
    ((make_step("b", ("a",)), make_step("a")), ("a", "b"), ("a",), ("b",)),
    ((make_step("a"), make_step("b", ("a",)), make_step("c", ("a",))), ("a", "b", "c"), ("a",), ("b", "c")),
    ((make_step("a"), make_step("b"), make_step("c", ("a", "b"))), ("a", "b", "c"), ("a", "b"), ("c",)),
    ((make_step("b"), make_step("a"), make_step("d", ("c",)), make_step("c")), ("b", "a", "c", "d"), ("b", "a", "c"), ("b", "a", "d")),
])
def test_legal_graphs_are_stable(steps, expected_order, roots, leaves) -> None:
    plan = make_plan(*steps)
    before = plan.steps
    graph = PlanGraphValidator.validate(plan)
    assert graph.topological_order == expected_order
    assert graph.root_step_ids == roots
    assert graph.leaf_step_ids == leaves
    assert plan.steps == before
    assert PlanGraphValidator.validate(plan) == graph


@pytest.mark.parametrize("steps, error_code", [
    ((make_step("a"), make_step("a")), "DUPLICATE_STEP_ID"),
    ((make_step("a", ("missing",)),), "MISSING_DEPENDENCY"),
    ((make_step("a", ("a",)),), "SELF_DEPENDENCY"),
    ((make_step("a"), make_step("b", ("a", "a"))), "DUPLICATE_DEPENDENCY"),
    ((make_step("a", ("b",)), make_step("b", ("a",))), "DEPENDENCY_CYCLE"),
    ((make_step("a", ("b",)), make_step("b", ("c",)), make_step("c", ("a",))), "DEPENDENCY_CYCLE"),
])
def test_illegal_graphs_have_safe_error_codes(steps, error_code) -> None:
    with pytest.raises(PlanGraphValidationError) as captured:
        PlanGraphValidator.validate(make_plan(*steps))
    assert captured.value.error_code == error_code


def test_cycle_diagnostics_exclude_downstream_and_are_stable() -> None:
    plan = make_plan(make_step("a", ("b",)), make_step("b", ("a",)), make_step("downstream", ("a",)))
    errors = []
    for _ in range(3):
        with pytest.raises(PlanGraphValidationError) as captured:
            PlanGraphValidator.validate(plan)
        errors.append(captured.value)
    assert errors[0].unresolved_step_ids == ("a", "b", "downstream")
    assert errors[0].cycle_path == ("a", "b", "a")
    assert "downstream" not in errors[0].cycle_path
    assert [error.cycle_path for error in errors] == [("a", "b", "a")] * 3


def test_graph_mappings_are_read_only_and_queries_are_safe() -> None:
    graph = PlanGraphValidator.validate(make_plan(make_step("a"), make_step("b", ("a",))))
    assert graph.dependencies_of("b") == ("a",)
    assert graph.dependents_of("a") == ("b",)
    assert not graph.contains_step("unknown")
    with pytest.raises(TypeError):
        graph.dependencies["a"] = ()
