"""WP4-B RunCoordinator retrieval hook 单元测试。

覆盖：hook 位置语义（run scope 内、每 Run 一次 retrieval）、failure
BEST_EFFORT_EMPTY_BUNDLE_NO_STALE_FALLBACK、cancellation/deadline 传播不被
best-effort 吞掉、safe event 只含 allowlist 事实。
全部为 DETERMINISTIC IMPLEMENTATION TEST。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from core.advanced_memory import AdvancedMemoryStore
from core.memory_manager import MemoryManager
from core.runtime import (
    AgentState,
    AgentStateMachine,
    BudgetLedger,
    ParallelExecutionPolicy,
    ParallelExecutor,
    ParallelFailureMode,
    RunBudget,
    RunCoordinator,
    RunHandle,
    RunRegistry,
    SerialScheduler,
    create_run_context,
)
from core.runtime.cancellation import RunCancelledError
from core.runtime.memory_retrieval import (
    MEMORY_DIRECT_SCOPE,
    MemoryContextBundle,
    MemoryRetrievalError,
    MemoryRetrievalErrorCode,
    MemoryRetrievalService,
)
from core.runtime.multi_agent_planning import (
    PlanResolver,
    PlanningRequest,
)
from core.runtime.plan_compiler import PlanCompiler
from core.runtime.agent_registry import DEFAULT_AGENT_REGISTRY
from tests.test_memory_retrieval_service import make_record


class StubRetrievalService(MemoryRetrievalService):
    """计数 + 可编排 outcome 的测试 stub；仍是 MemoryRetrievalService。"""

    def __init__(self, store, outcomes) -> None:
        super().__init__(store)
        self.outcomes = list(outcomes)
        self.calls = 0

    def retrieve(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_store(tmp_path) -> tuple[AdvancedMemoryStore, MemoryContextBundle]:
    db_path = str(tmp_path / "memory.db")
    MemoryManager(db_path=db_path)
    store = AdvancedMemoryStore(db_path)
    store.create(make_record("mem-pg", canonical_text="项目数据库使用 PostgreSQL", value="PostgreSQL", key="project.database"))
    service = MemoryRetrievalService(store)
    bundle = service.retrieve(
        agent_id="core_router",
        memory_scope=MEMORY_DIRECT_SCOPE,
        query="我们项目用什么数据库？",
    )
    return store, bundle


def make_dynamic_coordinator(service) -> RunCoordinator:
    context, source = create_run_context(entry_agent_id="core_router")
    ledger = BudgetLedger(
        RunBudget(), deadline_remaining=context.remaining_seconds
    )
    context.attach_budget_ledger(ledger)
    state = AgentState.for_run_context(context.run_id)
    machine = AgentStateMachine()
    handle = RunHandle(context.run_id, source, state, "run_coordinator")
    return RunCoordinator.for_dynamic_resolver(
        run_context=context,
        plan_resolver=PlanResolver(
            DEFAULT_AGENT_REGISTRY, PlanCompiler(DEFAULT_AGENT_REGISTRY), None
        ),
        planning_request=PlanningRequest("core_router", "我们项目用什么数据库？"),
        execution_factory=lambda: (
            SerialScheduler(machine),
            ParallelExecutor(machine, max_concurrency=1),
        ),
        agent_state=state,
        budget_ledger=ledger,
        run_handle=handle,
        run_registry=RunRegistry(),
        policy=ParallelExecutionPolicy(2, ParallelFailureMode.BEST_EFFORT),
        state_machine=machine,
        memory_retrieval_service=service,
    )


def test_retrieval_hook_is_single_shot_and_attaches_bundle(tmp_path) -> None:
    store, bundle = make_store(tmp_path)
    service = StubRetrievalService(store, [bundle])
    coordinator = make_dynamic_coordinator(service)

    coordinator._retrieve_memory_context()
    coordinator._retrieve_memory_context()  # 第二次调用不得再次查询。

    assert service.calls == 1
    assert coordinator.memory_context_bundle is bundle
    observation = coordinator._memory_retrieval_observation
    assert observation is not None
    assert observation.status == "SUCCEEDED"
    assert observation.safe_error_code is None
    assert observation.context_record_count == 0


def test_store_failure_yields_empty_bundle_and_run_continues(tmp_path) -> None:
    store, _ = make_store(tmp_path)
    service = StubRetrievalService(
        store,
        [
            MemoryRetrievalError(
                MemoryRetrievalErrorCode.UNAVAILABLE, "authority read failed"
            )
        ],
    )
    coordinator = make_dynamic_coordinator(service)

    coordinator._retrieve_memory_context()  # 不抛异常 → Run 继续。

    assert service.calls == 1
    bundle = coordinator.memory_context_bundle
    assert bundle is not None
    assert bundle.record_count == 0
    observation = coordinator._memory_retrieval_observation
    assert observation.status == "FAILED"
    assert observation.safe_error_code == MemoryRetrievalErrorCode.UNAVAILABLE
    assert observation.context_record_count == 0


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed_by_best_effort(tmp_path) -> None:
    store, _ = make_store(tmp_path)
    service = StubRetrievalService(store, [RunCancelledError("USER_CANCELLED")])
    coordinator = make_dynamic_coordinator(service)

    with pytest.raises(RunCancelledError):
        coordinator._retrieve_memory_context()
    # cancellation 是 terminal runtime signal：不产出 stale/empty fallback bundle。
    assert coordinator.memory_context_bundle is None


@pytest.mark.asyncio
async def test_retrieval_event_contains_only_safe_allowlist_facts(tmp_path) -> None:
    store, bundle = make_store(tmp_path)
    service = StubRetrievalService(store, [bundle])
    coordinator = make_dynamic_coordinator(service)
    captured = {}

    class StubEmitter:
        async def emit(self, event_type, payload, **kwargs):
            captured["event_type"] = event_type
            captured["payload"] = payload
            captured["kwargs"] = kwargs

    coordinator.event_emitter = StubEmitter()
    coordinator._retrieve_memory_context()
    from core.runtime.events import RuntimeEventType

    await coordinator._emit_memory_retrieval_observation()

    assert captured["event_type"] is RuntimeEventType.MEMORY_RETRIEVAL_COMPLETED
    assert captured["kwargs"]["component"] == "run_coordinator"
    payload = captured["payload"]
    safe = json.dumps(
        payload, ensure_ascii=False, default=lambda value: str(value)
    )
    assert payload.status == "SUCCEEDED"
    assert payload.selected_count == 1
    assert payload.planning_injected is False
    # 隐私边界：无 query、无 canonical text、无 logical_key、无 memory_id。
    assert "我们项目用什么数据库" not in safe
    assert "项目数据库使用 PostgreSQL" not in safe
    assert "project.database" not in safe
    assert "mem-pg" not in safe
