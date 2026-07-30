from __future__ import annotations

import pytest

from core.runtime import ApplicationRuntimeServices, GracefulShutdownCoordinator
from core.runtime.run_registry import RunRegistry


class _Resource:
    def __init__(self, name: str, calls: list[str], *, flush=False, fail=False):
        self.name = name
        self.calls = calls
        self.can_flush = flush
        self.fail = fail

    async def flush(self, timeout=1):
        if self.can_flush:
            self.calls.append(f"{self.name}.flush")
        if self.fail:
            raise RuntimeError("raw secret")
        return True

    def close(self):
        self.calls.append(f"{self.name}.close")
        if self.fail:
            raise RuntimeError("raw secret")


class _Executor:
    def __init__(self, calls, *, idle=True):
        self.calls = calls
        self.idle = idle

    def close_admission(self):
        self.calls.append("executor.admission")

    def wait_until_idle(self, timeout):
        self.calls.append("executor.drain")
        return self.idle

    def shutdown(self, *, wait=True, timeout=1):
        self.calls.append("executor.close")
        return True


@pytest.mark.asyncio
async def test_shutdown_order_keeps_journal_after_run_drain_and_continues_failures():
    calls: list[str] = []
    observability = _Resource("observability", calls, flush=True)
    span = _Resource("span", calls, flush=True)
    snapshot = _Resource("snapshot", calls)
    journal = _Resource("journal", calls)
    model = _Resource("model", calls, fail=True)
    remaining = _Resource("remaining", calls)
    executor = _Executor(calls)
    services = ApplicationRuntimeServices(
        event_journal=journal,
        observability_dispatcher=observability,
        structured_logger=object(),
        runtime_metrics_recorder=object(),
        span_recorder=span,
        snapshot_store=snapshot,
        recovery_validator=object(),
        model_invocation_router=object(),
        tool_execution_service=object(),
        retrieval_execution_service=object(),
        blocking_executors=(executor,),
        worker_trackers=(),
        run_registry=RunRegistry(),
        snapshot_enabled=True,
        recovery_enabled=True,
        extra_closeables=(
            ("model_engine_0", model),
            ("remaining_store", remaining),
        ),
    )
    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0.1,
        component_timeout_seconds=0.2,
    ).shutdown()

    assert calls.index("executor.admission") < calls.index("executor.drain")
    assert calls.index("observability.flush") < calls.index(
        "observability.close"
    )
    assert calls.index("span.flush") < calls.index("span.close")
    assert calls.index("snapshot.close") < calls.index("journal.close")
    assert calls.index("journal.close") < calls.index("model.close")
    assert calls.index("model.close") < calls.index("remaining.close")
    assert calls.index("remaining.close") < calls.index("executor.close")
    assert "RUNTIME_COMPONENT_CLOSE_FAILED" in report.error_codes


@pytest.mark.asyncio
async def test_active_worker_defers_model_close_without_skipping_other_resources():
    calls: list[str] = []
    model = _Resource("model", calls)
    remaining = _Resource("remaining", calls)
    executor = _Executor(calls, idle=False)
    services = ApplicationRuntimeServices(
        event_journal=_Resource("journal", calls),
        observability_dispatcher=_Resource("observability", calls),
        structured_logger=object(),
        runtime_metrics_recorder=object(),
        span_recorder=_Resource("span", calls),
        snapshot_store=None,
        recovery_validator=None,
        model_invocation_router=object(),
        tool_execution_service=object(),
        retrieval_execution_service=object(),
        blocking_executors=(executor,),
        worker_trackers=(),
        run_registry=RunRegistry(),
        snapshot_enabled=False,
        recovery_enabled=False,
        extra_closeables=(
            ("model_engine_0", model),
            ("remaining_store", remaining),
        ),
    )

    report = await GracefulShutdownCoordinator(
        services,
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.05,
    ).shutdown()

    assert "model.close" not in calls
    assert "remaining.close" in calls
    assert "executor.close" in calls
    assert (
        "RUNTIME_MODEL_CLOSE_DEFERRED_ACTIVE_WORKER"
        in report.error_codes
    )
