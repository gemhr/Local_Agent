"""WP6 final Memory failure matrix: atomicity, write-once, no-recommit."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from core.memory_manager import MemoryExchangeError, MemoryManager
from core.runtime import (
    CoordinatedRuntimeFactory,
    FaultPoint,
    OutputGateState,
    RunStatus,
    RuntimeEventType,
)
from core.runtime.multi_agent_status import format_frontend_status
from tests._stage2_5_wp6_fixtures import wp6_controller
from tests._wp3_fixtures import make_wp3_services
from tests.test_wp3_history_boundary import FakeModel, make_real_router


def _records(services, run_id: str):
    return services.event_journal.read_after(run_id, 0, 1000)


async def _run_with_memory_fault(
    fault_point,
    *,
    operation_kind: str,
    expected_code: str,
):
    services = make_wp3_services()
    with tempfile.TemporaryDirectory() as directory:
        controller = wp6_controller(
            fault_point,
            component="memory_manager",
            operation_kind=operation_kind,
        )
        memory = MemoryManager(
            str(Path(directory) / "memory.db"),
            fault_controller=controller,
        )
        model = FakeModel(
            planning_json=(
                '{"schema_version":1,"decision":"DELEGATE","tasks":['
                '{"task_id":"code","agent_id":"code_expert",'
                '"instruction":"inspect code","capabilities":["code_reasoning"]}'
                '],"synthesis_required":true}'
            )
        )
        router = make_real_router(memory, model=model)
        scope = await CoordinatedRuntimeFactory(
            router, services
        ).create_run_scope("core_router", "coordinate one review")

        result = await scope.execute()

        assert result.status is RunStatus.FAILED
        assert result.error_code == expected_code
        # 已交付、Final Step SUCCEEDED、Gate PUBLISHED。
        assert "synthesis" in result.succeeded_step_ids
        gate = scope.coordinator.output_gate
        assert gate is not None and gate.state is OutputGateState.PUBLISHED
        types = [item.event_type for item in _records(services, scope.run_id)]
        assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
        # 不重发：只有一次正文事实。
        # 无半个已提交 exchange：无任何 message 行。
        assert memory.count_messages("core_router") == 0
        assert memory.get_chat_history("core_router", ascending=True) == []
        await scope.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault_point", "operation_kind"),
    [
        (FaultPoint.MEMORY_BEFORE_EXCHANGE_BEGIN, "EXCHANGE_BEGIN"),
        (FaultPoint.MEMORY_BEFORE_USER_INSERT, "USER_INSERT"),
        (FaultPoint.MEMORY_BEFORE_ASSISTANT_INSERT, "ASSISTANT_INSERT"),
        (FaultPoint.MEMORY_BEFORE_EXCHANGE_COMMIT, "EXCHANGE_COMMIT"),
    ],
)
async def test_memory_exchange_faults_fail_run_without_resend(
    fault_point, operation_kind
) -> None:
    await _run_with_memory_fault(
        fault_point,
        operation_kind=operation_kind,
        expected_code="FINAL_OUTPUT_MEMORY_COMMIT_FAILED",
    )


@pytest.mark.asyncio
async def test_delivered_memory_failed_frontend_text() -> None:
    """前端分层文案：回答已交付但记忆失败，绝不建议重试。"""
    services = make_wp3_services()
    with tempfile.TemporaryDirectory() as directory:
        controller = wp6_controller(
            FaultPoint.MEMORY_BEFORE_EXCHANGE_COMMIT,
            component="memory_manager",
            operation_kind="EXCHANGE_COMMIT",
        )
        memory = MemoryManager(
            str(Path(directory) / "memory.db"),
            fault_controller=controller,
        )
        model = FakeModel(
            planning_json=(
                '{"schema_version":1,"decision":"DIRECT_ANSWER",'
                '"agent_id":"core_router","reason_code":"MODEL_DIRECT"}'
            )
        )
        router = make_real_router(memory, model=model)
        scope = await CoordinatedRuntimeFactory(
            router, services
        ).create_run_scope("core_router", "memory failed frontend")

        result = await scope.execute()

        assert result.error_code == "FINAL_OUTPUT_MEMORY_COMMIT_FAILED"
        run_completed = next(
            item
            for item in _records(services, scope.run_id)
            if item.event_type is RuntimeEventType.RUN_COMPLETED
        )
        text = format_frontend_status(
            {
                "event_type": "RUN_COMPLETED",
                "payload": dict(run_completed.safe_payload),
            }
        )
        assert text == "回答已交付，记忆保存失败。"
        await scope.close()


@pytest.mark.asyncio
async def test_rollback_does_not_corrupt_next_exchange() -> None:
    """一次 user insert 失败整体回滚后，新 Run 的 exchange 可正常提交。"""
    services = make_wp3_services()
    with tempfile.TemporaryDirectory() as directory:
        controller = wp6_controller(
            FaultPoint.MEMORY_BEFORE_USER_INSERT,
            component="memory_manager",
            operation_kind="USER_INSERT",
        )
        memory = MemoryManager(
            str(Path(directory) / "memory.db"),
            fault_controller=controller,
        )
        model = FakeModel(
            planning_json=(
                '{"schema_version":1,"decision":"DIRECT_ANSWER",'
                '"agent_id":"core_router","reason_code":"MODEL_DIRECT"}'
            )
        )
        router = make_real_router(memory, model=model)
        scope = await CoordinatedRuntimeFactory(
            router, services
        ).create_run_scope("core_router", "rollback integrity")

        result = await scope.execute()

        assert result.error_code == "FINAL_OUTPUT_MEMORY_COMMIT_FAILED"
        assert memory.count_messages("core_router") == 0
        # 同一 MemoryManager 后续提交（新 run_id）成功，证明回滚未损坏库。
        committed = memory.append_exchange_atomic(
            "core_router",
            "direct",
            "user-next",
            "assistant-next",
            run_id="next-run-1",
        )
        assert committed["exchange_id"] == "next-run-1"
        history = memory.get_chat_history("core_router", ascending=True)
        assert [message["role"] for message in history] == ["user", "assistant"]
        assert history[1]["content"] == "assistant-next"
        await scope.close()


def test_duplicate_exchange_is_rejected_without_duplication() -> None:
    """FP-MEM-05: 同一 run_id 重复提交抛 DUPLICATE_EXCHANGE，历史不变。"""
    with tempfile.TemporaryDirectory() as directory:
        memory = MemoryManager(str(Path(directory) / "memory.db"))
        first = memory.append_exchange_atomic(
            "core_router",
            "direct",
            "user-1",
            "assistant-1",
            run_id="run-dup",
        )
        assert first["exchange_id"] == "run-dup"
        with pytest.raises(MemoryExchangeError) as exc:
            memory.append_exchange_atomic(
                "core_router",
                "direct",
                "user-2",
                "assistant-2",
                run_id="run-dup",
            )
        assert exc.value.error_code == "DUPLICATE_EXCHANGE"
        history = memory.get_chat_history("core_router", ascending=True)
        assert [message["role"] for message in history] == ["user", "assistant"]
        assert history[0]["content"] == "user-1"
        assert history[1]["content"] == "assistant-1"


def test_writer_failure_then_reinvoke_is_rejected() -> None:
    """FP-MEM-10/11: writer 失败后再次调用被拒绝，不自动重试。"""
    from datetime import UTC, datetime

    from core.runtime import (
        AgentState,
        AgentStateMachine,
        ResultContentType,
        RunEventType,
        RunFinalMemoryWriter,
        RunStateEvent,
        StepEventType,
        StepResult,
        StepResultStore,
        StepStateEvent,
        TaskCapabilityRequirements,
    )
    from core.runtime.planning import ExecutionKind, OutputPolicy, Plan, PlanSource, PlanStep

    class FailingOnceMemory:
        DIRECT_MEMORY_SCOPE = "direct"

        def __init__(self) -> None:
            self.calls = 0

        def append_exchange_atomic(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("simulated memory failure")

    capabilities = TaskCapabilityRequirements()
    step = PlanStep(
        "answer",
        "answer",
        "desc",
        (),
        "done",
        "core_router",
        capabilities,
        ExecutionKind.AGENT,
        OutputPolicy.FINAL_PASSTHROUGH,
    )
    plan = Plan(
        "plan-writer",
        1,
        "summary",
        (step,),
        datetime.now(UTC),
        PlanSource.DETERMINISTIC,
    )
    store = StepResultStore(plan, run_id="run-writer")
    state = AgentState.for_run_context("run-writer")
    machine = AgentStateMachine()
    machine.register_plan_step(state, step_id="answer", name="answer")
    machine.apply_run_event(state, RunStateEvent(RunEventType.STARTED))
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
        StepResult(
            "answer",
            "core_router",
            ResultContentType.TEXT,
            "FINAL-TEXT",
        ),
        expected_agent_id="core_router",
    )
    store.mark_readable("answer", state)

    memory = FailingOnceMemory()
    writer = RunFinalMemoryWriter(
        type(
            "Router",
            (),
            {"memory_manager": memory, "DIRECT_MEMORY_SCOPE": "direct"},
        ),
        entry_agent_id="core_router",
        user_request="user text",
        persist=True,
        run_id="run-writer",
    )
    with pytest.raises(RuntimeError):
        writer.write_delivered(final_step_id="answer", store=store)
    # 失败后 _written 保持 True：第二次调用被拒绝，绝不自动重试。
    with pytest.raises(RuntimeError, match="只能写入一次"):
        writer.write_delivered(final_step_id="answer", store=store)
    assert memory.calls == 1
