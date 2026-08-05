"""Shared WP3 test fixtures: planner outputs and a recording fake router."""

from __future__ import annotations

import json
import threading
import time

from tests._runtime_assembly_fixtures import FakeRouter, make_services


def direct_json(agent_id: str = "core_router") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "decision": "DIRECT_ANSWER",
            "agent_id": agent_id,
            "reason_code": "MODEL_DIRECT",
        }
    )


def delegated_json(
    *,
    task_ids: tuple[str, ...] = ("code",),
    synthesis_required: bool = True,
) -> str:
    """Build a DELEGATE planner output for shape 2/3 plans."""
    tasks = [
        {
            "task_id": task_id,
            "agent_id": _AGENT_FOR_TASK[task_id],
            "instruction": f"Inspect the {task_id} contract.",
            "capabilities": _CAPABILITY_FOR_TASK[task_id],
        }
        for task_id in task_ids
    ]
    return json.dumps(
        {
            "schema_version": 1,
            "decision": "DELEGATE",
            "tasks": tasks,
            "synthesis_required": synthesis_required,
        }
    )


_AGENT_FOR_TASK = {
    "code": "code_expert",
    "data": "data_analyst",
    "knowledge": "knowledge_expert",
}
_CAPABILITY_FOR_TASK = {
    "code": ["code_reasoning"],
    "data": ["data_analysis"],
    "knowledge": ["rag"],
}


class Wp3RecordingRouter(FakeRouter):
    """Records calls, supports barriers, failures and per-agent outputs."""

    def __init__(
        self,
        planning_output: str | None = None,
        *,
        fail_agents: tuple[str, ...] = (),
        barrier_agents: tuple[str, ...] = (),
        barrier: threading.Barrier | None = None,
        output_for: dict[str, str] | None = None,
        planning_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.planning_output = planning_output or direct_json()
        self.planning_calls = 0
        self.agent_calls: list[tuple[str, str, dict]] = []
        self.entered: dict[str, float] = {}
        self.exited: dict[str, float] = {}
        self.fail_agents = set(fail_agents)
        self.barrier_agents = set(barrier_agents)
        self.barrier = barrier
        self.output_for = output_for or {}
        self.planning_error = planning_error
        self.active = 0
        self.max_active = 0
        self.order: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def complete_planning_decision(self, user_request: str, **kwargs) -> str:
        self.planning_calls += 1
        if self.planning_error is not None:
            raise self.planning_error
        return self.planning_output

    def complete_single_agent(
        self, agent_id: str, query: str, **kwargs
    ) -> str:
        with self._lock:
            self.agent_calls.append((agent_id, query, kwargs))
            self.entered[agent_id] = time.monotonic()
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.order.append((agent_id, "enter"))
        try:
            if agent_id in self.fail_agents:
                raise RuntimeError("simulated specialist failure")
            if (
                agent_id in self.barrier_agents
                and self.barrier is not None
            ):
                self.barrier.wait(timeout=15)
            if agent_id in self.output_for:
                return self.output_for[agent_id]
            return f"result-{agent_id}"
        finally:
            with self._lock:
                self.exited[agent_id] = time.monotonic()
                self.active -= 1
                self.order.append((agent_id, "exit"))

    def calls_for(self, agent_id: str) -> list[tuple[str, str, dict]]:
        return [call for call in self.agent_calls if call[0] == agent_id]

    def prompts_for(self, agent_id: str) -> list[str]:
        return [call[1] for call in self.agent_calls if call[0] == agent_id]

    def persist_flags(self) -> list[bool]:
        return [call[2].get("persist") for call in self.agent_calls]


def shape2_planning_json() -> str:
    return delegated_json(task_ids=("code",), synthesis_required=True)


def shape3_planning_json() -> str:
    return delegated_json(
        task_ids=("code", "knowledge"),
        synthesis_required=True,
    )


def shape3_parallel_router() -> Wp3RecordingRouter:
    barrier = threading.Barrier(2)
    return Wp3RecordingRouter(
        shape3_planning_json(),
        barrier_agents=("code_expert", "knowledge_expert"),
        barrier=barrier,
    )


def make_wp3_services(**kwargs):
    return make_services(**kwargs)


__all__ = [
    "Wp3RecordingRouter",
    "delegated_json",
    "direct_json",
    "make_wp3_services",
    "shape2_planning_json",
    "shape3_parallel_router",
    "shape3_planning_json",
]
