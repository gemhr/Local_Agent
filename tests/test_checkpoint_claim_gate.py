import asyncio
from datetime import UTC, datetime

import pytest

from core.runtime.cancellation import CancellationSource, RunCancelledError
from core.runtime.checkpoint_contract import SchedulerClaimGateState
from core.runtime.claim_gate import (
    SchedulerClaimGate,
    SchedulerClaimGateBusyError,
    SchedulerClaimGateClosedError,
)
from core.runtime.planning import (
    Plan,
    PlanSource,
    PlanStep,
    TaskCapabilityRequirements,
)
from core.runtime.scheduler import SerialScheduler
from core.runtime.state import AgentState, StepStatus


def _plan() -> Plan:
    return Plan(
        "plan",
        1,
        "summary",
        (
            PlanStep(
                "step",
                "step",
                "description",
                (),
                "done",
                "router",
                TaskCapabilityRequirements(),
            ),
        ),
        datetime(2026, 1, 1, tzinfo=UTC),
        PlanSource.DETERMINISTIC,
    )


@pytest.mark.asyncio
async def test_pause_waits_for_entered_claim_and_blocks_new_scheduler_claim():
    gate = SchedulerClaimGate()
    assert gate.enter_claim()
    pause = asyncio.create_task(gate.pause(timeout=1, cancellation_token=None))
    await asyncio.sleep(0)
    assert gate.snapshot().state is SchedulerClaimGateState.PAUSING
    assert gate.enter_claim() is False
    gate.exit_claim()
    await pause
    assert gate.snapshot().state is SchedulerClaimGateState.PAUSED

    state = AgentState("run")
    state.mark_running()
    scheduler = SerialScheduler(claim_gate=gate)
    scheduler.prepare(_plan(), state, datetime.now(UTC))
    assert scheduler.claim_ready(
        _plan(), state, 1, datetime.now(UTC)
    ) == ()
    assert state.steps["step"].status is StepStatus.PENDING

    gate.resume()
    claims = scheduler.claim_ready(_plan(), state, 1, datetime.now(UTC))
    assert tuple(item.step_id for item in claims) == ("step",)
    assert state.steps["step"].status is StepStatus.RUNNING


@pytest.mark.asyncio
async def test_pause_timeout_and_cancellation_restore_open():
    gate = SchedulerClaimGate()
    assert gate.enter_claim()
    with pytest.raises(TimeoutError):
        await gate.pause(timeout=0, cancellation_token=None)
    assert gate.snapshot().state is SchedulerClaimGateState.OPEN
    gate.exit_claim()

    source = CancellationSource()
    source.cancel()
    with pytest.raises(RunCancelledError):
        await gate.pause(timeout=1, cancellation_token=source.token)
    assert gate.snapshot().state is SchedulerClaimGateState.OPEN


@pytest.mark.asyncio
async def test_concurrent_pause_is_rejected_and_close_is_terminal():
    gate = SchedulerClaimGate()
    assert gate.enter_claim()
    first = asyncio.create_task(gate.pause(timeout=1, cancellation_token=None))
    await asyncio.sleep(0)
    with pytest.raises(SchedulerClaimGateBusyError):
        await gate.pause(timeout=1, cancellation_token=None)
    gate.exit_claim()
    await first
    gate.close()
    with pytest.raises(SchedulerClaimGateClosedError):
        gate.enter_claim()


@pytest.mark.asyncio
async def test_different_run_gates_are_independent():
    first = SchedulerClaimGate()
    second = SchedulerClaimGate()
    await first.pause(timeout=1, cancellation_token=None)
    assert first.enter_claim() is False
    assert second.enter_claim() is True
    second.exit_claim()
    first.resume()
