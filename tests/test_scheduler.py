#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""最小串行 Scheduler 的单元与真实状态机集成测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime
import inspect
import pickle
from threading import Barrier
import unittest

from core.runtime import (
    AgentState,
    AgentStateMachine,
    Plan,
    PlanGraphValidationError,
    PlanSource,
    PlanStep,
    RiskLevel,
    RunEventType,
    RunStateEvent,
    RunStatus,
    SchedulerClaimError,
    SchedulerPlanStateMismatchError,
    SchedulerSnapshot,
    SerialScheduler,
    StepClaim,
    StepEventType,
    StepStateEvent,
    StepStatus,
    TaskCapabilityRequirements,
)


class RecordingStateMachine(AgentStateMachine):
    """记录 Scheduler 交给真实状态机的 Step 事件。"""

    def __init__(self) -> None:
        self.added_step_ids: list[str] = []
        self.step_events: list[StepEventType] = []

    def add_step(self, state: AgentState, *, step_id: str, name: str) -> None:
        self.added_step_ids.append(step_id)
        super().add_step(state, step_id=step_id, name=name)

    def apply_step_event(self, state: AgentState, event: StepStateEvent) -> None:
        self.step_events.append(event.event_type)
        super().apply_step_event(state, event)


class RejectingStartStateMachine(AgentStateMachine):
    """在修改状态前拒绝 STARTED，用于验证 Claim 失败关闭。"""

    def apply_step_event(self, state: AgentState, event: StepStateEvent) -> None:
        if event.event_type == StepEventType.STARTED:
            raise RuntimeError("模拟 STARTED 失败")
        super().apply_step_event(state, event)


def requirement(*, requires_rag: bool = False) -> TaskCapabilityRequirements:
    return TaskCapabilityRequirements(
        requires_rag=requires_rag,
        risk_level=RiskLevel.LOW,
        estimated_steps=1,
    )


def step(
    step_id: str,
    *,
    title: str | None = None,
    depends_on: tuple[str, ...] = (),
    preferred_agent: str = "core_router",
    capabilities: TaskCapabilityRequirements | None = None,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        title=title or f"步骤 {step_id}",
        description="仅供 Planner 使用的静态说明。",
        depends_on=depends_on,
        completion_criteria="执行层确认成功。",
        preferred_agent=preferred_agent,
        capability_requirements=capabilities or requirement(),
    )


def plan(*steps: PlanStep, plan_id: str = "plan-1", version: int = 1) -> Plan:
    return Plan(
        plan_id=plan_id,
        version=version,
        task_summary="不应进入 AgentState 或 Scheduler 异常。",
        steps=tuple(steps),
        created_at=datetime.now(UTC),
        source=PlanSource.DETERMINISTIC,
    )


def running_state(run_id: str = "run-1") -> AgentState:
    state = AgentState.for_run_context(run_id)
    AgentStateMachine().apply_run_event(state, RunStateEvent(RunEventType.STARTED))
    return state


def finish_running(
    state: AgentState,
    step_id: str,
    event_type: StepEventType,
) -> None:
    error_code = (
        None if event_type == StepEventType.SUCCEEDED else f"STEP_{event_type.value}"
    )
    AgentStateMachine().apply_step_event(
        state,
        StepStateEvent(
            event_type,
            step_id,
            error_code=error_code,
            error_message="安全测试摘要" if error_code else None,
        ),
    )


class SchedulerPrepareTests(unittest.TestCase):
    def test_prepare_registers_all_steps_and_is_idempotent(self) -> None:
        machine = RecordingStateMachine()
        scheduler = SerialScheduler(machine)
        state = running_state()
        source_plan = plan(step("a"), step("b", depends_on=("a",)))
        before_plan = source_plan
        before_fields = tuple(field.name for field in fields(AgentState))

        first = scheduler.prepare(source_plan, state, datetime.now(UTC))
        second = scheduler.prepare(source_plan, state, datetime.now(UTC))

        self.assertEqual(machine.added_step_ids, ["a", "b"])
        self.assertEqual(tuple(state.steps), ("a", "b"))
        self.assertTrue(
            all(item.status == StepStatus.PENDING for item in state.steps.values())
        )
        self.assertEqual(state.steps["a"].name, "步骤 a")
        self.assertEqual(first, second)
        self.assertIs(source_plan, before_plan)
        self.assertEqual(
            tuple(field.name for field in fields(AgentState)), before_fields
        )
        self.assertNotIn("description", state.steps["a"].to_dict())
        self.assertNotIn("completion_criteria", state.steps["a"].to_dict())

    def test_prepare_rejects_name_conflict_without_partial_registration(self) -> None:
        state = running_state()
        machine = AgentStateMachine()
        machine.add_step(state, step_id="b", name="冲突名称")
        source_plan = plan(step("a"), step("b"))

        with self.assertRaises(SchedulerPlanStateMismatchError) as captured:
            SerialScheduler(machine).prepare(source_plan, state, datetime.now(UTC))

        self.assertNotIn("a", state.steps)
        self.assertEqual(captured.exception.error_code, "SCHEDULER_PLAN_STATE_MISMATCH")
        self.assertNotIn(source_plan.task_summary, str(captured.exception))

    def test_prepare_rejects_non_running_run(self) -> None:
        state = AgentState.for_run_context("run-created")
        with self.assertRaises(SchedulerPlanStateMismatchError):
            SerialScheduler().prepare(plan(step("a")), state, datetime.now(UTC))
        self.assertEqual(state.steps, {})

    def test_scheduler_instance_rejects_other_plan_version_or_run(self) -> None:
        source_steps = (step("a"),)
        state = running_state()
        scheduler = SerialScheduler()
        scheduler.prepare(plan(*source_steps), state, datetime.now(UTC))

        with self.assertRaises(SchedulerPlanStateMismatchError):
            scheduler.prepare(plan(*source_steps, version=2), state, datetime.now(UTC))
        with self.assertRaises(SchedulerPlanStateMismatchError):
            scheduler.prepare(
                plan(*source_steps), running_state("run-2"), datetime.now(UTC)
            )


class SchedulerReadyAndSnapshotTests(unittest.TestCase):
    def test_ready_rules_and_plan_order_are_stable(self) -> None:
        source_plan = plan(step("z"), step("a"), step("m", depends_on=("z",)))
        state = running_state()
        scheduler = SerialScheduler()
        scheduler.prepare(source_plan, state, datetime.now(UTC))

        snapshots = [scheduler.evaluate(source_plan, state) for _ in range(5)]

        self.assertTrue(all(item.ready_step_ids == ("z", "a") for item in snapshots))
        self.assertEqual(snapshots[0].pending_step_ids, ("z", "a", "m"))
        self.assertFalse(snapshots[0].has_unresolved_pending)

    def test_dependency_pending_and_running_wait_without_blocking(self) -> None:
        source_plan = plan(step("a"), step("b", depends_on=("a",)))
        state = running_state()
        scheduler = SerialScheduler()
        scheduler.prepare(source_plan, state, datetime.now(UTC))
        first = scheduler.evaluate(source_plan, state)
        claim = scheduler.claim_next(source_plan, state, datetime.now(UTC))
        running = scheduler.evaluate(source_plan, state)

        self.assertEqual(first.ready_step_ids, ("a",))
        self.assertEqual(claim.step_id if claim else None, "a")
        self.assertEqual(running.ready_step_ids, ())
        self.assertEqual(running.blocked_step_ids, ())
        self.assertTrue(running.is_waiting)
        self.assertFalse(running.is_complete)
        self.assertFalse(running.has_unresolved_pending)

    def test_success_releases_downstream_in_plan_order(self) -> None:
        source_plan = plan(
            step("root"),
            step("second", depends_on=("root",)),
            step("third", depends_on=("root",)),
        )
        state = running_state()
        scheduler = SerialScheduler()
        root_claim = scheduler.claim_next(source_plan, state, datetime.now(UTC))
        self.assertEqual(root_claim.step_id if root_claim else None, "root")
        finish_running(state, "root", StepEventType.SUCCEEDED)

        released = scheduler.evaluate(source_plan, state)

        self.assertEqual(released.ready_step_ids, ("second", "third"))
        self.assertEqual(
            scheduler.claim_next(source_plan, state, datetime.now(UTC)).step_id,
            "second",
        )

    def test_only_all_succeeded_is_complete_and_scheduler_keeps_run_running(
        self,
    ) -> None:
        source_plan = plan(step("a"))
        state = running_state()
        scheduler = SerialScheduler()
        claim = scheduler.claim_next(source_plan, state, datetime.now(UTC))
        self.assertIsNotNone(claim)
        finish_running(state, "a", StepEventType.SUCCEEDED)

        snapshot = scheduler.evaluate(source_plan, state)

        self.assertTrue(snapshot.is_complete)
        self.assertEqual(snapshot.terminal_step_ids, ("a",))
        self.assertEqual(state.status, RunStatus.RUNNING)

    def test_failed_or_blocked_is_not_successful_completion(self) -> None:
        source_plan = plan(step("a"), step("b", depends_on=("a",)))
        state = running_state()
        scheduler = SerialScheduler()
        scheduler.claim_next(source_plan, state, datetime.now(UTC))
        finish_running(state, "a", StepEventType.FAILED)

        snapshot = scheduler.evaluate(source_plan, state)

        self.assertFalse(snapshot.is_complete)
        self.assertEqual(snapshot.blocked_step_ids, ("b",))
        self.assertEqual(snapshot.terminal_step_ids, ("a", "b"))
        self.assertFalse(snapshot.has_unresolved_pending)
        self.assertEqual(state.status, RunStatus.RUNNING)

    def test_cycle_is_rejected_before_any_step_registration(self) -> None:
        source_plan = plan(step("a", depends_on=("b",)), step("b", depends_on=("a",)))
        state = running_state()
        machine = RecordingStateMachine()

        before = state.to_dict()
        with self.assertRaises(PlanGraphValidationError) as captured:
            SerialScheduler(machine).prepare(source_plan, state, datetime.now(UTC))

        self.assertEqual(captured.exception.error_code, "DEPENDENCY_CYCLE")
        self.assertEqual(state.to_dict(), before)
        self.assertEqual(machine.added_step_ids, [])
        self.assertEqual(machine.step_events, [])

    def test_claim_next_rejects_invalid_graph_before_registration(self) -> None:
        source_plan = plan(step("a", depends_on=("missing",)))
        state = running_state()
        before = state.to_dict()
        with self.assertRaises(PlanGraphValidationError):
            SerialScheduler().claim_next(source_plan, state, datetime.now(UTC))
        self.assertEqual(state.to_dict(), before)


class SchedulerBlockedPropagationTests(unittest.TestCase):
    def test_all_blocking_dependency_statuses_propagate(self) -> None:
        for event_type in (
            StepEventType.FAILED,
            StepEventType.CANCELLED,
            StepEventType.SKIPPED,
        ):
            with self.subTest(event_type=event_type):
                source_plan = plan(step("a"), step("b", depends_on=("a",)))
                state = running_state(f"run-{event_type.value}")
                scheduler = SerialScheduler()
                scheduler.prepare(source_plan, state, datetime.now(UTC))
                if event_type == StepEventType.SKIPPED:
                    AgentStateMachine().apply_step_event(
                        state,
                        StepStateEvent(StepEventType.SKIPPED, "a"),
                    )
                else:
                    scheduler.claim_next(source_plan, state, datetime.now(UTC))
                    finish_running(state, "a", event_type)

                snapshot = scheduler.evaluate(source_plan, state)

                self.assertEqual(snapshot.blocked_step_ids, ("b",))
                self.assertEqual(
                    state.steps["b"].error_code, "DEPENDENCY_NOT_SUCCESSFUL"
                )

    def test_blocked_propagates_to_fixed_point_without_duplicate_events(self) -> None:
        machine = RecordingStateMachine()
        source_plan = plan(
            step("a"),
            step("b", depends_on=("a",)),
            step("c", depends_on=("b",)),
            step("d", depends_on=("c",)),
        )
        state = running_state()
        scheduler = SerialScheduler(machine)
        scheduler.prepare(source_plan, state, datetime.now(UTC))
        scheduler.claim_next(source_plan, state, datetime.now(UTC))
        finish_running(state, "a", StepEventType.FAILED)

        first = scheduler.evaluate(source_plan, state)
        blocked_events_after_first = machine.step_events.count(StepEventType.BLOCKED)
        second = scheduler.evaluate(source_plan, state)

        self.assertEqual(first.blocked_step_ids, ("b", "c", "d"))
        self.assertEqual(second.blocked_step_ids, first.blocked_step_ids)
        self.assertEqual(blocked_events_after_first, 3)
        self.assertEqual(machine.step_events.count(StepEventType.BLOCKED), 3)

    def test_one_failed_dependency_blocks_and_pending_dependency_does_not(self) -> None:
        source_plan = plan(
            step("ok"),
            step("bad"),
            step("target", depends_on=("ok", "bad")),
        )
        state = running_state()
        scheduler = SerialScheduler()
        scheduler.prepare(source_plan, state, datetime.now(UTC))
        AgentStateMachine().apply_step_event(
            state, StepStateEvent(StepEventType.SKIPPED, "bad")
        )

        snapshot = scheduler.evaluate(source_plan, state)

        self.assertEqual(state.steps["ok"].status, StepStatus.PENDING)
        self.assertEqual(snapshot.blocked_step_ids, ("target",))


class SchedulerClaimTests(unittest.TestCase):
    def test_claim_uses_state_machine_and_returns_safe_immutable_value(self) -> None:
        machine = RecordingStateMachine()
        capabilities = requirement(requires_rag=True)
        source_plan = plan(
            step(
                "answer",
                preferred_agent="knowledge_expert",
                capabilities=capabilities,
            )
        )
        state = running_state()
        scheduler = SerialScheduler(machine)

        claim = scheduler.claim_next(source_plan, state, datetime.now(UTC))

        self.assertIsInstance(claim, StepClaim)
        self.assertEqual(claim.plan_id, "plan-1")
        self.assertEqual(claim.plan_version, 1)
        self.assertEqual(claim.step_id, "answer")
        self.assertEqual(claim.capability_requirements, capabilities)
        self.assertEqual(claim.preferred_agent, "knowledge_expert")
        self.assertEqual(claim.claimed_at.utcoffset().total_seconds(), 0)
        self.assertEqual(state.steps["answer"].status, StepStatus.RUNNING)
        self.assertEqual(state.active_step_ids, {"answer"})
        self.assertEqual(machine.step_events, [StepEventType.STARTED])
        with self.assertRaises(FrozenInstanceError):
            claim.step_id = "changed"

    def test_existing_running_step_prevents_duplicate_claim(self) -> None:
        source_plan = plan(step("a"), step("b"))
        state = running_state()
        scheduler = SerialScheduler()

        first = scheduler.claim_next(source_plan, state, datetime.now(UTC))
        second = scheduler.claim_next(source_plan, state, datetime.now(UTC))

        self.assertEqual(first.step_id if first else None, "a")
        self.assertIsNone(second)
        self.assertEqual(
            [item.status for item in state.steps.values()],
            [StepStatus.RUNNING, StepStatus.PENDING],
        )

    def test_two_threads_can_only_claim_once(self) -> None:
        source_plan = plan(step("a"))
        state = running_state()
        scheduler = SerialScheduler()
        barrier = Barrier(2)

        def claim() -> StepClaim | None:
            barrier.wait()
            return scheduler.claim_next(source_plan, state, datetime.now(UTC))

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _: claim(), range(2)))

        self.assertEqual(sum(item is not None for item in claims), 1)
        self.assertEqual(state.steps["a"].status, StepStatus.RUNNING)
        self.assertEqual(state.active_step_ids, {"a"})

    def test_start_failure_returns_no_fake_claim_and_keeps_state(self) -> None:
        source_plan = plan(step("a"))
        state = running_state()
        scheduler = SerialScheduler(RejectingStartStateMachine())
        scheduler.prepare(source_plan, state, datetime.now(UTC))
        before = state.to_dict()

        with self.assertRaises(SchedulerClaimError) as captured:
            scheduler.claim_next(source_plan, state, datetime.now(UTC))

        self.assertEqual(state.to_dict(), before)
        self.assertEqual(captured.exception.error_code, "SCHEDULER_STEP_CLAIM_FAILED")

    def test_snapshot_and_scheduler_types_are_immutable_or_non_serializing(
        self,
    ) -> None:
        snapshot = SchedulerSnapshot((), (), (), (), (), False, False, False)
        with self.assertRaises(FrozenInstanceError):
            snapshot.is_complete = True
        with self.assertRaises(TypeError):
            pickle.dumps(SerialScheduler())


class SchedulerBoundaryTests(unittest.TestCase):
    def test_scheduler_has_no_model_selection_provider_or_execution_logic(self) -> None:
        source = inspect.getsource(inspect.getmodule(SerialScheduler))
        forbidden = (
            "ModelSelectionPolicy",
            "ModelResolver",
            "DeepSeek",
            "Qwen",
            "final_output",
            "execute(",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_public_surface_is_prepare_evaluate_and_claim_without_fallback(
        self,
    ) -> None:
        methods = {
            name for name, _ in inspect.getmembers(SerialScheduler, inspect.isfunction)
        }
        self.assertTrue({"prepare", "evaluate", "claim_next"}.issubset(methods))
        self.assertNotIn("fallback", methods)
        self.assertNotIn("retry", methods)
        self.assertFalse(any("cycle" in name for name in methods))


if __name__ == "__main__":
    unittest.main()
