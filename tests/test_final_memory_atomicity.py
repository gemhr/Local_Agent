from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from core.memory_manager import (
    MemoryExchangeError,
    MemoryExchangeErrorCode,
    MemoryManager,
)
from core.runtime.final_memory_writer import RunFinalMemoryWriter
from core.runtime.planning import (
    ExecutionKind,
    OutputPolicy,
    Plan,
    PlanSource,
    PlanStep,
    TaskCapabilityRequirements,
)
from core.runtime.step_result import ResultContentType, StepResult
from core.runtime.step_result_store import StepResultStore
from core.runtime.state import AgentState
from core.runtime.state_machine import (
    AgentStateMachine,
    RunEventType,
    RunStateEvent,
    StepEventType,
    StepStateEvent,
)


def plan() -> Plan:
    return Plan(
        "plan",
        1,
        "summary",
        (
            PlanStep(
                "answer",
                "answer",
                "desc",
                (),
                "done",
                "core_router",
                TaskCapabilityRequirements(),
                ExecutionKind.AGENT,
                OutputPolicy.FINAL_PASSTHROUGH,
            ),
        ),
        datetime.now(UTC),
        PlanSource.DETERMINISTIC,
    )


class StubRouter:
    DIRECT_MEMORY_SCOPE = "direct"

    def __init__(self, memory_manager):
        self.memory_manager = memory_manager


def make_store() -> StepResultStore:
    p = plan()
    store = StepResultStore(p, run_id="run")
    state = AgentState.for_run_context("run")
    machine = AgentStateMachine()
    machine.apply_run_event(state, RunStateEvent(RunEventType.STARTED))
    for step in p.steps:
        machine.register_plan_step(state, step_id=step.step_id, name=step.title)
    machine.apply_step_event(
        state,
        StepStateEvent(
            StepEventType.STARTED,
            "answer",
            occurred_at=datetime.now(UTC),
        ),
    )
    machine.apply_step_event(
        state,
        StepStateEvent(
            StepEventType.SUCCEEDED,
            "answer",
            occurred_at=datetime.now(UTC),
        ),
    )
    store.write_prepared(
        StepResult("answer", "core_router", ResultContentType.TEXT, "FINAL"),
        expected_agent_id="core_router",
    )
    store.mark_readable("answer", state)
    return store


def test_atomic_exchange_commits_both_or_nothing(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory.db"))
    result = manager.append_exchange_atomic(
        "core_router",
        "direct",
        "user-text",
        "assistant-text",
        run_id="run-1",
    )
    assert result["exchange_id"] == "run-1"
    history = manager.get_chat_history("core_router", ascending=True)
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
    ]
    assert [message["content"] for message in history] == [
        "user-text",
        "assistant-text",
    ]

    with pytest.raises(MemoryExchangeError) as exc:
        manager.append_exchange_atomic(
            "core_router",
            "direct",
            "user-text-2",
            "assistant-text-2",
            run_id="run-1",
        )
    assert exc.value.error_code == MemoryExchangeErrorCode.DUPLICATE_EXCHANGE
    # 重复提交绝不重发用户正文。
    assert manager.count_messages("core_router") == 2


def test_incomplete_exchange_is_filtered_from_history(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory.db"))
    conn = sqlite3.connect(manager.db_path)
    conn.execute(
        """
        INSERT INTO message_exchanges
            (exchange_id, run_id, agent_id, memory_scope, state)
        VALUES ('partial-1', 'run-partial', 'core_router', 'direct', 'PENDING')
        """
    )
    conn.execute(
        """
        INSERT INTO messages
            (agent_id, role, content, metadata, memory_scope,
             exchange_id, run_id, sequence)
        VALUES ('core_router', 'user', 'orphan-user', NULL, 'direct',
                'partial-1', 'run-partial', 0)
        """
    )
    conn.commit()
    conn.close()

    history = manager.get_chat_history("core_router", ascending=True)
    assert history == []
    all_messages = manager.get_all_messages()
    assert all_messages == []


def test_writer_is_write_once_and_never_retries_after_failure(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory.db"))

    class FailingMemoryManager(MemoryManager):
        def append_exchange_atomic(self, *args, **kwargs):
            raise MemoryExchangeError(
                MemoryExchangeErrorCode.EXCHANGE_FAILED,
                "simulated failure",
            )

    writer = RunFinalMemoryWriter(
        StubRouter(FailingMemoryManager(str(tmp_path / "failing.db"))),
        entry_agent_id="core_router",
        user_request="user-request",
        persist=True,
        run_id="run-1",
    )
    store = make_store()
    with pytest.raises(MemoryExchangeError):
        writer.write_delivered(final_step_id="answer", store=store)
    assert writer.written is True
    # 失败后同一 Run 不得自动重试 / 不得再次写入。
    with pytest.raises(RuntimeError, match="只能写入一次"):
        writer.write_delivered(final_step_id="answer", store=store)
    assert manager.count_messages("core_router") == 0


def test_writer_commits_delivered_exchange_only(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory.db"))
    writer = RunFinalMemoryWriter(
        StubRouter(manager),
        entry_agent_id="core_router",
        user_request="SECRET_USER_INSTRUCTION",
        persist=True,
        run_id="run-1",
    )
    writer.write_delivered(final_step_id="answer", store=make_store())
    history = manager.get_chat_history("core_router", ascending=True)
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
    ]
    assert history[0]["content"] == "SECRET_USER_INSTRUCTION"
    assert history[1]["content"] == "FINAL"
    # specialist raw 永不写入：Memory 只保存 delivered exchange。
    assert all(
        "SECRET_SPECIALIST" not in message["content"]
        for message in history
    )


def test_writer_respects_persist_disabled(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory.db"))
    writer = RunFinalMemoryWriter(
        StubRouter(manager),
        entry_agent_id="core_router",
        user_request="user",
        persist=False,
        run_id="run-1",
    )
    writer.write_delivered(final_step_id="answer", store=make_store())
    assert manager.count_messages("core_router") == 0
