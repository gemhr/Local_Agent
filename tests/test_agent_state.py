#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AgentState 与 ChatService 状态集成的单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import unittest

from core.runtime import (
    AGENT_STATE_SCHEMA_VERSION,
    AgentState,
    AgentStateValidationError,
    RunStatus,
    StepState,
    StepStatus,
    StopReason,
    UnsupportedStateVersionError,
)


class AgentStateTests(unittest.TestCase):
    def make_state(self) -> AgentState:
        return AgentState.for_run_context("run-1")

    def test_initial_state_and_schema_version(self) -> None:
        state = self.make_state()
        self.assertEqual(state.status, RunStatus.CREATED)
        self.assertEqual(state.schema_version, AGENT_STATE_SCHEMA_VERSION)
        state.validate()

    def test_created_to_running_add_and_start_step(self) -> None:
        state = self.make_state()
        state.add_step("step-1", "First")
        state.mark_running()
        state.start_step("step-1")
        self.assertEqual(state.status, RunStatus.RUNNING)
        self.assertEqual(state.steps["step-1"].status, StepStatus.RUNNING)
        self.assertEqual(state.active_step_ids, {"step-1"})

    def test_step_success_failure_and_cancellation(self) -> None:
        state = self.make_state()
        state.mark_running()
        state.add_step("ok", "OK")
        state.start_step("ok")
        state.succeed_step("ok")
        self.assertEqual(state.steps["ok"].status, StepStatus.SUCCEEDED)
        state.add_step("bad", "Bad")
        state.start_step("bad")
        state.fail_step("bad", error_code="ERR", error_message="safe")
        self.assertEqual(state.steps["bad"].status, StepStatus.FAILED)
        state.add_step("cancel", "Cancel")
        state.start_step("cancel")
        state.cancel_step("cancel", error_message="stop")
        self.assertEqual(state.steps["cancel"].status, StepStatus.CANCELLED)

    def test_run_success_failure_and_cancellation(self) -> None:
        state = self.make_state()
        state.mark_running()
        state.mark_succeeded(final_output="done")
        self.assertEqual(state.status, RunStatus.SUCCEEDED)
        self.assertEqual(state.stop_reason, StopReason.COMPLETED)

        failed = self.make_state()
        failed.mark_running()
        failed.mark_failed(stop_reason=StopReason.UNHANDLED_ERROR, error_code="ERR", error_message="safe")
        self.assertEqual(failed.status, RunStatus.FAILED)

        cancelled = self.make_state()
        cancelled.mark_running()
        cancelled.mark_cancelled(stop_reason=StopReason.USER_CANCELLED)
        self.assertEqual(cancelled.status, RunStatus.CANCELLED)

    def test_run_stop_reason_invariants(self) -> None:
        with self.assertRaises(AgentStateValidationError):
            AgentState(run_id="run", status=RunStatus.CREATED, stop_reason=StopReason.COMPLETED)
        with self.assertRaises(AgentStateValidationError):
            AgentState(run_id="run", status=RunStatus.SUCCEEDED)
        with self.assertRaises(AgentStateValidationError):
            AgentState(run_id="run", status=RunStatus.SUCCEEDED, stop_reason=StopReason.UNHANDLED_ERROR)
        with self.assertRaises(AgentStateValidationError):
            AgentState(run_id="run", status=RunStatus.CANCELLED, stop_reason=StopReason.UNHANDLED_ERROR)
        with self.assertRaises(AgentStateValidationError):
            AgentState(run_id="run", status=RunStatus.FAILED, stop_reason=StopReason.COMPLETED)
        with self.assertRaises(AgentStateValidationError):
            AgentState(run_id="run", status=RunStatus.FAILED, stop_reason=StopReason.USER_CANCELLED)

    def test_terminal_run_cannot_keep_active_steps(self) -> None:
        step = StepState(
            step_id="s",
            name="S",
            status=StepStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        with self.assertRaises(AgentStateValidationError):
            AgentState(
                run_id="run",
                status=RunStatus.SUCCEEDED,
                steps={"s": step},
                active_step_ids={"s"},
                stop_reason=StopReason.COMPLETED,
            )

    def test_duplicate_empty_and_unknown_step_ids_are_rejected(self) -> None:
        state = self.make_state()
        state.add_step("s", "S")
        with self.assertRaises(AgentStateValidationError):
            state.add_step("s", "Duplicate")
        with self.assertRaises(AgentStateValidationError):
            StepState(step_id="", name="S").validate()
        with self.assertRaises(AgentStateValidationError):
            StepState(step_id="s", name="").validate()
        with self.assertRaises(AgentStateValidationError):
            state.start_step("missing")
        with self.assertRaises(AgentStateValidationError):
            AgentState(run_id="")

    def test_active_step_invariants_and_multiple_running_steps(self) -> None:
        now = datetime.now(UTC)
        with self.assertRaises(AgentStateValidationError):
            AgentState(run_id="run", steps={}, active_step_ids={"missing"})
        with self.assertRaises(AgentStateValidationError):
            AgentState(
                run_id="run",
                steps={"s": StepState(step_id="s", name="S")},
                active_step_ids={"s"},
            )
        with self.assertRaises(AgentStateValidationError):
            AgentState(
                run_id="run",
                steps={"s": StepState(step_id="s", name="S", status=StepStatus.RUNNING, started_at=now)},
                active_step_ids=set(),
            )
        state = self.make_state()
        state.mark_running()
        state.add_step("a", "A")
        state.add_step("b", "B")
        state.start_step("a")
        state.start_step("b")
        self.assertEqual(state.active_step_ids, {"a", "b"})
        state.validate()

    def test_step_time_and_error_invariants(self) -> None:
        now = datetime.now(UTC)
        with self.assertRaises(AgentStateValidationError):
            StepState(step_id="s", name="S", started_at=now).validate()
        with self.assertRaises(AgentStateValidationError):
            StepState(step_id="s", name="S", status=StepStatus.RUNNING).validate()
        with self.assertRaises(AgentStateValidationError):
            StepState(step_id="s", name="S", status=StepStatus.SUCCEEDED).validate()
        with self.assertRaises(AgentStateValidationError):
            StepState(step_id="s", name="S", status=StepStatus.SUCCEEDED, ended_at=now, error_message="bad").validate()
        with self.assertRaises(AgentStateValidationError):
            StepState(step_id="s", name="S", status=StepStatus.FAILED, ended_at=now).validate()
        with self.assertRaises(AgentStateValidationError):
            StepState(
                step_id="s",
                name="S",
                status=StepStatus.SUCCEEDED,
                created_at=now,
                started_at=now + timedelta(seconds=2),
                ended_at=now + timedelta(seconds=1),
            ).validate()

    def test_blocked_step_semantics_and_active_invariants(self) -> None:
        state = self.make_state()
        state.add_step("blocked", "Blocked")
        state.block_step("blocked", error_code="DEPENDENCY_FAILED", error_message="Prerequisite failed")
        step = state.steps["blocked"]
        self.assertEqual(step.status, StepStatus.BLOCKED)
        self.assertIsNone(step.started_at)
        self.assertIsNotNone(step.ended_at)
        self.assertNotIn("blocked", state.active_step_ids)
        state.validate()

        now = datetime.now(UTC)
        with self.assertRaises(AgentStateValidationError):
            StepState(
                step_id="blocked",
                name="Blocked",
                status=StepStatus.BLOCKED,
                started_at=now,
                ended_at=now,
            ).validate()
        with self.assertRaises(AgentStateValidationError):
            AgentState(
                run_id="run",
                steps={
                    "blocked": StepState(
                        step_id="blocked",
                        name="Blocked",
                        status=StepStatus.BLOCKED,
                        ended_at=now,
                    )
                },
                active_step_ids={"blocked"},
            )

    def test_utc_datetime_invariants(self) -> None:
        naive = datetime(2026, 7, 17, 12, 0)
        with self.assertRaises(AgentStateValidationError):
            AgentState(run_id="run", created_at=naive)
        with self.assertRaises(AgentStateValidationError):
            StepState(step_id="s", name="S", created_at=naive).validate()
        with self.assertRaises(AgentStateValidationError):
            AgentState(
                run_id="run",
                created_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
                updated_at=datetime(2026, 7, 17, 11, 59, tzinfo=UTC),
            )

    def test_serialization_round_trip_and_stable_order(self) -> None:
        state = self.make_state()
        state.mark_running()
        state.add_step("b", "B")
        state.add_step("a", "A")
        state.start_step("b")
        state.start_step("a")
        payload = state.to_dict()
        json.dumps(payload)
        self.assertEqual(payload["status"], "RUNNING")
        self.assertIn("+00:00", str(payload["created_at"]))
        self.assertEqual(payload["active_step_ids"], ["a", "b"])
        self.assertEqual([step["step_id"] for step in payload["steps"]], ["a", "b"])
        text = repr(payload).lower()
        for forbidden in ("clock", "token", "event", "lock", "traceback"):
            self.assertNotIn(forbidden, text)
        restored = AgentState.from_dict(payload)
        self.assertEqual(restored.to_dict(), payload)

    def test_deserialization_rejects_bad_version_enum_and_illegal_state(self) -> None:
        payload = self.make_state().to_dict()
        payload["schema_version"] = 999
        with self.assertRaises(UnsupportedStateVersionError):
            AgentState.from_dict(payload)
        payload = self.make_state().to_dict()
        del payload["schema_version"]
        with self.assertRaises(UnsupportedStateVersionError):
            AgentState.from_dict(payload)
        payload = self.make_state().to_dict()
        payload["schema_version"] = True
        with self.assertRaises(UnsupportedStateVersionError):
            AgentState.from_dict(payload)
        payload = self.make_state().to_dict()
        payload["schema_version"] = "1"
        with self.assertRaises(UnsupportedStateVersionError):
            AgentState.from_dict(payload)
        payload = self.make_state().to_dict()
        payload["status"] = "BOGUS"
        with self.assertRaises(ValueError):
            AgentState.from_dict(payload)
        payload = self.make_state().to_dict()
        payload["stop_reason"] = "COMPLETED"
        with self.assertRaises(AgentStateValidationError):
            AgentState.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
