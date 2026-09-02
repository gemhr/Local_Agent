#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WAITING_FOR_APPROVAL 状态与状态机转移测试（Stage5-Phase7-WP1）。

覆盖冻结 transition：
- RUNNING --APPROVAL_REQUESTED--> WAITING_FOR_APPROVAL
- WAITING_FOR_APPROVAL --APPROVAL_APPROVED--> RUNNING
- WAITING_FOR_APPROVAL --APPROVAL_REJECTED--> FAILED (TOOL_APPROVAL_REJECTED)
- WAITING_FOR_APPROVAL --CANCELLED--> CANCELLED

并验证：WAITING 保持 active、started_at 保留、ended_at 缺失、
active_step_ids 包含该 step、非法转移拒绝、serialization/validation 正确。
"""

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
from core.runtime.state import AgentStateValidationError


class WaitingForApprovalStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.machine = AgentStateMachine()

    def make_running_run(self) -> AgentState:
        state = AgentState.for_run_context("run-approval")
        self.machine.apply_run_event(state, RunStateEvent(RunEventType.STARTED))
        return state

    def start_step(self, state: AgentState, step_id: str = "step-1") -> None:
        self.machine.add_step(state, step_id=step_id, name=f"步骤 {step_id}")
        self.machine.apply_step_event(
            state, StepStateEvent(StepEventType.STARTED, step_id)
        )

    def test_running_to_waiting_preserves_active_step(self) -> None:
        state = self.make_running_run()
        self.start_step(state)
        step = state.steps["step-1"]
        started_at = step.started_at
        self.machine.apply_step_event(
            state, StepStateEvent(StepEventType.APPROVAL_REQUESTED, "step-1")
        )
        step = state.steps["step-1"]
        self.assertEqual(step.status, StepStatus.WAITING_FOR_APPROVAL)
        # active、already-started：started_at 保留、ended_at 缺失。
        self.assertEqual(step.started_at, started_at)
        self.assertIsNone(step.ended_at)
        self.assertIn("step-1", state.active_step_ids)
        self.assertEqual(step.error_code, None)
        # Run 仍在 RUNNING（不引入 paused/waiting/pending_approval）。
        self.assertEqual(state.status, RunStatus.RUNNING)
        state.validate()

    def test_waiting_to_running_on_approval_approved(self) -> None:
        state = self.make_running_run()
        self.start_step(state)
        self.machine.apply_step_event(
            state, StepStateEvent(StepEventType.APPROVAL_REQUESTED, "step-1")
        )
        self.machine.apply_step_event(
            state, StepStateEvent(StepEventType.APPROVAL_APPROVED, "step-1")
        )
        step = state.steps["step-1"]
        self.assertEqual(step.status, StepStatus.RUNNING)
        self.assertIsNotNone(step.started_at)
        self.assertIsNone(step.ended_at)
        self.assertIn("step-1", state.active_step_ids)
        state.validate()

    def test_waiting_to_failed_on_approval_rejected(self) -> None:
        state = self.make_running_run()
        self.start_step(state)
        self.machine.apply_step_event(
            state, StepStateEvent(StepEventType.APPROVAL_REQUESTED, "step-1")
        )
        self.machine.apply_step_event(
            state,
            StepStateEvent(
                StepEventType.APPROVAL_REJECTED,
                "step-1",
                error_code="TOOL_APPROVAL_REJECTED",
                error_message="Tool 调用已被拒绝审批",
            ),
        )
        step = state.steps["step-1"]
        self.assertEqual(step.status, StepStatus.FAILED)
        self.assertEqual(step.error_code, "TOOL_APPROVAL_REJECTED")
        self.assertIsNotNone(step.ended_at)
        self.assertNotIn("step-1", state.active_step_ids)
        state.validate()

    def test_waiting_to_cancelled(self) -> None:
        state = self.make_running_run()
        self.start_step(state)
        self.machine.apply_step_event(
            state, StepStateEvent(StepEventType.APPROVAL_REQUESTED, "step-1")
        )
        self.machine.apply_step_event(
            state,
            StepStateEvent(
                StepEventType.CANCELLED,
                "step-1",
                error_code="RUN_CANCELLED",
                error_message="运行取消使审批失效",
            ),
        )
        step = state.steps["step-1"]
        self.assertEqual(step.status, StepStatus.CANCELLED)
        self.assertNotIn("step-1", state.active_step_ids)
        state.validate()

    def test_invalid_transition_from_waiting_rejected(self) -> None:
        state = self.make_running_run()
        self.start_step(state)
        self.machine.apply_step_event(
            state, StepStateEvent(StepEventType.APPROVAL_REQUESTED, "step-1")
        )
        # WAITING 不允许直接 SUCCEEDED / BLOCKED / SKIPPED。
        for event_type in (
            StepEventType.SUCCEEDED,
            StepEventType.BLOCKED,
            StepEventType.SKIPPED,
            StepEventType.STARTED,
        ):
            with self.subTest(event_type=event_type), self.assertRaises(
                InvalidStateTransitionError
            ):
                self.machine.apply_step_event(
                    state, StepStateEvent(event_type, "step-1")
                )

    def test_approval_requested_requires_running_source(self) -> None:
        state = self.make_running_run()
        self.start_step(state)
        self.machine.apply_step_event(
            state, StepStateEvent(StepEventType.APPROVAL_REQUESTED, "step-1")
        )
        # 已 WAITING 时再次 APPROVAL_REQUESTED 无效。
        with self.assertRaises(InvalidStateTransitionError):
            self.machine.apply_step_event(
                state, StepStateEvent(StepEventType.APPROVAL_REQUESTED, "step-1")
            )

    def test_waiting_step_prevents_run_terminal(self) -> None:
        state = self.make_running_run()
        self.start_step(state)
        self.machine.apply_step_event(
            state, StepStateEvent(StepEventType.APPROVAL_REQUESTED, "step-1")
        )
        with self.assertRaises(InvalidStateTransitionError):
            self.machine.apply_run_event(
                state,
                RunStateEvent(
                    RunEventType.COMPLETED,
                    stop_reason=StopReason.COMPLETED,
                    final_output="done",
                ),
            )

    def test_serialization_roundtrip_of_waiting(self) -> None:
        state = self.make_running_run()
        self.start_step(state)
        self.machine.apply_step_event(
            state, StepStateEvent(StepEventType.APPROVAL_REQUESTED, "step-1")
        )
        payload = state.to_dict()
        restored = AgentState.from_dict(payload)
        step = restored.steps["step-1"]
        self.assertEqual(step.status, StepStatus.WAITING_FOR_APPROVAL)
        self.assertIsNotNone(step.started_at)
        self.assertIsNone(step.ended_at)
        self.assertIn("step-1", restored.active_step_ids)
        restored.validate()

    def test_waiting_status_serialization_rejects_missing_started(self) -> None:
        # WAITING 必须 started_at；构造 payload 时去掉 started_at 应校验失败。
        payload = {
            "schema_version": 1,
            "run_id": "run-x",
            "status": "RUNNING",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "steps": [
                {
                    "step_id": "step-1",
                    "name": "step",
                    "status": "WAITING_FOR_APPROVAL",
                    "created_at": datetime.now().isoformat(),
                    "started_at": None,
                    "ended_at": None,
                    "error_code": None,
                    "error_message": None,
                }
            ],
            "active_step_ids": ["step-1"],
            "stop_reason": None,
            "final_output": None,
            "error_code": None,
            "error_message": None,
        }
        with self.assertRaises(AgentStateValidationError):
            AgentState.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
