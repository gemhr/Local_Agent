"""WP6 required success E2E: Shape 0-3 main chains with full contract checks."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest

from core.memory_manager import MemoryManager
from core.runtime import (
    CoordinatedRuntimeFactory,
    InMemoryMetricsRecorder,
    InMemorySpanRecorder,
    RunStatus,
    RuntimeEventType,
    StopReason,
)
from core.runtime.multi_agent_status import format_frontend_status
from core.runtime.trace_contract import (
    RUNTIME_FINAL_MEMORY_COMMIT_SPAN,
    RUNTIME_OUTPUT_DELIVERY_SPAN,
    RUNTIME_PLANNING_SPAN,
    RUNTIME_RUN_SPAN,
    RUNTIME_STEP_SPAN,
    RUNTIME_SYNTHESIS_SPAN,
)
from tests._wp3_fixtures import (
    Wp3RecordingRouter,
    delegated_json,
    direct_json,
    make_wp3_services,
)
from tests.test_wp3_history_boundary import FakeModel, make_real_router


def _records(services, run_id: str):
    return services.event_journal.read_after(run_id, 0, 1000)


def _output_digests(services, run_id: str) -> list[str]:
    return [
        item.safe_payload["text_digest"]
        for item in _records(services, run_id)
        if item.event_type is RuntimeEventType.OUTPUT_DELTA
    ]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _frontend_terminal_text(services, run_id: str) -> str:
    run_completed = next(
        item
        for item in _records(services, run_id)
        if item.event_type is RuntimeEventType.RUN_COMPLETED
    )
    return (
        format_frontend_status(
            {
                "event_type": "RUN_COMPLETED",
                "payload": dict(run_completed.safe_payload),
            }
        )
        or ""
    )


async def _assert_success_contract(
    *,
    scope,
    services,
    recorder,
    metrics,
    router,
    expected_agents: tuple[str, ...],
    expected_synthesis: bool,
    final_text: str,
    entry_agent: str,
    expected_shape: str,
) -> None:
    result = await scope.execute()
    assert result.status is RunStatus.SUCCEEDED
    assert result.stop_reason is StopReason.COMPLETED
    assert result.error_code is None

    types = [item.event_type for item in _records(services, scope.run_id)]
    assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
    assert _digest(final_text) in _output_digests(services, scope.run_id)
    # INTERNAL 结果正文永不进入用户正文通道。
    for agent in expected_agents:
        assert _digest(f"result-{agent}") not in _output_digests(
            services, scope.run_id
        )
    # 事件顺序：OUTPUT_DELTA < STEP_COMPLETED(finish)。
    completed_indexes = [
        index
        for index, item in enumerate(_records(services, scope.run_id))
        if item.event_type is RuntimeEventType.STEP_COMPLETED
    ]
    assert completed_indexes
    assert types.index(RuntimeEventType.OUTPUT_DELTA) < completed_indexes[-1]
    # Journal 永不保存正文（只保存 digest/length）。
    journal_text = repr(_records(services, scope.run_id))
    assert final_text not in journal_text
    assert "result-" not in journal_text

    # Frontend terminal 状态为成功文案。
    assert "回答已交付" in _frontend_terminal_text(
        services, scope.run_id
    )

    # Trace 拓扑：run -> planning -> step* -> [synthesis] -> delivery -> memory。
    spans = recorder.snapshot()
    run_spans = [s for s in spans if s.operation == RUNTIME_RUN_SPAN]
    planning_spans = [
        s for s in spans if s.operation == RUNTIME_PLANNING_SPAN
    ]
    step_spans = [s for s in spans if s.operation == RUNTIME_STEP_SPAN]
    delivery_spans = [
        s for s in spans if s.operation == RUNTIME_OUTPUT_DELIVERY_SPAN
    ]
    memory_spans = [
        s for s in spans if s.operation == RUNTIME_FINAL_MEMORY_COMMIT_SPAN
    ]
    assert len(run_spans) == 1
    assert len(planning_spans) == 1
    assert planning_spans[0].parent_span_id == run_spans[0].span_id
    # 每个 specialist 一个 step span + 唯一 final step span。
    assert len(step_spans) == len(expected_agents) + 1
    assert len(delivery_spans) == 1
    assert len(memory_spans) == 1
    assert delivery_spans[0].attributes["delivery_status"] == "DELIVERED"
    assert memory_spans[0].attributes["user_write_status"] == "WRITTEN"
    assert memory_spans[0].attributes["assistant_write_status"] == "WRITTEN"
    if expected_synthesis:
        synthesis_spans = [
            s for s in spans if s.operation == RUNTIME_SYNTHESIS_SPAN
        ]
        assert len(synthesis_spans) == 1

    # Metrics：成功 Run delivery 恰好一次，planning 成功计数存在。
    snap = metrics.snapshot()
    assert snap.counter(
        "runtime_output_delivery_total",
        {"status": "DELIVERED", "error_code": "OK"},
    ) == 1
    assert snap.counter(
        "runtime_planning_total",
        {"planning_source": "MODEL", "status": "SUCCEEDED"},
    ) == 1

    # Memory：唯一 delivered exchange = user + assistant 各 1 条。
    assert router.memory_manager.count_messages(entry_agent) == 2
    history = router.memory_manager.get_chat_history(
        entry_agent, ascending=True
    )
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[1]["content"] == final_text

    # 默认动态入口始终经过 Planner model 一次（显式 entry 为零调用，
    # 由各自专项测试断言）。
    assert router.planning_calls == 1

    # shape 由真实 plan 计算并通过 PLANNING span 校验。
    plan_span = planning_spans[0]
    assert plan_span.attributes["compiled_shape"] == expected_shape

    await scope.close()


@pytest.mark.asyncio
async def test_shape1_explicit_knowledge_entry_single_output() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(direct_json("knowledge_expert"))
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("knowledge_expert", "显式知识专家请求")

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert router.planning_calls == 0
    assert len(router.calls_for("knowledge_expert")) == 1
    assert router.memory_manager.count_messages("knowledge_expert") == 2
    await scope.close()


@pytest.mark.asyncio
async def test_shape1_explicit_data_entry_single_output() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(direct_json("data_analyst"))
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("data_analyst", "显式数据专家请求")

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert router.planning_calls == 0
    assert len(router.calls_for("data_analyst")) == 1
    assert router.memory_manager.count_messages("data_analyst") == 2
    await scope.close()


@pytest.mark.asyncio
async def test_shape2_data_analyst_plus_synthesis_single_output() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        delegated_json(task_ids=("data",), synthesis_required=True),
        output_for={"synthesis_agent": "SHAPE2_DATA_FINAL"},
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("core_router", "coordinate one data review")

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert result.succeeded_step_ids == ("task-data", "synthesis")
    assert len(router.calls_for("data_analyst")) == 1
    assert len(router.calls_for("synthesis_agent")) == 1
    assert len(router.calls_for("code_expert")) == 0
    types = [item.event_type for item in _records(services, scope.run_id)]
    assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
    assert _digest("SHAPE2_DATA_FINAL") in _output_digests(
        services, scope.run_id
    )
    assert _digest("result-data_analyst") not in _output_digests(
        services, scope.run_id
    )
    await scope.close()


@pytest.mark.asyncio
async def test_shape3_knowledge_plus_data_single_output() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        delegated_json(
            task_ids=("knowledge", "data"), synthesis_required=True
        ),
        output_for={"synthesis_agent": "SHAPE3_KD_FINAL"},
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("core_router", "coordinate knowledge and data")

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert set(result.succeeded_step_ids) == {
        "task-knowledge",
        "task-data",
        "synthesis",
    }
    assert result.succeeded_step_ids[-1] == "synthesis"
    assert len(router.calls_for("knowledge_expert")) == 1
    assert len(router.calls_for("data_analyst")) == 1
    assert len(router.calls_for("synthesis_agent")) == 1
    types = [item.event_type for item in _records(services, scope.run_id)]
    assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
    assert _digest("SHAPE3_KD_FINAL") in _output_digests(
        services, scope.run_id
    )
    await scope.close()


@pytest.mark.asyncio
async def test_shape3_code_plus_data_single_output() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        delegated_json(task_ids=("code", "data"), synthesis_required=True),
        output_for={"synthesis_agent": "SHAPE3_CD_FINAL"},
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("core_router", "coordinate code and data")

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert set(result.succeeded_step_ids) == {
        "task-code",
        "task-data",
        "synthesis",
    }
    assert result.succeeded_step_ids[-1] == "synthesis"
    assert len(router.calls_for("code_expert")) == 1
    assert len(router.calls_for("data_analyst")) == 1
    assert len(router.calls_for("synthesis_agent")) == 1
    types = [item.event_type for item in _records(services, scope.run_id)]
    assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
    assert _digest("SHAPE3_CD_FINAL") in _output_digests(
        services, scope.run_id
    )
    await scope.close()


@pytest.mark.asyncio
async def test_shape3_three_specialists_single_output() -> None:
    """Shape 3 三 specialist：code + knowledge + data 并行后唯一 synthesis。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        delegated_json(
            task_ids=("code", "knowledge", "data"),
            synthesis_required=True,
        ),
        output_for={"synthesis_agent": "SHAPE3_THREE_FINAL"},
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("core_router", "coordinate three specialists")

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert set(result.succeeded_step_ids) == {
        "task-code",
        "task-knowledge",
        "task-data",
        "synthesis",
    }
    assert result.succeeded_step_ids[-1] == "synthesis"
    assert len(router.calls_for("code_expert")) == 1
    assert len(router.calls_for("knowledge_expert")) == 1
    assert len(router.calls_for("data_analyst")) == 1
    assert len(router.calls_for("synthesis_agent")) == 1
    types = [item.event_type for item in _records(services, scope.run_id)]
    assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
    output_digests = _output_digests(services, scope.run_id)
    assert _digest("SHAPE3_THREE_FINAL") in output_digests
    assert _digest("result-code_expert") not in output_digests
    assert _digest("result-knowledge_expert") not in output_digests
    assert _digest("result-data_analyst") not in output_digests
    assert router.memory_manager.count_messages("core_router") == 2
    await scope.close()


@pytest.mark.asyncio
async def test_shape3_full_contract_with_trace_metrics_and_frontend() -> None:
    """每场景断言完整清单：Planner 次数、shape、Agent 次数、并行、
    synthesis 次数、INTERNAL 输出 0、final OUTPUT 1、Final Step/Run、
    Memory exchange 1、Trace 拓扑、Journal 无正文、frontend 终态。"""
    recorder = InMemorySpanRecorder()
    metrics = InMemoryMetricsRecorder()
    services = make_wp3_services(
        span_recorder=recorder,
        runtime_metrics_recorder=metrics,
    )
    router = Wp3RecordingRouter(
        delegated_json(
            task_ids=("code", "knowledge", "data"),
            synthesis_required=True,
        ),
        output_for={"synthesis_agent": "SHAPE3_FULL_FINAL"},
    )
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("core_router", "full contract shape3")

    await _assert_success_contract(
        scope=scope,
        services=services,
        recorder=recorder,
        metrics=metrics,
        router=router,
        expected_agents=("code_expert", "knowledge_expert", "data_analyst"),
        expected_synthesis=True,
        final_text="SHAPE3_FULL_FINAL",
        entry_agent="core_router",
        expected_shape="3",
    )


@pytest.mark.asyncio
async def test_shape0_full_contract() -> None:
    recorder = InMemorySpanRecorder()
    metrics = InMemoryMetricsRecorder()
    services = make_wp3_services(
        span_recorder=recorder,
        runtime_metrics_recorder=metrics,
    )
    router = Wp3RecordingRouter(direct_json("core_router"))
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope("core_router", "direct core question")

    await _assert_success_contract(
        scope=scope,
        services=services,
        recorder=recorder,
        metrics=metrics,
        router=router,
        expected_agents=(),
        expected_synthesis=False,
        final_text="result-core_router",
        entry_agent="core_router",
        expected_shape="0",
    )


@pytest.mark.asyncio
async def test_deterministic_data_query_routes_to_data_analyst_without_model() -> None:
    """真实 E2E：\"查数据库…csv\" 走确定性路由，planning_calls==0，
    data_analyst + synthesis 各 1 次，唯一 OUTPUT，Memory exchange 完整。"""
    services = make_wp3_services()
    router = Wp3RecordingRouter()
    scope = await CoordinatedRuntimeFactory(
        router, services
    ).create_run_scope(
        "core_router",
        "查数据库，mock_test_results.csv这个表的第四列的表头是什么",
    )

    result = await scope.execute()

    assert result.status is RunStatus.SUCCEEDED
    assert router.planning_calls == 0
    assert len(router.calls_for("data_analyst")) == 1
    assert len(router.calls_for("synthesis_agent")) == 1
    assert len(router.calls_for("code_expert")) == 0
    types = [item.event_type for item in _records(services, scope.run_id)]
    assert types.count(RuntimeEventType.OUTPUT_DELTA) == 1
    assert router.memory_manager.count_messages("core_router") == 2
    await scope.close()


@pytest.mark.asyncio
async def test_planner_prompt_constrains_capability_whitelist() -> None:
    """模型路径的 planner system prompt 必须包含 capability 白名单。"""
    services = make_wp3_services()
    with tempfile.TemporaryDirectory() as directory:
        memory = MemoryManager(str(Path(directory) / "memory.db"))
        model = FakeModel(planning_json=direct_json("core_router"))
        router = make_real_router(memory, model=model)
        scope = await CoordinatedRuntimeFactory(
            router, services
        ).create_run_scope("core_router", "prompt whitelist")

        result = await scope.execute()

        assert result.status is RunStatus.SUCCEEDED
        assert model.all_messages, "planner 模型调用应被记录"
        system_prompt = model.all_messages[0][0]["content"]
        assert "LocalAgent Planner" in system_prompt
        assert "data_analysis" in system_prompt
        assert "code_reasoning" in system_prompt
        assert "rag" in system_prompt
        assert "data_analyst→data_analysis" in system_prompt
        await scope.close()
