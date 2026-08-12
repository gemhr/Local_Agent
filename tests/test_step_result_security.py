"""WP3 data security: specialist raw results stay out of every persistent and
observable channel except Store memory, the dependency view, the synthesis
model input and the synthesis adapter call stack."""

from __future__ import annotations

import inspect

import pytest

from core.agent_router import AgentRouter
from core.runtime import (
    ApplicationRuntimeServices,
    CoordinatedRuntimeFactory,
    InMemoryRunEventJournal,
    InMemorySpanRecorder,
    InMemorySnapshotStore,
    InMemoryStructuredRuntimeLogger,
    NoopMetricsRecorder,
    RecoveryValidator,
    RunStatus,
    RunRegistry,
    StopReason,
    ContextSourceType,
    ContextTrustLevel,
)
from core.runtime.events import RuntimeEventType
from tests._wp3_fixtures import (
    Wp3RecordingRouter,
    delegated_json,
    make_wp3_services,
)
from tests._runtime_assembly_fixtures import FakeDispatcher, process_blocking_executor

SECRET = "SECRET_SPECIALIST_RESULT_DO_NOT_PERSIST"
SECRET_PATH = r"\\internal\private\file.dat"


def make_security_services():
    journal = InMemoryRunEventJournal()
    snapshot_store = InMemorySnapshotStore()
    dispatcher = FakeDispatcher()
    span_recorder = InMemorySpanRecorder()
    structured_logger = InMemoryStructuredRuntimeLogger()
    run_registry = RunRegistry()
    return ApplicationRuntimeServices(
        event_journal=journal,
        observability_dispatcher=dispatcher,
        structured_logger=structured_logger,
        runtime_metrics_recorder=NoopMetricsRecorder(),
        span_recorder=span_recorder,
        snapshot_store=snapshot_store,
        recovery_validator=RecoveryValidator(
            snapshot_store=snapshot_store,
            journal=journal,
        ),
        model_invocation_router=object(),
        tool_execution_service=object(),
        retrieval_execution_service=object(),
        blocking_executors=(),
        worker_trackers=(),
        run_registry=run_registry,
        coordinated_step_executor=process_blocking_executor,
        snapshot_enabled=True,
        recovery_enabled=True,
    )


def render_all_channels(services, run_id: str) -> str:
    rendered: list[str] = []
    for record in services.event_journal.read_after(run_id, 0, 1000):
        rendered.append(repr(record))
        rendered.append(repr(record.safe_payload))
    for snapshot in services.snapshot_store.list_for_run(run_id, 10):
        rendered.append(repr(snapshot))
    for log in services.structured_logger.records:
        rendered.append(repr(log))
    for span in services.span_recorder.snapshot():
        rendered.append(repr(span))
        rendered.append(repr(dict(span.attributes)))
    return "\n".join(rendered)


@pytest.mark.asyncio
async def test_specialist_secrets_never_reach_observable_channels() -> None:
    services = make_security_services()
    router = Wp3RecordingRouter(
        delegated_json(task_ids=("code", "data"), synthesis_required=True),
        output_for={
            "code_expert": f"{SECRET} {SECRET_PATH}",
            "data_analyst": SECRET_PATH,
            "synthesis_agent": f"FINAL-{SECRET}",
        },
    )
    scope = await CoordinatedRuntimeFactory(router, services).create_run_scope(
        "core_router", "coordinate two reviews"
    )
    result = await scope.execute()
    assert result.status is RunStatus.SUCCEEDED
    assert result.error_code is None

    # Synthesis model input may contain the raw specialist results.
    synthesis_items = router.context_item_calls[0]
    synthesis_messages = router.context_message_calls[0]
    step_results = [
        item for item in synthesis_items
        if item.source_type is ContextSourceType.STEP_RESULT
    ]
    assert len(step_results) == 2
    assert all(
        item.trust_level is ContextTrustLevel.USER_CONTENT for item in step_results
    )
    synthesis_system = "\n".join(
        message["content"]
        for message in synthesis_messages
        if message["role"] == "system"
    )
    synthesis_users = "\n".join(
        message["content"]
        for message in synthesis_messages
        if message["role"] == "user"
    )
    assert SECRET not in synthesis_system
    assert SECRET_PATH not in synthesis_system
    assert SECRET in synthesis_users
    assert SECRET_PATH in synthesis_users

    rendered = render_all_channels(services, scope.run_id)
    assert SECRET not in rendered
    assert SECRET_PATH not in rendered
    assert SECRET not in repr(result)
    assert SECRET_PATH not in repr(result)
    assert SECRET not in repr(scope.coordinator.step_result_store)
    assert all(flag is False for flag in router.persist_flags())

    # WP4: the unique final candidate is delivered exactly once through
    # OUTPUT_DELTA; raw text stays out of Journal/Trace/Snapshot/logs and only
    # reaches the delivered Memory scope.
    records = [
        record.event_type
        for record in services.event_journal.read_after(scope.run_id, 0, 1000)
    ]
    assert records.count(RuntimeEventType.OUTPUT_DELTA) == 1
    output_event = next(
        record
        for record in services.event_journal.read_after(scope.run_id, 0, 1000)
        if record.event_type is RuntimeEventType.OUTPUT_DELTA
    )
    assert output_event.safe_payload["text_digest"] == (
        __import__("hashlib").sha256(f"FINAL-{SECRET}".encode("utf-8")).hexdigest()
    )
    assert SECRET not in repr(output_event.safe_payload)
    # The delivered final reaches the entry agent's direct Memory scope once.
    written = router.memory_manager.messages
    assert any(SECRET in message["content"] for message in written)
    assert SECRET_PATH not in "".join(
        message["content"] for message in written
    )
    assert [message["role"] for message in written] == ["user", "assistant"]
    await scope.close()


def test_non_persist_router_contract_guards_memory_writes() -> None:
    source = inspect.getsource(AgentRouter._run_agent_once)
    # Every memory write in the unified single-Agent path must be guarded by
    # persist, so persist=False specialist/synthesis calls never write Memory.
    assert "if persist:" in source
    assert "memory_manager.add_message" in source


@pytest.mark.asyncio
async def test_batch_report_and_exceptions_stay_clean() -> None:
    services = make_wp3_services()
    router = Wp3RecordingRouter(
        delegated_json(task_ids=("code", "data"), synthesis_required=True),
        output_for={
            "code_expert": SECRET,
            "data_analyst": SECRET,
            "synthesis_agent": SECRET,
        },
        fail_agents=(),
    )
    scope = await CoordinatedRuntimeFactory(router, services).create_run_scope(
        "core_router", "coordinate two reviews"
    )
    result = await scope.execute()
    assert result.status is RunStatus.SUCCEEDED
    rendered = "\n".join(
        [
            repr(result),
            str(result),
            repr(scope.coordinator.step_result_store),
        ]
    )
    assert SECRET not in rendered
    await scope.close()
