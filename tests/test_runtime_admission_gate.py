from __future__ import annotations

import math

import pytest

from core.runtime import (
    CoordinatedRuntimeFactory,
    RuntimeAdmissionGate,
    RuntimeAdmissionRejectedError,
    RuntimeAdmissionState,
)
from tests._runtime_assembly_fixtures import FakeRouter, make_services


def test_admission_state_machine_is_idempotent_and_rejects_new_leases():
    gate = RuntimeAdmissionGate()
    assert gate.state is RuntimeAdmissionState.ACCEPTING

    gate.acquire()
    assert gate.pending_admissions == 1
    assert gate.close_admission() is True
    assert gate.close_admission() is False
    with pytest.raises(RuntimeAdmissionRejectedError):
        gate.acquire()
    gate.release()
    assert gate.wait_until_settled(0.1) is True
    assert gate.mark_closed() is True
    assert gate.mark_closed() is False
    assert gate.state is RuntimeAdmissionState.CLOSED


@pytest.mark.asyncio
async def test_factory_rejects_before_creating_scope_channel_or_registry_handle():
    services = make_services(snapshot_enabled=False)
    factory = CoordinatedRuntimeFactory(FakeRouter(), services)
    services.admission_gate.close_admission()

    with pytest.raises(RuntimeAdmissionRejectedError):
        await factory.create_run_scope("agent-a", "question")

    assert services.run_registry.observability_snapshot()["active_runs"] == 0
    assert services.observability_dispatcher.gauge_provider.channels == set()


@pytest.mark.parametrize("value", [-1, math.nan, math.inf, True])
def test_admission_timeout_rejects_unsafe_values(value):
    with pytest.raises(ValueError):
        RuntimeAdmissionGate().wait_until_settled(value)
