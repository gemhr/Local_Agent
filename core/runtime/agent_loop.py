#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""最小同步智能体循环与旧版 AgentRouter 兼容驱动器。"""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass
from enum import Enum
import logging
import re
from typing import Protocol

from core.runtime.cancellation import RunCancelledError
from core.runtime.budget import BudgetExceededError
from core.runtime.context import RunContext, RunDeadlineExceededError
from core.runtime.state import AgentState, StopReason
from core.runtime.state_machine import (
    AgentStateMachine,
    RunEventType,
    RunStateEvent,
    StepEventType,
    StepStateEvent,
)


logger = logging.getLogger(__name__)

LEGACY_AGENT_ROUTER_STEP_ID = "legacy-agent-router"
LEGACY_AGENT_ROUTER_STEP_NAME = "Legacy AgentRouter execution"
ORCHESTRATION_EVENT_PREFIX = "[[ORCH]]"
_SAFE_DEDUP_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ActionOutcome(str, Enum):
    """单个已接受动作报告的安全终态结果。"""

    CONTINUE = "CONTINUE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class AgentAction:
    """由 AgentLoopDriver 选择的非敏感动作。"""

    step_id: str
    name: str
    action_type: str
    dedup_key: str

    def __post_init__(self) -> None:
        """拒绝空字段，并要求使用不含路径的不可读去重键。"""
        for field_name, value in (
            ("step_id", self.step_id),
            ("name", self.name),
            ("action_type", self.action_type),
            ("dedup_key", self.dedup_key),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if not _SAFE_DEDUP_KEY.fullmatch(self.dedup_key):
            raise ValueError("dedup_key must be an opaque identifier without paths or free-form content")


@dataclass(frozen=True)
class AgentObservation:
    """动作的安全结果，不包含异常对象或回溯文本。"""

    outcome: ActionOutcome
    final_output: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        """校验观察结果字段，不接受无类型或为空的错误元数据。"""
        if not isinstance(self.outcome, ActionOutcome):
            raise ValueError("outcome must be an ActionOutcome")
        for field_name, value in (
            ("final_output", self.final_output),
            ("error_code", self.error_code),
            ("error_message", self.error_message),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field_name} must be a non-empty string when provided")
        if self.outcome == ActionOutcome.FAILED and not (self.error_code or self.error_message):
            raise ValueError("failed observations must include a safe error summary")

    @property
    def should_continue(self) -> bool:
        """返回循环是否应请求下一次决策。"""
        return self.outcome == ActionOutcome.CONTINUE


@dataclass(frozen=True)
class AgentLoopPolicy:
    """由 AgentLoop 实例显式持有的有界循环限制。"""

    max_steps: int = 8
    max_consecutive_no_action: int = 1
    max_consecutive_same_action: int = 2

    def __post_init__(self) -> None:
        """要求正整数，并拒绝作为 int 子类的 bool。"""
        for field_name, value in (
            ("max_steps", self.max_steps),
            ("max_consecutive_no_action", self.max_consecutive_no_action),
            ("max_consecutive_same_action", self.max_consecutive_same_action),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


class AgentLoopDriver(Protocol):
    """同步循环的最小决策与执行边界。"""

    def decide(self, previous_observation: AgentObservation | None) -> AgentAction | None:
        """选择下一个动作，但不执行它。"""

    def execute(self, action: AgentAction, run_context: RunContext) -> Generator[str, None, AgentObservation]:
        """执行已接受动作并返回其安全观察结果。"""


class LegacyAgentRouter(Protocol):
    """兼容驱动器所需的旧版 Router 结构化依赖。"""

    def chat_stream(
        self, user_query: str, agent_id: str = "core_router", run_context: RunContext | None = None
    ) -> Generator[str, None, None]:
        """产出现有文本和编排数据块。"""


class LegacyAgentRouterDriver:
    """将未改变的 AgentRouter 流呈现为一次已完成的循环动作。"""

    def __init__(self, router: LegacyAgentRouter, *, user_query: str, agent_id: str) -> None:
        self._router = router
        self._user_query = user_query
        self._agent_id = agent_id
        self._decided = False

    def decide(self, previous_observation: AgentObservation | None) -> AgentAction | None:
        """恰好返回一个旧版动作；其完成观察结果将结束本次运行。"""
        if self._decided:
            return None
        self._decided = True
        return AgentAction(
            step_id=LEGACY_AGENT_ROUTER_STEP_ID,
            name=LEGACY_AGENT_ROUTER_STEP_NAME,
            action_type="execute_legacy_agent_router",
            dedup_key=LEGACY_AGENT_ROUTER_STEP_ID,
        )

    def execute(self, action: AgentAction, run_context: RunContext) -> Generator[str, None, AgentObservation]:
        """原样转发数据块，并一次性汇总最终文本输出。"""
        final_output_chunks: list[str] = []
        for chunk in self._router.chat_stream(
            user_query=self._user_query, agent_id=self._agent_id, run_context=run_context
        ):
            if not chunk.startswith(ORCHESTRATION_EVENT_PREFIX):
                final_output_chunks.append(chunk)
            yield chunk
        return AgentObservation(
            outcome=ActionOutcome.COMPLETED,
            final_output="".join(final_output_chunks) if final_output_chunks else None,
        )


class AgentLoop:
    """通过有界驱动循环管理一次 RunContext/AgentState 执行生命周期。"""

    def __init__(
        self,
        policy: AgentLoopPolicy | None = None,
        state_machine: AgentStateMachine | None = None,
    ) -> None:
        self._policy = policy or AgentLoopPolicy()
        self._state_machine = state_machine or AgentStateMachine()

    def run_stream(
        self,
        *,
        run_context: RunContext,
        agent_state: AgentState,
        driver: AgentLoopDriver,
        state_observer: Callable[[AgentState], None] | None = None,
    ) -> Generator[str, None, None]:
        """运行驱动器，同时应用状态更新和有界终止策略。"""
        agent_state.assert_matches_run_context(run_context.run_id)
        self._state_machine.apply_run_event(
            agent_state,
            RunStateEvent(RunEventType.STARTED),
        )
        self._observe(agent_state, state_observer)
        steps_taken = 0
        consecutive_no_action = 0
        previous_dedup_key: str | None = None
        consecutive_same_action = 0
        previous_observation: AgentObservation | None = None
        active_step_id: str | None = None
        try:
            while True:
                run_context.raise_if_inactive()
                if steps_taken >= self._policy.max_steps:
                    self._apply_failed_run_event(
                        agent_state,
                        RunEventType.MAX_STEPS_REACHED,
                        StopReason.MAX_STEPS_REACHED,
                        "MAX_STEPS_REACHED",
                        "Maximum steps reached",
                    )
                    self._observe(agent_state, state_observer)
                    return
                action = driver.decide(previous_observation)
                run_context.raise_if_inactive()
                if action is None:
                    consecutive_no_action += 1
                    if consecutive_no_action >= self._policy.max_consecutive_no_action:
                        self._apply_failed_run_event(
                            agent_state,
                            RunEventType.NO_ACTION,
                            StopReason.NO_ACTION,
                            "NO_ACTION",
                            "No action available",
                        )
                        self._observe(agent_state, state_observer)
                        return
                    continue
                consecutive_no_action = 0
                consecutive_same_action = (
                    consecutive_same_action + 1 if action.dedup_key == previous_dedup_key else 1
                )
                previous_dedup_key = action.dedup_key
                if consecutive_same_action > self._policy.max_consecutive_same_action:
                    self._apply_failed_run_event(
                        agent_state,
                        RunEventType.REPEATED_ACTION,
                        StopReason.REPEATED_ACTION,
                        "REPEATED_ACTION",
                        "Repeated action limit reached",
                    )
                    self._observe(agent_state, state_observer)
                    return
                steps_taken += 1
                self._state_machine.add_step(
                    agent_state,
                    step_id=action.step_id,
                    name=action.name,
                )
                self._state_machine.apply_step_event(
                    agent_state,
                    StepStateEvent(StepEventType.STARTED, action.step_id),
                )
                active_step_id = action.step_id
                execution = driver.execute(action, run_context)
                while True:
                    run_context.raise_if_inactive()
                    try:
                        chunk = next(execution)
                    except StopIteration as completed:
                        observation = completed.value
                        break
                    yield chunk
                if not isinstance(observation, AgentObservation):
                    raise TypeError("driver.execute must return an AgentObservation")
                if observation.outcome == ActionOutcome.FAILED:
                    self._state_machine.apply_step_event(
                        agent_state,
                        StepStateEvent(
                            StepEventType.FAILED,
                            active_step_id,
                            error_code=observation.error_code or "UNHANDLED_ERROR",
                            error_message=observation.error_message or "Agent execution failed",
                        ),
                    )
                    active_step_id = None
                    self._apply_failed_run_event(
                        agent_state,
                        RunEventType.FAILED,
                        StopReason.UNHANDLED_ERROR,
                        observation.error_code or "UNHANDLED_ERROR",
                        observation.error_message or "Agent execution failed",
                    )
                    self._observe(agent_state, state_observer)
                    return
                self._state_machine.apply_step_event(
                    agent_state,
                    StepStateEvent(StepEventType.SUCCEEDED, active_step_id),
                )
                active_step_id = None
                if observation.outcome == ActionOutcome.COMPLETED:
                    self._state_machine.apply_run_event(
                        agent_state,
                        RunStateEvent(
                            RunEventType.COMPLETED,
                            stop_reason=StopReason.COMPLETED,
                            final_output=observation.final_output,
                        ),
                    )
                    self._observe(agent_state, state_observer)
                    logger.info("AgentState final", extra={"agent_state": agent_state.to_dict()})
                    return
                previous_observation = observation
        except GeneratorExit:
            raise
        except BudgetExceededError:
            if active_step_id is not None:
                self._state_machine.apply_step_event(agent_state, StepStateEvent(StepEventType.FAILED, active_step_id, error_code="BUDGET_EXHAUSTED", error_message="预算额度不足"))
            self._apply_failed_run_event(agent_state, RunEventType.BUDGET_EXHAUSTED, StopReason.BUDGET_EXHAUSTED, "BUDGET_EXHAUSTED", "预算额度不足")
            self._observe(agent_state, state_observer)
            raise
        except RunDeadlineExceededError:
            if active_step_id is not None:
                self._state_machine.apply_step_event(
                    agent_state,
                    StepStateEvent(
                        StepEventType.FAILED,
                        active_step_id,
                        error_code="DEADLINE_EXCEEDED",
                        error_message="Run deadline exceeded",
                    ),
                )
                active_step_id = None
            self._apply_failed_run_event(
                agent_state,
                RunEventType.DEADLINE_EXCEEDED,
                StopReason.DEADLINE_EXCEEDED,
                "DEADLINE_EXCEEDED",
                "Run deadline exceeded",
            )
            self._observe(agent_state, state_observer)
            logger.info("AgentState final", extra={"agent_state": agent_state.to_dict()})
            raise
        except RunCancelledError:
            if active_step_id is not None:
                self._state_machine.apply_step_event(
                    agent_state,
                    StepStateEvent(
                        StepEventType.CANCELLED,
                        active_step_id,
                        error_code="USER_CANCELLED",
                        error_message="Run cancelled",
                    ),
                )
                active_step_id = None
            self._state_machine.apply_run_event(
                agent_state,
                RunStateEvent(
                    RunEventType.CANCELLED,
                    stop_reason=StopReason.USER_CANCELLED,
                    error_code="USER_CANCELLED",
                    error_message="Run cancelled",
                ),
            )
            self._observe(agent_state, state_observer)
            logger.info("AgentState final", extra={"agent_state": agent_state.to_dict()})
            raise
        except Exception:
            if active_step_id is not None:
                self._state_machine.apply_step_event(
                    agent_state,
                    StepStateEvent(
                        StepEventType.FAILED,
                        active_step_id,
                        error_code="UNHANDLED_ERROR",
                        error_message="Agent execution failed",
                    ),
                )
                active_step_id = None
            self._apply_failed_run_event(
                agent_state,
                RunEventType.FAILED,
                StopReason.UNHANDLED_ERROR,
                "UNHANDLED_ERROR",
                "Agent execution failed",
            )
            self._observe(agent_state, state_observer)
            logger.exception("Agent loop execution failed")
            logger.info("AgentState final", extra={"agent_state": agent_state.to_dict()})
            raise

    def _apply_failed_run_event(
        self,
        agent_state: AgentState,
        event_type: RunEventType,
        reason: StopReason,
        code: str,
        message: str,
    ) -> None:
        self._state_machine.apply_run_event(
            agent_state,
            RunStateEvent(
                event_type,
                stop_reason=reason,
                error_code=code,
                error_message=message,
            ),
        )

    @staticmethod
    def _observe(agent_state: AgentState, observer: Callable[[AgentState], None] | None) -> None:
        if observer is not None:
            observer(agent_state)
