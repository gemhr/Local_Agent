"""仅保留仍有效的纯合同/本地测试与共享 fixture；废弃 agent-only 节点见 .ai/archive/tests/scoped_memory_lifecycle/。"""

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
    ActiveRunControlHandle,
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
from core.runtime.memory_authorization import MemoryAccessPrincipal
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
        requester=MemoryAccessPrincipal("core_router"),
        target_owner_agent_id="core_router",
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
    handle = ActiveRunControlHandle(
        run_id=context.run_id,
        runtime_mode="COORDINATED",
        cancellation_source=source,
        owner="run_coordinator",
        active_step_count=lambda: len(state.active_step_ids),
    )
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
