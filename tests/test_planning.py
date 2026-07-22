from dataclasses import fields
from datetime import datetime, timezone

import pytest

from core.runtime.planning import Plan, PlanSource, PlanStep, PlanValidator, RiskLevel, TaskCapabilityRequirements, create_single_step_plan


def test_valid_single_and_multi_step_plans_are_accepted() -> None:
    req = TaskCapabilityRequirements(estimated_steps=2, risk_level=RiskLevel.MEDIUM)
    first = PlanStep("first", "收集信息", "收集完成任务所需的输入。", (), "已获得必要输入。", "core_router", req)
    second = PlanStep("second", "生成结论", "依据输入形成结论。", ("first",), "已生成结论。", "core_router", req)
    plan = Plan("plan-1", 1, "完成当前任务。", (first, second), datetime.now(timezone.utc), PlanSource.DETERMINISTIC)
    PlanValidator.validate(plan)
    assert create_single_step_plan("knowledge_expert", TaskCapabilityRequirements()).steps[0].step_id == "answer"


@pytest.mark.parametrize("plan_id,version", [("", 1), ("plan", 0), ("plan", True)])
def test_plan_rejects_invalid_identity_or_version(plan_id, version) -> None:
    plan = Plan(plan_id, version, "任务", (), datetime.now(timezone.utc), PlanSource.DETERMINISTIC)
    with pytest.raises(ValueError): PlanValidator.validate(plan)


def test_plan_validator_rejects_dependencies_and_naive_datetime() -> None:
    req = TaskCapabilityRequirements()
    duplicate = PlanStep("a", "步骤", "说明", ("a", "a"), "完成", "agent", req)
    plan = Plan("p", 1, "任务", (duplicate,), datetime.now(), PlanSource.DETERMINISTIC)
    with pytest.raises(ValueError): PlanValidator.validate(plan)


def test_capability_validation_and_static_plan_fields() -> None:
    with pytest.raises(ValueError): TaskCapabilityRequirements(estimated_steps=True)
    with pytest.raises(ValueError): TaskCapabilityRequirements(requires_rag=1)
    names = {field.name for field in fields(PlanStep)}
    assert not names & {"status", "started_at", "ended_at", "error_message", "provider", "model_name", "base_url"}
    plan = create_single_step_plan("core_router", TaskCapabilityRequirements())
    with pytest.raises(Exception): plan.version = 2
