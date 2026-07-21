#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AgentStateMachine 的状态转移、Guard 与原子性测试。"""

from __future__ import annotations

from datetime import datetime
import unittest

from core.runtime import (
    AgentState,
    AgentStateMachine,
    InvalidStateTransitionError,
    RunEventType,
    RunStateEvent,
    RunStatus,
    StepEventType,
    StepStateEvent,
    StepStatus,
    StopReason,
)


_RUN_FAILURE_CASES = (
    (RunEventType.DEADLINE_EXCEEDED, StopReason.DEADLINE_EXCEEDED),
    (RunEventType.MAX_STEPS_REACHED, StopReason.MAX_STEPS_REACHED),
    (RunEventType.NO_ACTION, StopReason.NO_ACTION),
    (RunEventType.REPEATED_ACTION, StopReason.REPEATED_ACTION),
    (RunEventType.BUDGET_EXHAUSTED, StopReason.BUDGET_EXHAUSTED),
)


class AgentStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.machine = AgentStateMachine()

    def make_state(self, *, started: bool = False) -> AgentState:
        state = AgentState.for_run_context("run-1")
        if started:
            self.machine.apply_run_event(state, RunStateEvent(RunEventType.STARTED))
        return state

    @staticmethod
    def completed_event(final_output: str | None = "完成") -> RunStateEvent:
        return RunStateEvent(
            RunEventType.COMPLETED,
            stop_reason=StopReason.COMPLETED,
            final_output=final_output,
        )

    @staticmethod
    def failed_event(
        event_type: RunEventType = RunEventType.FAILED,
        reason: StopReason = StopReason.UNHANDLED_ERROR,
    ) -> RunStateEvent:
        return RunStateEvent(
            event_type,
            stop_reason=reason,
            error_code=reason.value,
            error_message="安全错误摘要",
        )

    @staticmethod
    def cancelled_event(reason: StopReason = StopReason.USER_CANCELLED) -> RunStateEvent:
        return RunStateEvent(
            RunEventType.CANCELLED,
            stop_reason=reason,
            error_code=reason.value,
            error_message="运行已取消",
        )

    def add_pending_step(self, state: AgentState, step_id: str = "step-1") -> None:
        self.machine.add_step(state, step_id=step_id, name=f"步骤 {step_id}")

    def start_step(self, state: AgentState, step_id: str = "step-1") -> None:
        self.add_pending_step(state, step_id)
        self.machine.apply_step_event(state, StepStateEvent(StepEventType.STARTED, step_id))

    def assert_rejected_without_change(
        self,
        state: AgentState,
        callback,
        exception_type: type[Exception] = InvalidStateTransitionError,
    ) -> None:
        before = state.to_dict()
        with self.assertRaises(exception_type):
            callback()
        self.assertEqual(state.to_dict(), before)

    def test_run_created_to_running(self) -> None:
        state = self.make_state()
        self.machine.apply_run_event(state, RunStateEvent(RunEventType.STARTED))
        self.assertEqual(state.status, RunStatus.RUNNING)
        self.assertIsNone(state.stop_reason)

    def test_run_running_to_succeeded(self) -> None:
        state = self.make_state(started=True)
        self.machine.apply_run_event(state, self.completed_event("回答"))
        self.assertEqual(state.status, RunStatus.SUCCEEDED)
        self.assertEqual(state.stop_reason, StopReason.COMPLETED)
        self.assertEqual(state.final_output, "回答")

    def test_run_generic_failure_and_cancellation(self) -> None:
        failed = self.make_state(started=True)
        self.machine.apply_run_event(failed, self.failed_event())
        self.assertEqual(failed.status, RunStatus.FAILED)
        self.assertEqual(failed.stop_reason, StopReason.UNHANDLED_ERROR)

        cancelled = self.make_state(started=True)
        self.machine.apply_run_event(cancelled, self.cancelled_event())
        self.assertEqual(cancelled.status, RunStatus.CANCELLED)
        self.assertEqual(cancelled.stop_reason, StopReason.USER_CANCELLED)

    def test_created_allows_generic_failure_and_cancellation(self) -> None:
        failed = self.make_state()
        self.machine.apply_run_event(failed, self.failed_event())
        self.assertEqual(failed.status, RunStatus.FAILED)

        cancelled = self.make_state()
        self.machine.apply_run_event(cancelled, self.cancelled_event())
        self.assertEqual(cancelled.status, RunStatus.CANCELLED)

    def test_special_run_failures_map_to_expected_stop_reason(self) -> None:
        for event_type, reason in _RUN_FAILURE_CASES:
            with self.subTest(event_type=event_type):
                state = self.make_state(started=True)
                self.machine.apply_run_event(state, self.failed_event(event_type, reason))
                self.assertEqual(state.status, RunStatus.FAILED)
                self.assertEqual(state.stop_reason, reason)

    def test_step_pending_to_running(self) -> None:
        state = self.make_state(started=True)
        self.add_pending_step(state)
        self.machine.apply_step_event(state, StepStateEvent(StepEventType.STARTED, "step-1"))
        self.assertEqual(state.steps["step-1"].status, StepStatus.RUNNING)
        self.assertEqual(state.active_step_ids, {"step-1"})

    def test_running_step_success_failure_and_cancellation(self) -> None:
        cases = (
            (StepEventType.SUCCEEDED, StepStatus.SUCCEEDED, None),
            (StepEventType.FAILED, StepStatus.FAILED, "STEP_FAILED"),
            (StepEventType.CANCELLED, StepStatus.CANCELLED, "STEP_CANCELLED"),
        )
        for event_type, expected_status, error_code in cases:
            with self.subTest(event_type=event_type):
                state = self.make_state(started=True)
                self.start_step(state)
                event = StepStateEvent(
                    event_type,
                    "step-1",
                    error_code=error_code,
                    error_message="安全摘要" if error_code else None,
                )
                self.machine.apply_step_event(state, event)
                self.assertEqual(state.steps["step-1"].status, expected_status)
                self.assertEqual(state.active_step_ids, set())

    def test_pending_step_cancellation_blocked_and_skipped(self) -> None:
        cases = (
            (StepEventType.CANCELLED, StepStatus.CANCELLED, "STEP_CANCELLED"),
            (StepEventType.BLOCKED, StepStatus.BLOCKED, "DEPENDENCY_FAILED"),
            (StepEventType.SKIPPED, StepStatus.SKIPPED, None),
        )
        for event_type, expected_status, error_code in cases:
            with self.subTest(event_type=event_type):
                state = self.make_state(started=True)
                self.add_pending_step(state)
                event = StepStateEvent(
                    event_type,
                    "step-1",
                    error_code=error_code,
                    error_message="安全摘要" if error_code else None,
                )
                self.machine.apply_step_event(state, event)
                step = state.steps["step-1"]
                self.assertEqual(step.status, expected_status)
                self.assertIsNone(step.started_at)
                self.assertIsNotNone(step.ended_at)

    def test_created_cannot_complete_and_running_cannot_restart(self) -> None:
        created = self.make_state()
        self.assert_rejected_without_change(
            created,
            lambda: self.machine.apply_run_event(created, self.completed_event()),
        )
        running = self.make_state(started=True)
        self.assert_rejected_without_change(
            running,
            lambda: self.machine.apply_run_event(running, RunStateEvent(RunEventType.STARTED)),
        )

    def test_pending_cannot_succeed_and_running_cannot_be_blocked(self) -> None:
        pending = self.make_state(started=True)
        self.add_pending_step(pending)
        self.assert_rejected_without_change(
            pending,
            lambda: self.machine.apply_step_event(
                pending,
                StepStateEvent(StepEventType.SUCCEEDED, "step-1"),
            ),
        )

        running = self.make_state(started=True)
        self.start_step(running)
        self.assert_rejected_without_change(
            running,
            lambda: self.machine.apply_step_event(
                running,
                StepStateEvent(StepEventType.BLOCKED, "step-1"),
            ),
        )

    def test_terminal_run_and_step_reject_late_events(self) -> None:
        state = self.make_state(started=True)
        self.machine.apply_run_event(state, self.completed_event())
        self.assert_rejected_without_change(
            state,
            lambda: self.machine.apply_run_event(state, self.failed_event()),
        )

        state = self.make_state(started=True)
        self.start_step(state)
        self.machine.apply_step_event(state, StepStateEvent(StepEventType.SUCCEEDED, "step-1"))
        self.assert_rejected_without_change(
            state,
            lambda: self.machine.apply_step_event(
                state,
                StepStateEvent(
                    StepEventType.FAILED,
                    "step-1",
                    error_code="LATE_FAILURE",
                ),
            ),
        )

    def test_terminal_run_rejects_late_pending_step_event(self) -> None:
        state = self.make_state(started=True)
        self.add_pending_step(state)
        self.machine.apply_run_event(state, self.completed_event())
        self.assert_rejected_without_change(
            state,
            lambda: self.machine.apply_step_event(
                state,
                StepStateEvent(StepEventType.SKIPPED, "step-1"),
            ),
        )

    def test_unknown_step_and_non_running_run_start_are_rejected(self) -> None:
        state = self.make_state(started=True)
        self.assert_rejected_without_change(
            state,
            lambda: self.machine.apply_step_event(
                state,
                StepStateEvent(StepEventType.STARTED, "missing"),
            ),
        )

        created = self.make_state()
        created.add_step("step-1", "步骤")
        self.assert_rejected_without_change(
            created,
            lambda: self.machine.apply_step_event(
                created,
                StepStateEvent(StepEventType.STARTED, "step-1"),
            ),
        )

    def test_running_step_missing_from_active_set_is_rejected(self) -> None:
        state = self.make_state(started=True)
        self.start_step(state)
        state.active_step_ids.clear()
        with self.assertRaises(InvalidStateTransitionError):
            self.machine.apply_step_event(
                state,
                StepStateEvent(StepEventType.SUCCEEDED, "step-1"),
            )
        self.assertEqual(state.steps["step-1"].status, StepStatus.RUNNING)

    def test_active_step_blocks_all_run_terminal_transitions(self) -> None:
        event_factories = (self.completed_event, self.failed_event, self.cancelled_event)
        for event_factory in event_factories:
            with self.subTest(event_factory=event_factory):
                state = self.make_state(started=True)
                self.start_step(state)
                event = event_factory()
                self.assert_rejected_without_change(
                    state,
                    lambda event=event, state=state: self.machine.apply_run_event(state, event),
                )

    def test_invalid_run_and_step_transitions_are_atomic(self) -> None:
        run_state = self.make_state(started=True)
        self.start_step(run_state)
        self.assert_rejected_without_change(
            run_state,
            lambda: self.machine.apply_run_event(run_state, self.completed_event()),
        )

        step_state = self.make_state(started=True)
        self.add_pending_step(step_state)
        self.assert_rejected_without_change(
            step_state,
            lambda: self.machine.apply_step_event(
                step_state,
                StepStateEvent(StepEventType.SUCCEEDED, "step-1"),
            ),
        )

    def test_event_validation_rejects_naive_datetime_and_empty_step_id(self) -> None:
        with self.assertRaises(ValueError):
            RunStateEvent(RunEventType.STARTED, occurred_at=datetime(2026, 7, 21, 12, 0))
        with self.assertRaises(ValueError):
            StepStateEvent(StepEventType.STARTED, "   ")

    def test_success_events_reject_error_information(self) -> None:
        with self.assertRaises(ValueError):
            RunStateEvent(
                RunEventType.COMPLETED,
                stop_reason=StopReason.COMPLETED,
                error_code="SHOULD_NOT_EXIST",
            )
        with self.assertRaises(ValueError):
            StepStateEvent(
                StepEventType.SUCCEEDED,
                "step-1",
                error_message="不应存在",
            )

    def test_failed_event_requires_safe_error_information(self) -> None:
        with self.assertRaises(ValueError):
            StepStateEvent(StepEventType.FAILED, "step-1")
        with self.assertRaises(ValueError):
            RunStateEvent(
                RunEventType.FAILED,
                stop_reason=StopReason.UNHANDLED_ERROR,
                error_code="",
            )
        with self.assertRaises(ValueError):
            RunStateEvent(
                RunEventType.FAILED,
                stop_reason=StopReason.UNHANDLED_ERROR,
                error_code="UNHANDLED_ERROR",
                error_message="Traceback\nsecret",
            )

    def test_cancelled_event_rejects_non_cancellation_stop_reason(self) -> None:
        with self.assertRaises(ValueError):
            self.cancelled_event(StopReason.UNHANDLED_ERROR)

    def test_special_failure_rejects_mismatched_stop_reason(self) -> None:
        with self.assertRaises(ValueError):
            self.failed_event(RunEventType.DEADLINE_EXCEEDED, StopReason.NO_ACTION)

    def test_exception_contains_only_safe_transition_context(self) -> None:
        state = self.make_state()
        with self.assertRaises(InvalidStateTransitionError) as captured:
            self.machine.apply_run_event(state, self.completed_event())
        error = captured.exception
        self.assertEqual(error.entity_type, "Run")
        self.assertEqual(error.current_status, "CREATED")
        self.assertEqual(error.event_type, "COMPLETED")
        self.assertEqual(error.entity_id, "run-1")
        self.assertNotIn("prompt", str(error).casefold())


if __name__ == "__main__":
    unittest.main()
