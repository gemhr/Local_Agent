from __future__ import annotations

import pytest

from core.runtime import CoordinatedRuntimeFactory
from tests._runtime_assembly_fixtures import FakeRouter, make_services


@pytest.mark.asyncio
async def test_factory_creates_fresh_run_scope_and_close_releases_registry() -> None:
    services = make_services(snapshot_enabled=False)
    factory = CoordinatedRuntimeFactory(FakeRouter(), services)
    first = await factory.create_run_scope("agent-a", "first")
    second = await factory.create_run_scope("agent-a", "second")

    assert first is not second
    assert first.run_context is not second.run_context
    assert first.agent_state is not second.agent_state
    assert first.event_channel is not second.event_channel
    assert first.budget_ledger is not second.budget_ledger
    assert services.run_registry.observability_snapshot()["active_runs"] == 2

    await first.close()
    await second.close()

    assert services.run_registry.observability_snapshot()["active_runs"] == 0
    assert services.observability_dispatcher.gauge_provider.channels == set()


def test_application_factory_does_not_cache_a_current_scope() -> None:
    services = make_services(snapshot_enabled=False)
    factory = CoordinatedRuntimeFactory(FakeRouter(), services)

    assert not any(
        name in {"_scope", "_current_scope", "_run_scope", "_controller"}
        for name in factory.__slots__
    )
