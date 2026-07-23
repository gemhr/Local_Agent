#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""最小有界 AgentLoop 的单元测试。"""

from __future__ import annotations

from collections.abc import Generator
import inspect
import unittest

from core.runtime import (
    ActionOutcome,
    AgentAction,
    AgentLoop,
    AgentLoopPolicy,
    AgentObservation,
    AgentState,
    AgentStateMachine,
    CancellationSource,
    LegacyAgentRouterDriver,
    RunCancelledError,
    RunDeadlineExceededError,
    RunEventType,
    RunStateEvent,
    RunStatus,
    StepStatus,
    StepEventType,
    StepStateEvent,
    StopReason,
    create_run_context,
)


class RecordingStateMachine(AgentStateMachine):
    """记录 Loop 发送的事件，同时执行真实状态转移。"""

    def __init__(self) -> None:
        self.run_events: list[RunEventType] = []
        self.step_events: list[StepEventType] = []
        self.added_steps: list[str] = []

    def add_step(self, state: AgentState, *, step_id: str, name: str) -> None:
        self.added_steps.append(step_id)
        super().add_step(state, step_id=step_id, name=name)

    def apply_run_event(self, state: AgentState, event: RunStateEvent) -> None:
        self.run_events.append(event.event_type)
        super().apply_run_event(state, event)

    def apply_step_event(self, state: AgentState, event: StepStateEvent) -> None:
        self.step_events.append(event.event_type)
        super().apply_step_event(state, event)


class ScriptedDriver:
    def __init__(self, actions: list[AgentAction | None], observations: list[AgentObservation]) -> None:
        self.actions = actions
        self.observations = observations
        self.previous: list[AgentObservation | None] = []
        self.executed: list[str] = []

    def decide(self, previous_observation: AgentObservation | None) -> AgentAction | None:
        self.previous.append(previous_observation)
        return self.actions.pop(0)

    def execute(self, action: AgentAction, run_context) -> Generator[str, None, AgentObservation]:
        self.executed.append(action.step_id)
        yield f"{action.step_id}:one"
        yield f"{action.step_id}:two"
        return self.observations.pop(0)


def action(step_id: str, dedup_key: str | None = None) -> AgentAction:
    return AgentAction(step_id, f"Action {step_id}", "test_action", dedup_key or step_id)


class AgentLoopTests(unittest.TestCase):
    def run_loop(self, driver: ScriptedDriver, policy: AgentLoopPolicy | None = None):
        context, _ = create_run_context(entry_agent_id="test")
        state = AgentState.for_run_context(context.run_id)
        return list(AgentLoop(policy).run_stream(run_context=context, agent_state=state, driver=driver)), state

    def test_policy_defaults_and_invalid_values(self) -> None:
        self.assertEqual(AgentLoopPolicy(), AgentLoopPolicy(8, 1, 2))
        for invalid in (0, -1, True, 1.5, "1"):
            for field_name in (
                "max_steps",
                "max_consecutive_no_action",
                "max_consecutive_same_action",
            ):
                with self.assertRaises(ValueError):
                    AgentLoopPolicy(**{field_name: invalid})  # type: ignore[arg-type]

    def test_completed_action_preserves_order_and_state(self) -> None:
        driver = ScriptedDriver([action("one")], [AgentObservation(ActionOutcome.COMPLETED, final_output="done")])
        output, state = self.run_loop(driver)
        self.assertEqual(output, ["one:one", "one:two"])
        self.assertEqual(state.status, RunStatus.SUCCEEDED)
        self.assertEqual(state.stop_reason, StopReason.COMPLETED)
        self.assertEqual(state.steps["one"].status, StepStatus.SUCCEEDED)
        self.assertEqual(state.active_step_ids, set())

    def test_continue_passes_observation_to_next_decision(self) -> None:
        continued = AgentObservation(ActionOutcome.CONTINUE)
        driver = ScriptedDriver([action("one"), action("two")], [continued, AgentObservation(ActionOutcome.COMPLETED)])
        _, state = self.run_loop(driver)
        self.assertEqual(driver.previous, [None, continued])
        self.assertEqual([state.steps[key].status for key in ("one", "two")], [StepStatus.SUCCEEDED, StepStatus.SUCCEEDED])
        self.assertEqual(state.status, RunStatus.SUCCEEDED)

    def test_observation_should_continue_is_derived_and_read_only(self) -> None:
        continued = AgentObservation(ActionOutcome.CONTINUE)
        completed = AgentObservation(ActionOutcome.COMPLETED)
        failed = AgentObservation(ActionOutcome.FAILED, error_code="SAFE_ERROR")
        self.assertTrue(continued.should_continue)
        self.assertFalse(completed.should_continue)
        self.assertFalse(failed.should_continue)
        with self.assertRaises(TypeError):
            AgentObservation(ActionOutcome.COMPLETED, should_continue=True)  # type: ignore[call-arg]

    def test_max_steps_stops_before_next_action(self) -> None:
        driver = ScriptedDriver([action("one"), action("two")], [AgentObservation(ActionOutcome.CONTINUE)])
        _, state = self.run_loop(driver, AgentLoopPolicy(max_steps=1, max_consecutive_no_action=1, max_consecutive_same_action=2))
        self.assertEqual(driver.executed, ["one"])
        self.assertNotIn("two", state.steps)
        self.assertEqual(state.stop_reason, StopReason.MAX_STEPS_REACHED)
        self.assertEqual(state.active_step_ids, set())

    def test_no_action_has_no_step(self) -> None:
        driver = ScriptedDriver([None], [])
        _, state = self.run_loop(driver)
        self.assertEqual(state.stop_reason, StopReason.NO_ACTION)
        self.assertEqual(state.steps, {})
        self.assertEqual(len(driver.previous), 1)

    def test_repeated_action_stops_before_excess_action_and_resets_for_other_key(self) -> None:
        driver = ScriptedDriver(
            [action("one", "same"), action("two", "other"), action("three", "same"), action("four", "same"), action("five", "same")],
            [AgentObservation(ActionOutcome.CONTINUE)] * 4,
        )
        _, state = self.run_loop(driver, AgentLoopPolicy(max_steps=8, max_consecutive_no_action=1, max_consecutive_same_action=2))
        self.assertEqual(driver.executed, ["one", "two", "three", "four"])
        self.assertNotIn("five", state.steps)
        self.assertEqual(state.stop_reason, StopReason.REPEATED_ACTION)

    def test_deadline_cancellation_unknown_error_and_generator_close(self) -> None:
        class DeadlineDriver(ScriptedDriver):
            def execute(self, selected, context):
                raise RunDeadlineExceededError("deadline")
                yield "unreachable"

        context, _ = create_run_context(entry_agent_id="test")
        state = AgentState.for_run_context(context.run_id)
        with self.assertRaises(RunDeadlineExceededError):
            list(AgentLoop().run_stream(run_context=context, agent_state=state, driver=DeadlineDriver([action("one")], [])))
        self.assertEqual(state.stop_reason, StopReason.DEADLINE_EXCEEDED)
        self.assertEqual(state.steps["one"].status, StepStatus.FAILED)

        source = CancellationSource()
        source.cancel()
        context, _ = create_run_context(entry_agent_id="test", cancellation_source=source)
        state = AgentState.for_run_context(context.run_id)
        with self.assertRaises(RunCancelledError):
            list(AgentLoop().run_stream(run_context=context, agent_state=state, driver=ScriptedDriver([], [])))
        self.assertEqual(state.status, RunStatus.CANCELLED)

        class BrokenDriver(ScriptedDriver):
            def execute(self, selected, context):
                raise RuntimeError("secret path /private")
                yield "unreachable"

        context, _ = create_run_context(entry_agent_id="test")
        state = AgentState.for_run_context(context.run_id)
        with self.assertLogs("core.runtime.agent_loop", level="ERROR"):
            with self.assertRaises(RuntimeError):
                list(AgentLoop().run_stream(run_context=context, agent_state=state, driver=BrokenDriver([action("one")], [])))
        self.assertEqual(state.stop_reason, StopReason.UNHANDLED_ERROR)
        self.assertNotIn("secret", repr(state.to_dict()))

        driver = ScriptedDriver([action("one")], [AgentObservation(ActionOutcome.COMPLETED)])
        context, _ = create_run_context(entry_agent_id="test")
        state = AgentState.for_run_context(context.run_id)
        stream = AgentLoop().run_stream(run_context=context, agent_state=state, driver=driver)
        self.assertEqual(next(stream), "one:one")
        stream.close()
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual(state.stop_reason, StopReason.CLIENT_DISCONNECTED)
        self.assertEqual(state.steps["one"].status, StepStatus.CANCELLED)

    def test_legacy_driver_calls_router_once_and_keeps_chunks_and_context(self) -> None:
        class Router:
            def __init__(self) -> None:
                self.calls = 0
                self.context = None

            def chat_stream(self, user_query, agent_id="core_router", run_context=None):
                self.calls += 1
                self.context = run_context
                yield "[[ORCH]]status"
                yield "plain"
                yield " answer"

        router = Router()
        context, _ = create_run_context(entry_agent_id="test")
        state = AgentState.for_run_context(context.run_id)
        output = list(AgentLoop().run_stream(run_context=context, agent_state=state, driver=LegacyAgentRouterDriver(router, user_query="q", agent_id="test")))
        self.assertEqual(output, ["[[ORCH]]status", "plain", " answer"])
        self.assertEqual(router.calls, 1)
        self.assertIs(router.context, context)
        self.assertEqual(state.steps["legacy-agent-router"].status, StepStatus.SUCCEEDED)
        self.assertEqual(state.status, RunStatus.SUCCEEDED)
        self.assertEqual(state.final_output, "plain answer")
        self.assertNotIn("[[ORCH]]", state.final_output or "")

    def test_partial_stream_is_not_a_success_after_unknown_exception(self) -> None:
        class PartialThenBrokenDriver:
            def decide(self, previous_observation):
                return action("partial")

            def execute(self, selected, context):
                yield "partial text"
                raise RuntimeError("later failure")

        context, _ = create_run_context(entry_agent_id="test")
        state = AgentState.for_run_context(context.run_id)
        stream = AgentLoop().run_stream(
            run_context=context,
            agent_state=state,
            driver=PartialThenBrokenDriver(),
        )
        self.assertEqual(next(stream), "partial text")
        with self.assertLogs("core.runtime.agent_loop", level="ERROR"):
            with self.assertRaises(RuntimeError):
                next(stream)
        self.assertEqual(state.status, RunStatus.FAILED)
        self.assertNotEqual(state.status, RunStatus.SUCCEEDED)

    def test_normal_event_sequence(self) -> None:
        driver = ScriptedDriver(
            [action("one")],
            [AgentObservation(ActionOutcome.COMPLETED, final_output="完成")],
        )
        context, _ = create_run_context(entry_agent_id="test")
        state = AgentState.for_run_context(context.run_id)
        machine = RecordingStateMachine()
        list(
            AgentLoop(state_machine=machine).run_stream(
                run_context=context,
                agent_state=state,
                driver=driver,
            )
        )
        self.assertEqual(machine.added_steps, ["one"])
        self.assertEqual(machine.run_events, [RunEventType.STARTED, RunEventType.COMPLETED])
        self.assertEqual(machine.step_events, [StepEventType.STARTED, StepEventType.SUCCEEDED])

    def test_deadline_and_cancellation_event_sequences(self) -> None:
        class DeadlineDriver(ScriptedDriver):
            def execute(self, selected, context):
                raise RunDeadlineExceededError("deadline")
                yield "unreachable"

        context, _ = create_run_context(entry_agent_id="test")
        state = AgentState.for_run_context(context.run_id)
        machine = RecordingStateMachine()
        with self.assertRaises(RunDeadlineExceededError):
            list(
                AgentLoop(state_machine=machine).run_stream(
                    run_context=context,
                    agent_state=state,
                    driver=DeadlineDriver([action("one")], []),
                )
            )
        self.assertEqual(machine.run_events, [RunEventType.STARTED, RunEventType.DEADLINE_EXCEEDED])
        self.assertEqual(machine.step_events, [StepEventType.STARTED, StepEventType.FAILED])

        class CancelledDriver(ScriptedDriver):
            def execute(self, selected, context):
                raise RunCancelledError("cancelled")
                yield "unreachable"

        context, _ = create_run_context(entry_agent_id="test")
        state = AgentState.for_run_context(context.run_id)
        machine = RecordingStateMachine()
        with self.assertRaises(RunCancelledError):
            list(
                AgentLoop(state_machine=machine).run_stream(
                    run_context=context,
                    agent_state=state,
                    driver=CancelledDriver([action("one")], []),
                )
            )
        self.assertEqual(machine.run_events, [RunEventType.STARTED, RunEventType.CANCELLED])
        self.assertEqual(machine.step_events, [StepEventType.STARTED, StepEventType.CANCELLED])

    def test_policy_stop_events_and_unknown_exception_sequence(self) -> None:
        cases = (
            (
                ScriptedDriver([action("one"), action("two")], [AgentObservation(ActionOutcome.CONTINUE)]),
                AgentLoopPolicy(max_steps=1),
                RunEventType.MAX_STEPS_REACHED,
            ),
            (ScriptedDriver([None], []), None, RunEventType.NO_ACTION),
            (
                ScriptedDriver(
                    [action("one", "same"), action("two", "same")],
                    [AgentObservation(ActionOutcome.CONTINUE)],
                ),
                AgentLoopPolicy(max_consecutive_same_action=1),
                RunEventType.REPEATED_ACTION,
            ),
        )
        for driver, policy, expected_event in cases:
            with self.subTest(expected_event=expected_event):
                context, _ = create_run_context(entry_agent_id="test")
                state = AgentState.for_run_context(context.run_id)
                machine = RecordingStateMachine()
                list(
                    AgentLoop(policy, machine).run_stream(
                        run_context=context,
                        agent_state=state,
                        driver=driver,
                    )
                )
                self.assertEqual(machine.run_events[-1], expected_event)

        class BrokenDriver(ScriptedDriver):
            def execute(self, selected, context):
                raise RuntimeError("secret")
                yield "unreachable"

        context, _ = create_run_context(entry_agent_id="test")
        state = AgentState.for_run_context(context.run_id)
        machine = RecordingStateMachine()
        with self.assertLogs("core.runtime.agent_loop", level="ERROR"):
            with self.assertRaises(RuntimeError):
                list(
                    AgentLoop(state_machine=machine).run_stream(
                        run_context=context,
                        agent_state=state,
                        driver=BrokenDriver([action("one")], []),
                    )
                )
        self.assertEqual(machine.run_events, [RunEventType.STARTED, RunEventType.FAILED])
        self.assertEqual(machine.step_events, [StepEventType.STARTED, StepEventType.FAILED])

    def test_agent_loop_does_not_call_agent_state_lifecycle_mutations_directly(self) -> None:
        source = inspect.getsource(AgentLoop.run_stream)
        for method_name in (
            "mark_running",
            "start_step",
            "succeed_step",
            "fail_step",
            "cancel_step",
            "mark_succeeded",
            "mark_failed",
            "mark_cancelled",
        ):
            self.assertNotIn(f"agent_state.{method_name}(", source)
        self.assertNotIn("agent_state.add_step(", source)


if __name__ == "__main__":
    unittest.main()
