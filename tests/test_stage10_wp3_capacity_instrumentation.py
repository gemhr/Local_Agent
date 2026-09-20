from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import server

from core.observability import ObservabilityService
from core.runtime import CancellationSource, ToolConcurrencyController
from core.runtime.client_event_feed import ClientDeliveryEvent
from core.settings import Settings
from scripts.capacity.run_stage9_runtime_capacity import (
    ARTIFACT_SCHEMA,
    _soak_windows,
    _tool_controller_unit_point,
    _tool_query,
    _write_artifact,
)


def _service() -> ObservabilityService:
    return ObservabilityService(replace(Settings.load(), metrics_enabled=True), process_role="api")


def test_wp3_metrics_are_registered_once_and_have_no_unbounded_labels() -> None:
    service = _service()
    names = [family.name for family in service.registry.collect()]
    assert len(names) == len(set(names))
    body = service.render_metrics().body.decode()
    assert "localagent_runtime_run_admission_wait_seconds" in body
    assert "localagent_runtime_db_checkout_wait_seconds" in body
    assert "localagent_runtime_tool_permit_wait_seconds" in body
    assert "localagent_runtime_sse_active_connections" in body
    assert "run_id" not in body


@pytest.mark.asyncio
async def test_tool_permit_wait_is_observed_and_active_returns_to_zero() -> None:
    service = _service()
    controller = ToolConcurrencyController(max_concurrency=1, metrics=service)
    source = CancellationSource()
    first = await controller.acquire(
        tool_name="deterministic", tool_max_concurrency=1, resource_key=None,
        cancellation_token=source.token, remaining_seconds=lambda: 1.0,
    )
    second_task = asyncio.create_task(controller.acquire(
        tool_name="deterministic", tool_max_concurrency=1, resource_key=None,
        cancellation_token=CancellationSource().token, remaining_seconds=lambda: 1.0,
    ))
    await asyncio.sleep(0.02)
    assert not second_task.done()
    assert controller.active_permit_count == 1
    body = service.render_metrics().body.decode()
    assert "localagent_runtime_tool_active 1.0" in body
    first.release()
    second = await asyncio.wait_for(second_task, 1)
    second.release()
    body = service.render_metrics().body.decode()
    assert "localagent_runtime_tool_permit_wait_seconds_count" in body
    assert "localagent_runtime_tool_active 0.0" in body


@pytest.mark.asyncio
async def test_metric_callback_failure_does_not_break_permit_lifecycle() -> None:
    class BrokenMetrics:
        def observe_tool_permit_wait(self, _duration: float) -> None:
            raise RuntimeError("wait callback failed")

        def set_tool_active(self, _value: int) -> None:
            raise RuntimeError("active callback failed")

    controller = ToolConcurrencyController(max_concurrency=1, metrics=BrokenMetrics())
    first = await controller.acquire(
        tool_name="deterministic", tool_max_concurrency=1, resource_key=None,
        cancellation_token=CancellationSource().token, remaining_seconds=lambda: 1.0,
    )
    second_task = asyncio.create_task(controller.acquire(
        tool_name="deterministic", tool_max_concurrency=1, resource_key=None,
        cancellation_token=CancellationSource().token, remaining_seconds=lambda: 1.0,
    ))
    await asyncio.sleep(0.02)
    first.release()
    second = await asyncio.wait_for(second_task, 1)
    second.release()
    assert controller.active_permit_count == 0


def test_sse_gauge_returns_to_baseline() -> None:
    service = _service()
    service.observe_sse_open()
    service.observe_sse_poll(0.001)
    service.observe_sse_events(2)
    service.observe_sse_close()
    body = service.render_metrics().body.decode()
    assert "localagent_runtime_sse_active_connections 0.0" in body
    assert "localagent_runtime_sse_poll_total 1.0" in body


def test_wp3_artifact_envelope_is_machine_readable(tmp_path: Path) -> None:
    _write_artifact(
        tmp_path,
        "sample.json",
        {
            "scenario": "unit",
            "concurrency": 2,
            "attempted": 3,
            "duration_seconds": 0.25,
            "succeeded": 3,
            "failed": 0,
            "raw": [{"latency_ms": 1.0}],
        },
        {"python": "3.12", "database": "test"},
    )
    payload = json.loads((tmp_path / "sample.json").read_text(encoding="utf-8"))
    assert payload["schema"] == ARTIFACT_SCHEMA
    assert payload["timestamp"]
    assert payload["env"]["database"] == "test"
    for key in ("config", "sample", "concurrency", "duration", "raw", "summary", "failures"):
        assert key in payload
    assert payload["sample"] == 3
    assert payload["raw"] == [{"latency_ms": 1.0}]


def test_soak_windows_cover_duration_without_overlap() -> None:
    windows = _soak_windows(12.0)
    assert windows["first"] == (0.0, 4.0)
    assert windows["middle"] == (4.0, 8.0)
    assert windows["last"] == (8.0, 12.0)


@pytest.mark.asyncio
async def test_tool_capacity_harness_uses_production_controller_and_releases_permits() -> None:
    result = await _tool_controller_unit_point(
        limit=2,
        load_concurrency=3,
        samples=3,
        delay_seconds=0.001,
        timeout_seconds=1.0,
    )
    assert result["execution_path"]["controller"] == "production ToolConcurrencyController"
    assert result["active_peak"] <= 2
    assert result["correctness"]["active_returned_to_zero"] is True
    assert result["metrics"]["permit_wait_samples"] == 3
    assert len(result["raw"]) == 3


def test_real_tool_query_uses_explicit_production_tool_contract() -> None:
    query = _tool_query(resource_key="capacity-resource-1", item_count=3)
    assert query.startswith("请调用 complex_workflow_simulator(")
    assert '"execution_mode":"DRY_RUN"' in query
    assert '"resource_key":"capacity-resource-1"' in query
    assert query.count('"action":"ADD"') == 3
    assert '"processing_delay_ms":250' in query


@pytest.mark.asyncio
async def test_run_admission_metric_callback_failure_does_not_leak_slot() -> None:
    class BrokenMetrics:
        def set_run_admission_waiting(self, _value: int) -> None:
            raise RuntimeError("waiting callback failed")

        def observe_run_admission_wait(self, _duration: float) -> None:
            raise RuntimeError("wait callback failed")

    supervisor = server.RunExecutionSupervisor(
        max_active_runs=1, metrics=BrokenMetrics()
    )
    first = await supervisor.acquire_slot()
    blocked = asyncio.create_task(supervisor.acquire_slot())
    await asyncio.sleep(0)
    assert not blocked.done()
    supervisor.release_slot(first)
    second = await asyncio.wait_for(blocked, 1)
    supervisor.release_slot(second)
    third = await asyncio.wait_for(supervisor.acquire_slot(), 1)
    supervisor.release_slot(third)


@pytest.mark.asyncio
async def test_sse_metric_callback_failure_does_not_break_terminal_event(monkeypatch) -> None:
    class BrokenMetrics:
        def observe_sse_open(self) -> None:
            raise RuntimeError("open callback failed")

        def observe_sse_poll(self, _duration: float) -> None:
            raise RuntimeError("poll callback failed")

        def observe_sse_events(self, _count: int) -> None:
            raise RuntimeError("events callback failed")

        def observe_sse_close(self) -> None:
            raise RuntimeError("close callback failed")

    class Feed:
        async def read_after(self, _run_id: str, _cursor: int):
            return (
                ClientDeliveryEvent(
                    run_id="run-sse-callback-failure",
                    cursor=1,
                    event_type="run.completed",
                    payload={"status": "succeeded"},
                    created_at=datetime.now(UTC),
                ),
            )

    async def allow(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(server, "_authorize_run", allow)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                client_event_feed=Feed(),
                observability_service=BrokenMetrics(),
            )
        ),
        headers={},
    )
    response = await server.v1_run_events_endpoint(
        "run-sse-callback-failure", request, after_cursor=0
    )
    frames = [frame async for frame in response.body_iterator]
    assert len(frames) == 1
    assert "event: run.completed" in frames[0]
    assert '"status":"succeeded"' in frames[0]


@pytest.mark.asyncio
async def test_producer_exception_is_retrieved_and_slot_is_released(monkeypatch) -> None:
    async def broken_stream():
        raise RuntimeError("sensitive producer detail")
        yield None

    calls = []

    class Logger:
        def warning(self, message: str, *, extra: dict[str, object]) -> None:
            calls.append((message, extra))

    monkeypatch.setattr(server, "logger", Logger())
    supervisor = server.RunExecutionSupervisor(max_active_runs=1)
    reserved = await supervisor.acquire_slot()
    supervisor.spawn(
        "run-producer-failure", broken_stream(), reserved_slot=reserved
    )
    replacement = await asyncio.wait_for(supervisor.acquire_slot(), 1)
    supervisor.release_slot(replacement)
    await supervisor.close()
    await asyncio.sleep(0)
    assert calls == [
        (
            "Run producer task failed",
            {
                "component": "run_execution_supervisor",
                "phase": "producer",
                "run_id": "run-producer-failure",
                "error_type": "RuntimeError",
            },
        )
    ]
