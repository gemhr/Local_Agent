from __future__ import annotations

import json

from core.runtime import (
    ApplicationRuntimeServices,
    CoordinatedRuntimeFactory,
    InMemoryRunEventJournal,
    InMemorySnapshotStore,
    NoopMetricsRecorder,
    NoopSpanRecorder,
    NoopStructuredRuntimeLogger,
    RecoveryValidator,
    RunRegistry,
    TaskCapabilityRequirements,
    create_single_step_plan,
    process_run_registry,
    process_blocking_executor,
)
from core.chat_service import ChatService


class FakeGaugeProvider:
    def __init__(self) -> None:
        self.channels: set[object] = set()

    def register_channel(self, channel) -> None:
        self.channels.add(channel)

    def unregister_channel(self, channel) -> None:
        self.channels.discard(channel)


class FakeDispatcher:
    def __init__(self) -> None:
        self.gauge_provider = FakeGaugeProvider()
        self.records: list[object] = []
        self.closed = 0
        self.flushed = 0

    def try_submit(self, record) -> bool:
        self.records.append(record)
        return True

    async def flush(self, timeout: float) -> bool:
        self.flushed += 1
        return True

    async def close(self, timeout: float) -> bool:
        self.closed += 1
        return True


class FakeRouter:
    def __init__(self) -> None:
        self.model_invocation_router = object()
        self.tool_execution_service = object()
        self.retrieval_execution_service = object()

    def build_single_agent_plan(self, agent_id: str, query: str):
        return create_single_step_plan(agent_id, TaskCapabilityRequirements())

    def complete_single_agent(self, agent_id: str, query: str, **kwargs) -> str:
        return "assembled-output"

    def complete_planning_decision(self, user_request: str, **kwargs) -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "decision": "DIRECT_ANSWER",
                "agent_id": "core_router",
                "reason_code": "MODEL_DIRECT",
            }
        )


def make_services(
    *,
    journal=None,
    snapshot_store=None,
    dispatcher=None,
    span_recorder=None,
    runtime_metrics_recorder=None,
    run_registry=None,
    snapshot_enabled: bool = True,
) -> ApplicationRuntimeServices:
    active_journal = journal or InMemoryRunEventJournal()
    active_snapshot = (
        snapshot_store or InMemorySnapshotStore()
        if snapshot_enabled
        else None
    )
    active_dispatcher = dispatcher or FakeDispatcher()
    router = FakeRouter()
    return ApplicationRuntimeServices(
        event_journal=active_journal,
        observability_dispatcher=active_dispatcher,
        structured_logger=NoopStructuredRuntimeLogger(),
        runtime_metrics_recorder=(
            runtime_metrics_recorder or NoopMetricsRecorder()
        ),
        span_recorder=span_recorder or NoopSpanRecorder(),
        snapshot_store=active_snapshot,
        recovery_validator=(
            RecoveryValidator(
                snapshot_store=active_snapshot,
                journal=active_journal,
            )
            if active_snapshot is not None
            else None
        ),
        model_invocation_router=router.model_invocation_router,
        tool_execution_service=router.tool_execution_service,
        retrieval_execution_service=router.retrieval_execution_service,
        blocking_executors=(),
        worker_trackers=(),
        run_registry=run_registry or RunRegistry(),
        coordinated_step_executor=process_blocking_executor,
        snapshot_enabled=snapshot_enabled,
        recovery_enabled=snapshot_enabled,
    )


def make_coordinated_chat_service(
    router,
    *,
    state_observer=None,
    event_channel_capacity: int = 32,
    run_registry=None,
) -> ChatService:
    """Explicit test-only assembly for the factory-required production path."""
    active_registry = run_registry or process_run_registry
    services = make_services(
        run_registry=active_registry,
        snapshot_enabled=False,
    )
    factory = CoordinatedRuntimeFactory(
        router,
        services,
        event_channel_capacity=event_channel_capacity,
    )
    return ChatService(
        router,
        state_observer=state_observer,
        event_channel_capacity=event_channel_capacity,
        event_journal=services.event_journal,
        observability_dispatcher=services.observability_dispatcher,
        gauge_provider=services.observability_dispatcher.gauge_provider,
        coordinated_runtime_factory=factory,
        run_registry=active_registry,
    )
