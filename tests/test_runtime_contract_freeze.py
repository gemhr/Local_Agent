from __future__ import annotations

import inspect
from dataclasses import fields

import core.runtime as runtime
from core.runtime import AgentState, ApplicationRuntimeServices, PlanStep
from core.runtime.recovery_validation import RecoveryValidator


def test_plan_definition_and_runtime_state_have_disjoint_owners() -> None:
    plan_fields = {item.name for item in fields(PlanStep)}
    state_fields = {item.name for item in fields(AgentState)}

    assert plan_fields.isdisjoint(
        {"status", "runtime_status", "attempt_count", "error_code"}
    )
    assert {"status", "steps", "active_step_ids", "stop_reason"} <= state_fields


def test_application_container_has_no_per_run_or_operation_controller_field() -> None:
    names = {item.name for item in fields(ApplicationRuntimeServices)}

    assert not names.intersection(
        {
            "run_context",
            "agent_state",
            "event_channel",
            "current_run",
            "run_scope",
            "fault_controller",
        }
    )


def test_recovery_validator_is_read_only_and_test_fixture_is_not_public() -> None:
    source = inspect.getsource(RecoveryValidator)

    assert "AgentState" not in source
    assert "agent_state" not in source
    assert not hasattr(runtime, "ToolCompletionGapFixture")
