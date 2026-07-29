from __future__ import annotations

import pytest

from core.runtime import (
    AgentState,
    ApplicationRuntimeServices,
    RuntimeInitializationError,
    RuntimeInitializationStack,
    RuntimeLifecycleState,
)
from tests._runtime_assembly_fixtures import make_services


class RecordingResource:
    def __init__(self, name: str, calls: list[str], *, fail: bool = False) -> None:
        self.name = name
        self.calls = calls
        self.fail = fail

    def close(self) -> None:
        self.calls.append(self.name)
        if self.fail:
            raise RuntimeError("unsafe raw exception")


@pytest.mark.asyncio
async def test_close_is_idempotent_and_continues_after_component_failure() -> None:
    calls: list[str] = []
    first = RecordingResource("first", calls, fail=True)
    second = RecordingResource("second", calls)
    base = make_services()
    services = ApplicationRuntimeServices(
        event_journal=base.event_journal,
        observability_dispatcher=base.observability_dispatcher,
        structured_logger=base.structured_logger,
        runtime_metrics_recorder=base.runtime_metrics_recorder,
        span_recorder=base.span_recorder,
        snapshot_store=base.snapshot_store,
        recovery_validator=base.recovery_validator,
        model_invocation_router=base.model_invocation_router,
        tool_execution_service=base.tool_execution_service,
        retrieval_execution_service=base.retrieval_execution_service,
        blocking_executors=(),
        worker_trackers=(),
        run_registry=base.run_registry,
        extra_closeables=(("first_resource", first), ("second_resource", second)),
    )

    report = await services.close(1)
    repeated = await services.close(1)

    assert calls == ["first", "second"]
    assert report is repeated
    assert report.completed is False
    assert report.error_codes == ("RUNTIME_COMPONENT_CLOSE_FAILED",)
    assert services.lifecycle_state is RuntimeLifecycleState.CLOSED


def test_repr_is_safe_and_container_rejects_per_run_state() -> None:
    services = make_services()
    rendered = repr(services)
    assert "application_runtime_services" in rendered
    assert ".db" not in rendered
    assert "object at" not in rendered

    state = AgentState.for_run_context("run-1")
    values = {
        field: getattr(services, field)
        for field in (
            "event_journal",
            "observability_dispatcher",
            "structured_logger",
            "runtime_metrics_recorder",
            "span_recorder",
            "snapshot_store",
            "recovery_validator",
            "model_invocation_router",
            "tool_execution_service",
            "retrieval_execution_service",
            "blocking_executors",
            "worker_trackers",
            "run_registry",
        )
    }
    values["model_invocation_router"] = state
    with pytest.raises(ValueError, match="per-run"):
        ApplicationRuntimeServices(**values)


@pytest.mark.asyncio
async def test_initialization_failure_cleanup_is_reverse_and_best_effort() -> None:
    calls: list[str] = []
    stack = RuntimeInitializationStack()
    stack.track("first", RecordingResource("first", calls))
    stack.track("second", RecordingResource("second", calls, fail=True))
    stack.track("third", RecordingResource("third", calls))

    report = await stack.close(1)

    assert calls == ["third", "second", "first"]
    assert report.completed is False
    assert report.error_codes == ("RUNTIME_INITIALIZATION_CLEANUP_FAILED",)


@pytest.mark.asyncio
async def test_initialization_failure_is_projected_without_raw_exception() -> None:
    calls: list[str] = []
    stack = RuntimeInitializationStack()
    stack.track("opened", RecordingResource("opened", calls))

    with pytest.raises(RuntimeInitializationError) as captured:
        await stack.create(
            "snapshot_store",
            lambda: (_ for _ in ()).throw(
                RuntimeError("C:/secret/runtime_snapshots.db")
            ),
        )

    assert calls == ["opened"]
    assert captured.value.error_code == "RUNTIME_INITIALIZATION_FAILED"
    assert "secret" not in str(captured.value)
