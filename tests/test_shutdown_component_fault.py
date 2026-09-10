from __future__ import annotations

import asyncio

import pytest

from core.runtime import FaultPoint, GracefulShutdownCoordinator
from tests._runtime_assembly_fixtures import FakeDispatcher, make_services
from tests._shutdown_fault_fixtures import (
    RecordingResource,
    shutdown_controller,
    shutdown_rule,
)


def services_for_components(*, journal, snapshot=None, extra=()):
    services = make_services(
        journal=journal,
        snapshot_store=snapshot,
        snapshot_enabled=snapshot is not None,
    )
    return services.__class__(
        **{
            **{
                field: getattr(services, field)
                for field in services.__dataclass_fields__
                if field != "_lifecycle" and field != "extra_closeables"
            },
            "extra_closeables": tuple(extra),
        }
    )


@pytest.mark.asyncio
async def test_component_failure_does_not_skip_later_component():
    calls: list[str] = []
    failing = RecordingResource("failing", calls, fail_close=True)
    later = RecordingResource("later", calls)
    report = await GracefulShutdownCoordinator(
        services_for_components(
            journal=RecordingResource("journal", calls),
            extra=(("remaining_store", failing), ("http_client", later)),
        ),
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown()

    assert failing.close_calls == later.close_calls == 1
    assert "RUNTIME_COMPONENT_CLOSE_FAILED" in report.error_codes
    assert "provider-secret-error" not in repr(report)


@pytest.mark.asyncio
async def test_component_close_timeout_is_bounded_and_later_component_runs():
    calls: list[str] = []

    class SlowResource:
        async def close(self):
            calls.append("slow.close")
            await asyncio.sleep(1)

    later = RecordingResource("later", calls)
    report = await GracefulShutdownCoordinator(
        services_for_components(
            journal=RecordingResource("journal", calls),
            extra=(("remaining_store", SlowResource()), ("http_client", later)),
        ),
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.02,
    ).shutdown()

    assert "RUNTIME_COMPONENT_CLOSE_TIMEOUT" in report.error_codes
    assert later.close_calls == 1


@pytest.mark.asyncio
async def test_specific_component_fault_does_not_skip_journal_or_model():
    calls: list[str] = []
    snapshot = RecordingResource("snapshot", calls)
    journal = RecordingResource("journal", calls)
    model = RecordingResource("model", calls)
    controller = shutdown_controller(
        shutdown_rule(
            FaultPoint.SHUTDOWN_COMPONENT_CLOSE,
            shutdown_component="snapshot_store",
        )
    )
    report = await GracefulShutdownCoordinator(
        services_for_components(
            journal=journal,
            snapshot=snapshot,
            extra=(("model_engine_0", model),),
        ),
        shutdown_grace_seconds=0,
        component_timeout_seconds=0.1,
    ).shutdown(controller)

    assert snapshot.close_calls == 0
    assert journal.close_calls == model.close_calls == 1
    assert "RUNTIME_COMPONENT_CLOSE_INJECTED_FAILURE" in report.error_codes
