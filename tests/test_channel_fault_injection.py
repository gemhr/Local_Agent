from __future__ import annotations

import asyncio

import pytest

from core.runtime import (
    CancellationReason,
    CancellationSource,
    ControllableFaultSleeper,
    EventChannelConsumerOwner,
    EventChannelClosedError,
    FaultAction,
    FaultBlocker,
    FaultPoint,
    InjectedFaultError,
    RunCancelledError,
    RuntimeEventChannel,
)
from tests._event_fault_fixtures import event_controller, run_started_draft


@pytest.mark.asyncio
async def test_receive_fault_happens_before_queue_removal_and_drain_takes_over_once():
    channel = RuntimeEventChannel(
        2,
        run_id="run-a",
        fault_controller=event_controller(FaultPoint.CHANNEL_BEFORE_RECEIVE),
    )
    event = await channel.publish(run_started_draft())
    iterator = channel.__aiter__()

    with pytest.raises(InjectedFaultError):
        await anext(iterator)

    assert channel.buffered_count == 1
    assert channel.consumer_owner is EventChannelConsumerOwner.RELEASED
    drain = asyncio.create_task(channel.drain_to_discard())
    await asyncio.sleep(0)
    await channel.close()
    await asyncio.wait_for(drain, 1)
    assert event.sequence == 1
    assert channel.buffered_count == 0
    assert channel.consumer_owner is EventChannelConsumerOwner.RELEASED


@pytest.mark.asyncio
async def test_receive_delay_is_interrupted_by_first_wins_run_cancellation():
    source = CancellationSource()
    sleeper = ControllableFaultSleeper()
    controller = event_controller(
        FaultPoint.CHANNEL_BEFORE_RECEIVE,
        action=FaultAction.DELAY,
        sleeper=sleeper,
    )
    channel = RuntimeEventChannel(
        2,
        run_id="run-a",
        cancellation_token=source.token,
        fault_controller=controller,
    )
    await channel.publish(run_started_draft())
    iterator = channel.__aiter__()
    waiting = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(sleeper.entered.wait(), 1)

    assert source.cancel(CancellationReason.CLIENT_DISCONNECTED) is True
    assert source.cancel(CancellationReason.SERVER_SHUTDOWN) is False
    with pytest.raises(RunCancelledError) as captured:
        await asyncio.wait_for(waiting, 1)
    assert captured.value.reason == CancellationReason.CLIENT_DISCONNECTED.value
    assert channel.buffered_count == 1
    assert channel.consumer_owner is EventChannelConsumerOwner.RELEASED
    await channel.abort()


@pytest.mark.asyncio
async def test_drain_handoff_block_keeps_owner_released_and_backpressure_bounded():
    blocker = FaultBlocker(timeout_seconds=2)
    controller = event_controller(
        FaultPoint.CHANNEL_BEFORE_DRAIN_HANDOFF,
        action=FaultAction.BLOCK_UNTIL_RELEASED,
        blocker=blocker,
    )
    channel = RuntimeEventChannel(
        1, run_id="run-a", fault_controller=controller
    )
    await channel.publish(run_started_draft())
    transport = channel.__aiter__()
    await transport.aclose()

    drain = asyncio.create_task(channel.drain_to_discard())
    await asyncio.wait_for(blocker.entered.wait(), 1)
    assert channel.consumer_owner is EventChannelConsumerOwner.RELEASED
    with pytest.raises(RuntimeError, match="handoff is pending"):
        channel.__aiter__()
    producer = asyncio.create_task(channel.publish(run_started_draft()))
    await asyncio.sleep(0)
    assert not producer.done()

    blocker.release.set()
    second = await asyncio.wait_for(producer, 1)
    assert second.sequence == 2
    await channel.close()
    await asyncio.wait_for(drain, 1)
    assert channel.consumer_owner is EventChannelConsumerOwner.RELEASED
    assert channel.buffered_count == 0


@pytest.mark.asyncio
async def test_abort_wakes_receive_fault_block_without_removing_business_event_twice():
    blocker = FaultBlocker(timeout_seconds=2)
    channel = RuntimeEventChannel(
        2,
        run_id="run-a",
        fault_controller=event_controller(
            FaultPoint.CHANNEL_BEFORE_RECEIVE,
            action=FaultAction.BLOCK_UNTIL_RELEASED,
            blocker=blocker,
        ),
    )
    await channel.publish(run_started_draft())
    iterator = channel.__aiter__()
    waiting = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(blocker.entered.wait(), 1)
    await channel.abort()
    with pytest.raises(EventChannelClosedError):
        await asyncio.wait_for(waiting, 1)
    assert channel.buffered_count == 0
    assert channel.consumer_owner is EventChannelConsumerOwner.ABORTED
