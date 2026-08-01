from __future__ import annotations

import asyncio

import pytest

from core.runtime import (
    CancellationSource,
    ControllableFaultSleeper,
    FaultAction,
    FaultBlocker,
    FaultPoint,
    ObservabilityOperationError,
)
from tests._diagnostic_fault_fixtures import diagnostic_controller
from tests.test_observability_dispatcher import dispatcher, record


@pytest.mark.asyncio
async def test_flush_fault_reports_fixed_failure_and_preserves_records():
    value, logger, *_ = dispatcher()
    assert value.try_submit(record())
    assert await value.flush()
    before = tuple(logger.records)
    controller = diagnostic_controller(
        FaultPoint.OBSERVABILITY_BEFORE_FLUSH,
        component="observability_dispatcher",
    )

    with pytest.raises(ObservabilityOperationError) as captured:
        await value.flush(fault_controller=controller)

    assert captured.value.error_code == "OBSERVABILITY_FLUSH_FAILED"
    assert tuple(logger.records) == before
    health = value.health.snapshot()
    assert health.flush_failures == 1
    assert health.last_safe_error_code == "OBSERVABILITY_FLUSH_FAILED"
    assert await value.flush()
    assert await value.close()


@pytest.mark.asyncio
async def test_flush_delay_cancellation_is_bounded_and_later_close_succeeds():
    value, *_ = dispatcher()
    sleeper = ControllableFaultSleeper()
    source = CancellationSource()
    controller = diagnostic_controller(
        FaultPoint.OBSERVABILITY_BEFORE_FLUSH,
        component="observability_dispatcher",
        action=FaultAction.DELAY,
        sleeper=sleeper,
    )
    task = asyncio.create_task(
        value.flush(
            timeout=1,
            fault_controller=controller,
            cancellation_token=source.token,
        )
    )
    await asyncio.wait_for(sleeper.entered.wait(), 1)
    source.cancel()
    with pytest.raises(ObservabilityOperationError) as captured:
        await asyncio.wait_for(task, 1)
    assert captured.value.error_code == "OBSERVABILITY_FLUSH_CANCELLED"
    assert value.health.snapshot().flush_failures == 1
    assert await value.close()


@pytest.mark.asyncio
async def test_flush_block_timeout_is_not_reported_as_success():
    value, *_ = dispatcher()
    blocker = FaultBlocker(timeout_seconds=0.02)
    controller = diagnostic_controller(
        FaultPoint.OBSERVABILITY_BEFORE_FLUSH,
        component="observability_dispatcher",
        action=FaultAction.BLOCK_UNTIL_RELEASED,
        blocker=blocker,
    )
    with pytest.raises(ObservabilityOperationError) as captured:
        await value.flush(timeout=1, fault_controller=controller)
    assert captured.value.error_code == "OBSERVABILITY_FLUSH_FAILED"
    assert value.health.snapshot().status == "DEGRADED"
    blocker.close()
    assert await value.close()
