#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Minimal synchronous agent loop and legacy AgentRouter compatibility driver."""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass
from enum import Enum
import logging
import re
from typing import Protocol

from core.runtime.cancellation import RunCancelledError
from core.runtime.context import RunContext, RunDeadlineExceededError
from core.runtime.state import AgentState, AgentStateValidationError, RunStatus, StopReason


logger = logging.getLogger(__name__)

LEGACY_AGENT_ROUTER_STEP_ID = "legacy-agent-router"
LEGACY_AGENT_ROUTER_STEP_NAME = "Legacy AgentRouter execution"
ORCHESTRATION_EVENT_PREFIX = "[[ORCH]]"
_SAFE_DEDUP_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ActionOutcome(str, Enum):
    """The safe terminal result reported by one accepted action."""

    CONTINUE = "CONTINUE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class AgentAction:
    """A non-sensitive action selected by an AgentLoopDriver."""

    step_id: str
    name: str
    action_type: str
    dedup_key: str

    def __post_init__(self) -> None:
        """Reject empty fields and require an opaque, non-path deduplication key."""
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
    """Safe result of an action, without exception objects or traceback text."""

    outcome: ActionOutcome
    final_output: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        """Validate observation fields without accepting untyped or empty error metadata."""
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
        """Return whether the loop should request a further decision."""
        return self.outcome == ActionOutcome.CONTINUE


@dataclass(frozen=True)
class AgentLoopPolicy:
    """Bounded loop limits owned explicitly by an AgentLoop instance."""

    max_steps: int = 8
    max_consecutive_no_action: int = 1
    max_consecutive_same_action: int = 2

    def __post_init__(self) -> None:
        """Require positive integers while rejecting bool, which is an int subclass."""
        for field_name, value in (
            ("max_steps", self.max_steps),
            ("max_consecutive_no_action", self.max_consecutive_no_action),
            ("max_consecutive_same_action", self.max_consecutive_same_action),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


class AgentLoopDriver(Protocol):
    """Minimal decision and execution boundary for the synchronous loop."""

    def decide(self, previous_observation: AgentObservation | None) -> AgentAction | None:
        """Select the next action without performing it."""

    def execute(self, action: AgentAction, run_context: RunContext) -> Generator[str, None, AgentObservation]:
        """Execute an accepted action and return its safe observation."""


class LegacyAgentRouter(Protocol):
    """Structural legacy Router dependency needed by the compatibility driver."""

    def chat_stream(
        self, user_query: str, agent_id: str = "core_router", run_context: RunContext | None = None
    ) -> Generator[str, None, None]:
        """Yield existing text and orchestration chunks."""


class LegacyAgentRouterDriver:
    """Present the unchanged AgentRouter stream as one completed loop action."""

    def __init__(self, router: LegacyAgentRouter, *, user_query: str, agent_id: str) -> None:
        self._router = router
        self._user_query = user_query
        self._agent_id = agent_id
        self._decided = False

    def decide(self, previous_observation: AgentObservation | None) -> AgentAction | None:
        """Return exactly one legacy action; its completed observation ends the run."""
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
        """Forward chunks unchanged and aggregate the final text output once."""
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
    """Own one RunContext/AgentState execution lifecycle using a bounded driver loop."""

    def __init__(self, policy: AgentLoopPolicy | None = None) -> None:
        self._policy = policy or AgentLoopPolicy()

    def run_stream(
        self,
        *,
        run_context: RunContext,
        agent_state: AgentState,
        driver: AgentLoopDriver,
        state_observer: Callable[[AgentState], None] | None = None,
    ) -> Generator[str, None, None]:
        """Run a driver while applying state updates and bounded termination policies."""
        agent_state.assert_matches_run_context(run_context.run_id)
        if agent_state.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
            raise AgentStateValidationError("cannot run an already terminated AgentState")
        agent_state.mark_running()
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
                    self._mark_failed(agent_state, StopReason.MAX_STEPS_REACHED, "MAX_STEPS_REACHED", "Maximum steps reached")
                    self._observe(agent_state, state_observer)
                    return
                action = driver.decide(previous_observation)
                run_context.raise_if_inactive()
                if action is None:
                    consecutive_no_action += 1
                    if consecutive_no_action >= self._policy.max_consecutive_no_action:
                        self._mark_failed(agent_state, StopReason.NO_ACTION, "NO_ACTION", "No action available")
                        self._observe(agent_state, state_observer)
                        return
                    continue
                consecutive_no_action = 0
                consecutive_same_action = (
                    consecutive_same_action + 1 if action.dedup_key == previous_dedup_key else 1
                )
                previous_dedup_key = action.dedup_key
                if consecutive_same_action > self._policy.max_consecutive_same_action:
                    self._mark_failed(agent_state, StopReason.REPEATED_ACTION, "REPEATED_ACTION", "Repeated action limit reached")
                    self._observe(agent_state, state_observer)
                    return
                steps_taken += 1
                agent_state.add_step(action.step_id, action.name)
                agent_state.start_step(action.step_id)
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
                    agent_state.fail_step(active_step_id, error_code=observation.error_code or "UNHANDLED_ERROR", error_message=observation.error_message or "Agent execution failed")
                    self._mark_failed(agent_state, StopReason.UNHANDLED_ERROR, observation.error_code or "UNHANDLED_ERROR", observation.error_message or "Agent execution failed")
                    self._observe(agent_state, state_observer)
                    return
                agent_state.succeed_step(active_step_id)
                active_step_id = None
                if observation.outcome == ActionOutcome.COMPLETED:
                    agent_state.mark_succeeded(final_output=observation.final_output)
                    self._observe(agent_state, state_observer)
                    logger.info("AgentState final", extra={"agent_state": agent_state.to_dict()})
                    return
                previous_observation = observation
        except GeneratorExit:
            raise
        except RunDeadlineExceededError:
            if active_step_id is not None:
                agent_state.fail_step(active_step_id, error_code="DEADLINE_EXCEEDED", error_message="Run deadline exceeded")
            self._mark_failed(agent_state, StopReason.DEADLINE_EXCEEDED, "DEADLINE_EXCEEDED", "Run deadline exceeded")
            self._observe(agent_state, state_observer)
            logger.info("AgentState final", extra={"agent_state": agent_state.to_dict()})
            raise
        except RunCancelledError:
            if active_step_id is not None:
                agent_state.cancel_step(active_step_id, error_code="USER_CANCELLED", error_message="Run cancelled")
            agent_state.mark_cancelled(stop_reason=StopReason.USER_CANCELLED, error_code="USER_CANCELLED", error_message="Run cancelled")
            self._observe(agent_state, state_observer)
            logger.info("AgentState final", extra={"agent_state": agent_state.to_dict()})
            raise
        except Exception:
            if active_step_id is not None:
                agent_state.fail_step(active_step_id, error_code="UNHANDLED_ERROR", error_message="Agent execution failed")
            self._mark_failed(agent_state, StopReason.UNHANDLED_ERROR, "UNHANDLED_ERROR", "Agent execution failed")
            self._observe(agent_state, state_observer)
            logger.exception("Agent loop execution failed")
            logger.info("AgentState final", extra={"agent_state": agent_state.to_dict()})
            raise

    @staticmethod
    def _mark_failed(agent_state: AgentState, reason: StopReason, code: str, message: str) -> None:
        agent_state.mark_failed(stop_reason=reason, error_code=code, error_message=message)

    @staticmethod
    def _observe(agent_state: AgentState, observer: Callable[[AgentState], None] | None) -> None:
        if observer is not None:
            observer(agent_state)
