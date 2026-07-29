#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RunCoordinator 所有权、调度、终态与真实入口测试。"""

from __future__ import annotations

import asyncio
import inspect
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path

from core.agent_router import AgentRouter
from core.chat_service import ChatService, _CoordinatedSingleAgentDriver
from core.memory_manager import MemoryManager
from core.runtime import (
    AgentState,
    AgentStateMachine,
    BudgetLedger,
    BudgetUsage,
    CancellationReason,
    InMemorySpanRecorder,
    ParallelExecutionInfrastructureError,
    ParallelExecutionPolicy,
    ParallelExecutor,
    ParallelFailureMode,
    Plan,
    PlanSource,
    PlanStep,
    RunBudget,
    RunCoordinator,
    RunCoordinatorError,
    RunDeadlineExceededError,
    RunFinalizationDecision,
    RunHandle,
    RunRegistry,
    RunStateEvent,
    RunEventType,
    RunStatus,
    SerialScheduler,
    StepEventType,
    StepExecutionMode,
    StepStateEvent,
    StepStatus,
    StopReason,
    TaskCapabilityRequirements,
    create_run_context,
)
from core.runtime.scheduler import SchedulerSnapshot


def make_plan(
    dependencies: dict[str, tuple[str, ...]],
    *,
    plan_id: str = "coordinator-plan",
) -> Plan:
    requirements = TaskCapabilityRequirements()
    return Plan(
        plan_id=plan_id,
        version=1,
        task_summary="安全任务摘要",
        steps=tuple(
            PlanStep(
                step_id,
                step_id,
                "安全步骤说明",
                depends_on,
                "步骤完成",
                "test_agent",
                requirements,
            )
            for step_id, depends_on in dependencies.items()
        ),
        created_at=datetime.now(UTC),
        source=PlanSource.DETERMINISTIC,
    )


class AsyncDriver:
    def __init__(
        self,
        *,
        failing: set[str] | None = None,
        cancelling: set[str] | None = None,
    ) -> None:
        self.failing = failing or set()
        self.cancelling = cancelling or set()
        self.calls: list[str] = []

    async def execute(self, claim, run_context):
        self.calls.append(claim.step_id)
        if claim.step_id in self.failing:
            raise RuntimeError("不得写入状态的原始业务异常")
        if claim.step_id in self.cancelling:
            raise asyncio.CancelledError()
        return claim.step_id


class RecordingScheduler(SerialScheduler):
    def __init__(self, machine: AgentStateMachine) -> None:
        super().__init__(machine)
        self.plan_objects: list[Plan] = []
        self.evaluate_count = 0

    def prepare(self, plan, state, occurred_at, max_parallelism=1):
        self.plan_objects.append(plan)
        return super().prepare(plan, state, occurred_at, max_parallelism)

    def evaluate(self, plan, state, max_parallelism=1):
        self.plan_objects.append(plan)
        self.evaluate_count += 1
        return super().evaluate(plan, state, max_parallelism)

    def claim_ready(
        self,
        plan,
        state,
        max_parallelism,
        occurred_at,
        *,
        budget_ledger=None,
    ):
        self.plan_objects.append(plan)
        return super().claim_ready(
            plan,
            state,
            max_parallelism,
            occurred_at,
            budget_ledger=budget_ledger,
        )


class CoordinatorFixture:
    def __init__(
        self,
        plan: Plan | None = None,
        *,
        budget: RunBudget | None = None,
        policy: ParallelExecutionPolicy | None = None,
        scheduler: SerialScheduler | None = None,
        executor: ParallelExecutor | None = None,
        machine: AgentStateMachine | None = None,
        span_recorder=None,
    ) -> None:
        self.context, self.source = create_run_context(entry_agent_id="test_agent")
        self.ledger = BudgetLedger(
            budget or RunBudget(),
            deadline_remaining=self.context.remaining_seconds,
        )
        self.context.attach_budget_ledger(self.ledger)
        self.state = AgentState.for_run_context(self.context.run_id)
        self.plan = plan or make_plan({"answer": ()})
        self.machine = machine or AgentStateMachine()
        self.scheduler = scheduler or RecordingScheduler(self.machine)
        self.executor = executor or ParallelExecutor(
            self.machine, max_concurrency=4
        )
        self.registry = RunRegistry()
        self.handle = RunHandle(
            self.context.run_id,
            self.source,
            self.state,
            "run_coordinator",
        )
        self.policy = policy or ParallelExecutionPolicy(
            2, ParallelFailureMode.BEST_EFFORT
        )
        self.coordinator = RunCoordinator(
            run_context=self.context,
            plan=self.plan,
            agent_state=self.state,
            budget_ledger=self.ledger,
            run_handle=self.handle,
            scheduler=self.scheduler,
            executor=self.executor,
            run_registry=self.registry,
            policy=self.policy,
            state_machine=self.machine,
            span_recorder=span_recorder,
        )


class RunCoordinatorOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_trace_active_span_invariant_after_success(self) -> None:
        recorder = InMemorySpanRecorder()
        fixture = CoordinatorFixture(span_recorder=recorder)
        result = await fixture.coordinator.execute(driver=AsyncDriver())
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(recorder.health_snapshot().active_span_count, 0)
        run = next(r for r in recorder.snapshot() if r.component == "runtime")
        step = next(r for r in recorder.snapshot() if r.component == "step")
        self.assertEqual(step.parent_span_id, run.span_id)

    async def test_single_step_success_and_per_run_ownership(self) -> None:
        fixture = CoordinatorFixture()
        result = await fixture.coordinator.execute(driver=AsyncDriver())
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.succeeded_step_ids, ("answer",))
        self.assertEqual(result.plan_id, fixture.plan.plan_id)
        self.assertIs(fixture.coordinator.plan, fixture.plan)
        self.assertIs(fixture.coordinator.budget_ledger, fixture.ledger)
        self.assertIs(
            fixture.context.cancellation_token,
            fixture.source.token,
        )
        self.assertIsNone(fixture.registry.get(fixture.context.run_id))

    async def test_execute_twice_is_rejected(self) -> None:
        fixture = CoordinatorFixture()
        await fixture.coordinator.execute(driver=AsyncDriver())
        with self.assertRaisesRegex(
            RunCoordinatorError, "COORDINATOR_ALREADY_EXECUTED"
        ):
            await fixture.coordinator.execute(driver=AsyncDriver())

    async def test_run_id_mismatch_is_rejected_before_registration(self) -> None:
        fixture = CoordinatorFixture()
        fixture.state.run_id = "other-run"
        with self.assertRaisesRegex(
            RunCoordinatorError, "COORDINATOR_RUN_ID_MISMATCH"
        ):
            await fixture.coordinator.execute(driver=AsyncDriver())
        self.assertEqual(fixture.registry.snapshot(), {})

    async def test_same_plan_object_reaches_every_scheduler_call(self) -> None:
        fixture = CoordinatorFixture(make_plan({"a": (), "b": ("a",)}))
        await fixture.coordinator.execute(driver=AsyncDriver())
        scheduler = fixture.scheduler
        self.assertGreaterEqual(len(scheduler.plan_objects), 4)
        self.assertTrue(all(item is fixture.plan for item in scheduler.plan_objects))

    async def test_context_must_hold_the_exact_same_ledger(self) -> None:
        fixture = CoordinatorFixture()
        fixture.coordinator.budget_ledger = BudgetLedger(RunBudget())
        with self.assertRaisesRegex(
            RunCoordinatorError, "COORDINATOR_BUDGET_OWNERSHIP_MISMATCH"
        ):
            await fixture.coordinator.execute(driver=AsyncDriver())


class RunCoordinatorSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_dependency_chain_uses_multiple_batches(self) -> None:
        fixture = CoordinatorFixture(
            make_plan({"first": (), "second": ("first",), "third": ("second",)})
        )
        driver = AsyncDriver()
        result = await fixture.coordinator.execute(driver=driver)
        self.assertEqual(driver.calls, ["first", "second", "third"])
        self.assertEqual(
            result.succeeded_step_ids, ("first", "second", "third")
        )
        self.assertGreaterEqual(fixture.scheduler.evaluate_count, 4)

    async def test_fork_then_join_waits_for_second_batch(self) -> None:
        fixture = CoordinatorFixture(
            make_plan(
                {
                    "root": (),
                    "left": ("root",),
                    "right": ("root",),
                    "join": ("left", "right"),
                }
            ),
            policy=ParallelExecutionPolicy(2, ParallelFailureMode.BEST_EFFORT),
        )
        driver = AsyncDriver()
        result = await fixture.coordinator.execute(driver=driver)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(driver.calls[0], "root")
        self.assertEqual(set(driver.calls[1:3]), {"left", "right"})
        self.assertEqual(driver.calls[3], "join")

    async def test_first_successful_batch_does_not_finalize_run(self) -> None:
        fixture = CoordinatorFixture(make_plan({"a": (), "b": ("a",)}))
        result = await fixture.coordinator.execute(driver=AsyncDriver())
        self.assertEqual(result.succeeded_step_ids, ("a", "b"))
        self.assertEqual(fixture.state.status, RunStatus.SUCCEEDED)

    async def test_best_effort_converges_failed_and_blocked_steps(self) -> None:
        fixture = CoordinatorFixture(
            make_plan({"bad": (), "good": (), "join": ("bad", "good")}),
            policy=ParallelExecutionPolicy(2, ParallelFailureMode.BEST_EFFORT),
        )
        result = await fixture.coordinator.execute(
            driver=AsyncDriver(failing={"bad"})
        )
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.failed_step_ids, ("bad",))
        self.assertEqual(result.succeeded_step_ids, ("good",))
        self.assertEqual(result.blocked_step_ids, ("join",))

    async def test_fail_fast_sibling_cancel_does_not_cancel_run(self) -> None:
        recorder = InMemorySpanRecorder()
        fixture = CoordinatorFixture(
            make_plan({"bad": (), "sibling": ()}),
            policy=ParallelExecutionPolicy(2, ParallelFailureMode.FAIL_FAST),
            span_recorder=recorder,
        )
        result = await fixture.coordinator.execute(
            driver=AsyncDriver(failing={"bad"})
        )
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.stop_reason, StopReason.UNHANDLED_ERROR)
        self.assertEqual(result.failed_step_ids, ("bad",))
        self.assertEqual(result.cancelled_step_ids, ("sibling",))
        self.assertFalse(fixture.source.token.is_cancelled())
        self.assertEqual(recorder.health_snapshot().active_span_count, 0)

    async def test_cancelled_step_without_run_token_converges_to_no_action(self) -> None:
        fixture = CoordinatorFixture()
        result = await fixture.coordinator.execute(
            driver=AsyncDriver(cancelling={"answer"})
        )
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.stop_reason, StopReason.NO_ACTION)
        self.assertEqual(result.cancelled_step_ids, ("answer",))
        self.assertLess(fixture.scheduler.evaluate_count, 5)


class RunCoordinatorDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_trace_active_span_invariant_after_cancel_and_timeout(self) -> None:
        cancelled_recorder = InMemorySpanRecorder()
        cancelled = CoordinatorFixture(span_recorder=cancelled_recorder)
        cancelled.source.cancel(CancellationReason.USER_CANCELLED)
        await cancelled.coordinator.execute(driver=AsyncDriver())
        self.assertEqual(cancelled_recorder.health_snapshot().active_span_count, 0)

        class DeadlineDriver:
            async def execute(self, claim, run_context):
                raise RunDeadlineExceededError("deadline")

        timeout_recorder = InMemorySpanRecorder()
        timed_out = CoordinatorFixture(span_recorder=timeout_recorder)
        await timed_out.coordinator.execute(driver=DeadlineDriver())
        self.assertEqual(timeout_recorder.health_snapshot().active_span_count, 0)

    async def test_run_level_cancellation_mapping(self) -> None:
        cases = (
            (
                CancellationReason.USER_CANCELLED,
                RunStatus.CANCELLED,
                StopReason.USER_CANCELLED,
            ),
            (
                CancellationReason.CLIENT_DISCONNECTED,
                RunStatus.CANCELLED,
                StopReason.CLIENT_DISCONNECTED,
            ),
            (
                CancellationReason.SYSTEM_SHUTDOWN,
                RunStatus.CANCELLED,
                StopReason.SYSTEM_SHUTDOWN,
            ),
            (
                CancellationReason.DEADLINE_EXCEEDED,
                RunStatus.FAILED,
                StopReason.DEADLINE_EXCEEDED,
            ),
        )
        for cancellation_reason, status, stop_reason in cases:
            with self.subTest(reason=cancellation_reason):
                fixture = CoordinatorFixture()
                fixture.source.cancel(cancellation_reason)
                result = await fixture.coordinator.execute(driver=AsyncDriver())
                self.assertEqual(result.status, status)
                self.assertEqual(result.stop_reason, stop_reason)
                self.assertFalse(fixture.state.active_step_ids)

    async def test_budget_exhaustion_uses_existing_budget_event(self) -> None:
        fixture = CoordinatorFixture(
            budget=RunBudget(max_step_starts=0)
        )
        result = await fixture.coordinator.execute(driver=AsyncDriver())
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.stop_reason, StopReason.BUDGET_EXHAUSTED)
        self.assertEqual(result.error_code, "BUDGET_EXHAUSTED")

    async def test_driver_budget_exhaustion_is_not_downgraded_to_step_failure(
        self,
    ) -> None:
        class BudgetDriver:
            async def execute(self, claim, run_context):
                run_context.budget_ledger.reserve(
                    BudgetUsage(model_calls=1),
                    reservation_type="model_call",
                )

        fixture = CoordinatorFixture(
            budget=RunBudget(max_model_calls=0)
        )
        result = await fixture.coordinator.execute(driver=BudgetDriver())
        self.assertEqual(result.stop_reason, StopReason.BUDGET_EXHAUSTED)
        self.assertEqual(result.cancelled_step_ids, ("answer",))
        self.assertFalse(fixture.state.active_step_ids)

    async def test_driver_deadline_error_keeps_deadline_stop_reason(self) -> None:
        class DeadlineDriver:
            async def execute(self, claim, run_context):
                raise RunDeadlineExceededError("deadline")

        fixture = CoordinatorFixture()
        result = await fixture.coordinator.execute(driver=DeadlineDriver())
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.stop_reason, StopReason.DEADLINE_EXCEEDED)
        self.assertEqual(result.cancelled_step_ids, ("answer",))

    async def test_executor_infrastructure_error_maps_to_failed(self) -> None:
        class BrokenExecutor:
            async def execute_ready(self, **kwargs):
                raise ParallelExecutionInfrastructureError(
                    "BROKEN_EXECUTOR", "执行器基础设施失败"
                )

        fixture = CoordinatorFixture(executor=BrokenExecutor())
        result = await fixture.coordinator.execute(driver=AsyncDriver())
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.error_code, "BROKEN_EXECUTOR")

    async def test_no_action_does_not_busy_loop(self) -> None:
        class BlockingScheduler(RecordingScheduler):
            def evaluate(self, plan, state, max_parallelism=1):
                self.evaluate_count += 1
                if state.steps["answer"].status == StepStatus.PENDING:
                    self._state_machine.apply_step_event(
                        state,
                        StepStateEvent(
                            StepEventType.BLOCKED,
                            "answer",
                            error_code="TEST_BLOCKED",
                            error_message="测试构造的安全阻塞",
                        ),
                    )
                return super().evaluate(plan, state, max_parallelism)

        machine = AgentStateMachine()
        scheduler = BlockingScheduler(machine)
        fixture = CoordinatorFixture(machine=machine, scheduler=scheduler)
        result = await fixture.coordinator.execute(driver=AsyncDriver())
        self.assertEqual(result.stop_reason, StopReason.NO_ACTION)
        self.assertEqual(scheduler.evaluate_count, 2)


class CountingStateMachine(AgentStateMachine):
    def __init__(self) -> None:
        self.terminal_event_count = 0
        self._count_lock = threading.Lock()

    def apply_run_event(self, state, event):
        if event.event_type != RunEventType.STARTED:
            with self._count_lock:
                self.terminal_event_count += 1
        return super().apply_run_event(state, event)


class RunCoordinatorFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_finalize_is_exactly_once_and_late_error_does_not_override(self) -> None:
        machine = CountingStateMachine()
        fixture = CoordinatorFixture(machine=machine)
        machine.apply_run_event(
            fixture.state, RunStateEvent(RunEventType.STARTED)
        )
        success = RunFinalizationDecision(
            RunStatus.SUCCEEDED,
            StopReason.COMPLETED,
            None,
            "运行已成功完成",
        )
        late_error = RunFinalizationDecision(
            RunStatus.FAILED,
            StopReason.UNHANDLED_ERROR,
            "LATE_ERROR",
            "运行未能完成",
        )
        first = fixture.coordinator._finalize_once(success)
        second = fixture.coordinator._finalize_once(late_error)
        self.assertIs(first, second)
        self.assertEqual(fixture.state.status, RunStatus.SUCCEEDED)
        self.assertEqual(machine.terminal_event_count, 1)

    async def test_finalize_lock_is_thread_safe(self) -> None:
        machine = CountingStateMachine()
        fixture = CoordinatorFixture(machine=machine)
        machine.apply_run_event(
            fixture.state, RunStateEvent(RunEventType.STARTED)
        )
        decisions = (
            RunFinalizationDecision(
                RunStatus.SUCCEEDED,
                StopReason.COMPLETED,
                None,
                "运行已成功完成",
            ),
            RunFinalizationDecision(
                RunStatus.FAILED,
                StopReason.UNHANDLED_ERROR,
                "COMPETING_ERROR",
                "运行未能完成",
            ),
        )
        barrier = threading.Barrier(3)
        results = []

        def finalize(decision):
            barrier.wait()
            results.append(fixture.coordinator._finalize_once(decision))

        threads = [
            threading.Thread(target=finalize, args=(decision,))
            for decision in decisions
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(machine.terminal_event_count, 1)

    async def test_active_step_is_cancelled_before_run_finalization(self) -> None:
        class OrphanScheduler(RecordingScheduler):
            def evaluate(self, plan, state, max_parallelism=1):
                self.evaluate_count += 1
                if state.steps["answer"].status == StepStatus.PENDING:
                    self._state_machine.apply_step_event(
                        state,
                        StepStateEvent(StepEventType.STARTED, "answer"),
                    )
                return SchedulerSnapshot(
                    ready_step_ids=(),
                    running_step_ids=("answer",),
                    pending_step_ids=(),
                    blocked_step_ids=(),
                    terminal_step_ids=(),
                    is_complete=False,
                    is_waiting=True,
                    has_unresolved_pending=False,
                    claimable_step_ids=(),
                    max_parallelism=1,
                    available_slots=0,
                )

        machine = AgentStateMachine()
        fixture = CoordinatorFixture(
            machine=machine, scheduler=OrphanScheduler(machine)
        )
        result = await fixture.coordinator.execute(driver=AsyncDriver())
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.cancelled_step_ids, ("answer",))
        self.assertFalse(fixture.state.active_step_ids)


class RunCoordinatorCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_callbacks_are_lifo_and_continue_after_failure(self) -> None:
        fixture = CoordinatorFixture()
        calls = []

        def first():
            calls.append("first")

        def failing():
            calls.append("failing")
            raise RuntimeError("清理私密错误")

        async def last():
            calls.append("last")

        fixture.coordinator.add_cleanup_callback(first)
        fixture.coordinator.add_cleanup_callback(failing)
        fixture.coordinator.add_cleanup_callback(last)
        result = await fixture.coordinator.execute(driver=AsyncDriver())
        self.assertEqual(calls, ["last", "failing", "first"])
        self.assertEqual(
            result.cleanup_error_codes, ("RUN_CLEANUP_CALLBACK_FAILED",)
        )
        self.assertEqual(result.stop_reason, StopReason.COMPLETED)

    async def test_registry_stays_registered_through_callbacks_then_unregisters(self) -> None:
        fixture = CoordinatorFixture()
        seen = []
        fixture.coordinator.add_cleanup_callback(
            lambda: seen.append(
                fixture.registry.get(fixture.context.run_id) is fixture.handle
            )
        )
        await fixture.coordinator.execute(driver=AsyncDriver())
        self.assertEqual(seen, [True])
        self.assertIsNone(fixture.registry.get(fixture.context.run_id))

    async def test_reservation_leak_is_reported_but_not_released(self) -> None:
        fixture = CoordinatorFixture()
        fixture.ledger.reserve(
            BudgetUsage(),
            reservation_type="driver_owned",
        )
        result = await fixture.coordinator.execute(driver=AsyncDriver())
        self.assertIn("BUDGET_RESERVATION_LEAK", result.cleanup_error_codes)
        self.assertEqual(result.budget_snapshot.active_reservation_count, 1)
        self.assertEqual(fixture.ledger.snapshot().active_reservation_count, 1)


class RunCoordinatorRealEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_service_real_single_agent_path_uses_one_long_lived_plan(
        self,
    ) -> None:
        class FakeModel:
            def __init__(self) -> None:
                self.calls = 0

            def generate(self, messages, **kwargs):
                self.calls += 1
                yield "coordinated answer"

        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryManager(str(Path(directory) / "memory.db"))
            model = FakeModel()
            router = AgentRouter(
                llm_engine=model,
                memory_manager=memory,
                orchestration_enabled=False,
            )
            plan_count = 0
            original = router.build_single_agent_plan

            def count_plan(agent_id, query):
                nonlocal plan_count
                plan_count += 1
                return original(agent_id, query)

            router.build_single_agent_plan = count_plan
            states = []
            service = ChatService(router, state_observer=states.append)
            output, result = await service.run_coordinated_agent(
                "core_router", "检查运行时所有权", persist=False
            )
        self.assertEqual(output, "coordinated answer")
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(plan_count, 1)
        self.assertEqual(model.calls, 1)
        self.assertEqual(states[-1].status, RunStatus.SUCCEEDED)

    def test_new_driver_has_no_lifecycle_or_registry_writes(self) -> None:
        source = inspect.getsource(_CoordinatedSingleAgentDriver.execute)
        for forbidden in (
            "RunEventType.STARTED",
            "apply_run_event",
            "apply_step_event",
            "register(",
            "unregister(",
        ):
            self.assertNotIn(forbidden, source)

    def test_legacy_stream_entry_remains_available(self) -> None:
        self.assertTrue(hasattr(ChatService, "stream_chat"))
        self.assertTrue(hasattr(ChatService, "run_coordinated_agent"))


if __name__ == "__main__":
    unittest.main()
