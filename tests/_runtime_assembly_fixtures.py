from __future__ import annotations

from core.runtime import (
    ApplicationRuntimeServices,
    InMemoryRunEventJournal,
    InMemorySnapshotStore,
    NoopMetricsRecorder,
    NoopSpanRecorder,
    NoopStructuredRuntimeLogger,
    RecoveryValidator,
    RunRegistry,
    TaskCapabilityRequirements,
    create_single_step_plan,
)


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


def make_services(
    *,
    journal=None,
    snapshot_store=None,
    dispatcher=None,
    span_recorder=None,
    run_registry=None,
) -> ApplicationRuntimeServices:
    active_journal = journal or InMemoryRunEventJournal()
    active_snapshot = snapshot_store or InMemorySnapshotStore()
    active_dispatcher = dispatcher or FakeDispatcher()
    router = FakeRouter()
    return ApplicationRuntimeServices(
        event_journal=active_journal,
        observability_dispatcher=active_dispatcher,
        structured_logger=NoopStructuredRuntimeLogger(),
        runtime_metrics_recorder=NoopMetricsRecorder(),
        span_recorder=span_recorder or NoopSpanRecorder(),
        snapshot_store=active_snapshot,
        recovery_validator=RecoveryValidator(
            snapshot_store=active_snapshot,
            journal=active_journal,
        ),
        model_invocation_router=router.model_invocation_router,
        tool_execution_service=router.tool_execution_service,
        retrieval_execution_service=router.retrieval_execution_service,
        blocking_executors=(),
        worker_trackers=(),
        run_registry=run_registry or RunRegistry(),
        snapshot_enabled=True,
    )
