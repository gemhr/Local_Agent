from dataclasses import replace
from datetime import UTC, datetime, timedelta

from core.runtime.plan_fingerprint import PlanFingerprinter
from core.runtime.planning import (
    ExecutionKind,
    OutputPolicy,
    Plan,
    PlanSource,
    PlanStep,
    TaskCapabilityRequirements,
)
from core.runtime.snapshot_contract import PlanSnapshot, PlanStepSnapshot
from core.runtime.state import AgentState


def _step(
    step_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    capability: TaskCapabilityRequirements | None = None,
    criteria: str = "done",
    title: str = "title",
    agent: str = "router",
    execution_kind: ExecutionKind = ExecutionKind.AGENT,
    output_policy: OutputPolicy = OutputPolicy.FINAL_PASSTHROUGH,
) -> PlanStep:
    return PlanStep(
        step_id,
        title,
        "description",
        dependencies,
        criteria,
        agent,
        capability or TaskCapabilityRequirements(),
        execution_kind=execution_kind,
        output_policy=output_policy,
    )


def _plan(
    steps: tuple[PlanStep, ...], *, created_at: datetime | None = None
) -> Plan:
    return Plan(
        "plan",
        1,
        "summary",
        steps,
        created_at or datetime(2026, 1, 1, tzinfo=UTC),
        PlanSource.DETERMINISTIC,
    )


def test_same_semantics_across_instances_order_and_runtime_time():
    first = _plan((_step("a"), _step("b", dependencies=("a",))))
    second = _plan(
        (_step("b", dependencies=("a",)), _step("a")),
        created_at=first.created_at + timedelta(days=1),
    )
    assert PlanFingerprinter.fingerprint(first) == PlanFingerprinter.fingerprint(
        second
    )


def test_dependency_order_is_canonical():
    first = _plan(
        (
            _step("a"),
            _step("b"),
            _step("c", dependencies=("a", "b")),
        )
    )
    second = replace(
        first,
        steps=(
            _step("c", dependencies=("b", "a")),
            _step("b"),
            _step("a"),
        ),
    )
    assert PlanFingerprinter.fingerprint(first) == PlanFingerprinter.fingerprint(
        second
    )


def test_static_mapping_order_is_canonical():
    snapshot = PlanSnapshot.from_plan(_plan((_step("a"),)))
    step = snapshot.steps[0]
    reordered = PlanStepSnapshot(
        step_id=step.step_id,
        agent=step.agent,
        dependency_step_ids=step.dependency_step_ids,
        static_execution_kind=step.static_execution_kind,
        capability_requirements=dict(reversed(tuple(step.capability_requirements.items()))),
        completion_criteria=step.completion_criteria,
        static_inputs=dict(reversed(tuple(step.static_inputs.items()))),
    )
    assert PlanFingerprinter.fingerprint_snapshot(snapshot) == (
        PlanFingerprinter.fingerprint_snapshot(replace(snapshot, steps=(reordered,)))
    )


def test_each_static_definition_change_changes_fingerprint():
    base = _plan((_step("a"), _step("b", dependencies=("a",))))
    variants = (
        replace(base, steps=(_step("x"), _step("b", dependencies=("x",)))),
        replace(base, steps=(_step("a"), _step("b"))),
        replace(
            base,
            steps=(
                _step("a"),
                _step(
                    "b",
                    dependencies=("a",),
                    capability=TaskCapabilityRequirements(requires_tools=True),
                ),
            ),
        ),
        replace(
            base,
            steps=(_step("a"), _step("b", dependencies=("a",), criteria="other")),
        ),
        replace(
            base,
            steps=(_step("a"), _step("b", dependencies=("a",), title="other")),
        ),
        replace(
            base,
            steps=(_step("a"), _step("b", dependencies=("a",), agent="other")),
        ),
    )
    fingerprint = PlanFingerprinter.fingerprint(base)
    assert all(PlanFingerprinter.fingerprint(item) != fingerprint for item in variants)


def test_runtime_step_status_is_outside_fingerprint_owner():
    plan = _plan((_step("a"),))
    before = PlanFingerprinter.fingerprint(plan)
    state = AgentState("run")
    state.add_step("a", "runtime")
    state.mark_running()
    state.start_step("a")
    assert PlanFingerprinter.fingerprint(plan) == before


def test_execution_kind_output_policy_and_schema_change_fingerprint():
    base = _plan((_step("a"),))
    execution_variant = _plan(
        (_step("a", execution_kind=ExecutionKind.SYNTHESIS),)
    )
    output_variant = _plan(
        (_step("a", output_policy=OutputPolicy.INTERNAL),)
    )
    fingerprint = PlanFingerprinter.fingerprint(base)
    assert PlanFingerprinter.fingerprint(execution_variant) != fingerprint
    assert PlanFingerprinter.fingerprint(output_variant) != fingerprint
    snapshot = PlanSnapshot.from_plan(base)
    assert PlanFingerprinter.fingerprint_snapshot(
        replace(snapshot, plan_schema_version=1)
    ) != fingerprint


def test_v1_fingerprint_ignores_v2_output_policy_field() -> None:
    snapshot = replace(
        PlanSnapshot.from_plan(_plan((_step("a"),))),
        plan_schema_version=1,
    )
    changed = replace(
        snapshot,
        steps=(replace(snapshot.steps[0], output_policy="INTERNAL"),),
    )
    assert PlanFingerprinter.fingerprint_snapshot(snapshot) == (
        PlanFingerprinter.fingerprint_snapshot(changed)
    )
