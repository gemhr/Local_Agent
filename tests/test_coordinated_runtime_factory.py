from __future__ import annotations

import pytest

from core.runtime import (
    CoordinatedRuntimeFactory,
    EventChannelState,
    FaultInjectionController,
)
from tests._runtime_assembly_fixtures import FakeRouter, make_services


@pytest.mark.asyncio
async def test_factory_creates_isolated_single_identity_run_scopes() -> None:
    services = make_services()
    factory = CoordinatedRuntimeFactory(FakeRouter(), services)

    first = await factory.create_run_scope("core_router", "question")
    second = await factory.create_run_scope("core_router", "question")

    assert first.run_context.run_id == first.agent_state.run_id
    assert first.event_channel.run_id == first.run_context.run_id
    assert first.event_emitter.channel is first.event_channel
    assert first.coordinator.event_emitter is first.event_emitter
    assert first.coordinator.activity_tracker is first.run_context.activity_tracker
    assert first.cancellation_source.token is first.run_context.cancellation_token
    assert first.checkpoint_coordinator is None
    assert first.plan is None
    assert first.scheduler is None
    assert first.run_context.run_id != second.run_context.run_id
    assert first.agent_state is not second.agent_state
    assert first.event_channel is not second.event_channel

    await first.close()
    await first.close()
    await second.close()
    assert first.event_channel.state is EventChannelState.CLOSED


@pytest.mark.asyncio
async def test_factory_does_not_cache_request_scope_or_auto_checkpoint() -> None:
    services = make_services()
    factory = CoordinatedRuntimeFactory(FakeRouter(), services)
    scope = await factory.create("core_router", "question")

    assert not hasattr(services, "run_context")
    assert not hasattr(services, "agent_state")
    assert not hasattr(services, "event_channel")
    assert services.snapshot_store.list_for_run(scope.run_id, 10) == ()

    await scope.close()


@pytest.mark.asyncio
async def test_factory_transports_fault_controller_only_on_the_selected_run() -> None:
    services = make_services()
    factory = CoordinatedRuntimeFactory(FakeRouter(), services)
    controller = FaultInjectionController.disabled()

    selected = await factory.create_run_scope(
        "core_router",
        "question",
        fault_controller=controller,
    )
    ordinary = await factory.create_run_scope("core_router", "question")

    assert selected.fault_controller is controller
    assert selected.driver._fault_controller is controller
    assert ordinary.fault_controller is None
    assert ordinary.driver._fault_controller is None
    assert not hasattr(services, "fault_controller")

    await selected.close()
    await ordinary.close()


@pytest.mark.asyncio
async def test_unexecuted_scope_can_be_aborted_safely() -> None:
    services = make_services()
    scope = await CoordinatedRuntimeFactory(
        FakeRouter(), services
    ).create_run_scope("core_router", "question")

    await scope.close(abort=True)
    await scope.close(abort=True)

    assert scope.event_channel.state is EventChannelState.ABORTED
    assert scope.cancellation_source.token.is_cancelled() is False


@pytest.mark.asyncio
async def test_factory_failure_unregisters_request_channel(monkeypatch) -> None:
    services = make_services()
    dispatcher = services.observability_dispatcher

    class BrokenCoordinator:
        @classmethod
        def for_dynamic_resolver(cls, **kwargs):
            raise RuntimeError("constructor failed")

    monkeypatch.setattr(
        "core.runtime.runtime_factory.RunCoordinator",
        BrokenCoordinator,
    )
    factory = CoordinatedRuntimeFactory(FakeRouter(), services)

    with pytest.raises(RuntimeError, match="constructor failed"):
        await factory.create_run_scope("core_router", "question")

    assert dispatcher.gauge_provider.channels == set()
