import asyncio
from datetime import datetime, UTC
from sqlalchemy import update

import pytest

from core.persistence.models import DurableContinuationRow, RunControlRow
from core.runtime.continuation import ContinuationConflict, ContinuationState, GenericContinuationService
from core.runtime.run_control import DurableRunControlService, OwnershipLost

pytestmark = __import__("pytest").mark.asyncio


async def _service(clean_database, run_id):
    run_control = DurableRunControlService(clean_database, lease_seconds=1)
    bootstrap = await run_control.claim(run_id, "bootstrap")
    await run_control.release(bootstrap)
    return GenericContinuationService(
        clean_database, lease_seconds=1, run_control=run_control
    )


async def test_generic_claim_is_single_and_stale_token_is_fenced(clean_database):
    service = await _service(clean_database, "wp4-claim-run")
    item = await service.create(run_id="wp4-claim-run", continuation_kind="TEST", subject_type="x", subject_id="1", payload={})
    await service.mark_ready(item.continuation_id)
    first = await service.claim_ready("worker-a")
    second = await service.claim_ready("worker-b")
    assert first is not None and second is None
    async with clean_database.transaction() as session:
        await session.execute(update(DurableContinuationRow).where(DurableContinuationRow.continuation_id == item.continuation_id).values(claim_deadline_at=datetime(2000, 1, 1, tzinfo=UTC)))
    await service.reap_expired_once()
    current = await service.claim_ready("worker-b")
    assert current is not None
    for operation in (service.heartbeat, service.complete, lambda value: service.fail(value, "STALE")):
        try:
            await operation(first)
        except ContinuationConflict:
            pass
    assert (await service.get(current.continuation_id)).state == ContinuationState.PROCESSING


async def test_reaper_only_requeues_expired_processing(clean_database):
    service = await _service(clean_database, "wp4-reap-run")
    item = await service.create(run_id="wp4-reap-run", continuation_kind="TEST", subject_type="x", subject_id="1", payload={})
    await service.mark_ready(item.continuation_id)
    claimed = await service.claim_ready("worker-a")
    assert claimed is not None
    await service.heartbeat(claimed)
    assert await service.reap_expired_once() == 0
    async with clean_database.transaction() as session:
        await session.execute(update(DurableContinuationRow).where(DurableContinuationRow.continuation_id == item.continuation_id).values(claim_deadline_at=datetime(2000, 1, 1, tzinfo=UTC)))
    assert await service.reap_expired_once() == 1
    assert (await service.get(item.continuation_id)).state == ContinuationState.READY


async def test_payload_tamper_fails_closed_before_handler(clean_database):
    service = await _service(clean_database, "wp4-payload-run")
    item = await service.create(run_id="wp4-payload-run", continuation_kind="TEST", subject_type="x", subject_id="1", payload={"approval_id": "a"})
    await service.mark_ready(item.continuation_id)
    claimed = await service.claim_ready("worker-a")
    async with clean_database.transaction() as session:
        await session.execute(update(DurableContinuationRow).where(DurableContinuationRow.continuation_id == item.continuation_id).values(payload={"approval_id": "tampered"}))
    called = False
    async def handler(*_):
        nonlocal called
        called = True
    try:
        await service.resume_claimed(await service.get(item.continuation_id), handler)
    except ContinuationConflict:
        pass
    assert called is False
    assert (await service.get(item.continuation_id)).state == ContinuationState.FAILED


async def test_two_service_instances_have_one_atomic_claim_winner(clean_database):
    service_a = await _service(clean_database, "wp4-two-worker-run")
    service_b = GenericContinuationService(
        clean_database,
        lease_seconds=1,
        run_control=DurableRunControlService(clean_database, lease_seconds=1),
    )
    item = await service_a.create(
        run_id="wp4-two-worker-run", continuation_kind="TEST",
        subject_type="x", subject_id="1", payload={"durable": True},
    )
    await service_a.mark_ready(item.continuation_id)
    claims = await asyncio.gather(
        service_a.claim_ready("worker-a", continuation_id=item.continuation_id),
        service_b.claim_ready("worker-b", continuation_id=item.continuation_id),
    )
    assert sum(claim is not None for claim in claims) == 1
    assert (await service_b.list_by_run(item.run_id))[0].continuation_id == item.continuation_id


async def test_cross_worker_takeover_fences_old_continuation_and_run(clean_database):
    service_a = await _service(clean_database, "wp4-takeover-run")
    service_b = GenericContinuationService(
        clean_database,
        lease_seconds=1,
        run_control=DurableRunControlService(clean_database, lease_seconds=1),
    )
    item = await service_a.create(
        run_id="wp4-takeover-run", continuation_kind="TEST",
        subject_type="x", subject_id="1", payload={},
    )
    await service_a.mark_ready(item.continuation_id)
    claim_a = await service_a.claim_ready("worker-a", continuation_id=item.continuation_id)
    lease_a = await service_a.run_control.claim(item.run_id, "worker-a")
    async with clean_database.transaction() as session:
        await session.execute(update(DurableContinuationRow).where(
            DurableContinuationRow.continuation_id == item.continuation_id
        ).values(claim_deadline_at=datetime(2000, 1, 1, tzinfo=UTC)))
        await session.execute(update(RunControlRow).where(
            RunControlRow.run_id == item.run_id
        ).values(lease_until=datetime(2000, 1, 1, tzinfo=UTC)))
    assert await service_b.reap_expired_once() == 1
    claim_b = await service_b.claim_ready("worker-b", continuation_id=item.continuation_id)
    assert claim_b.claim_token != claim_a.claim_token
    resumed_leases = []

    async def resume_on_worker_b(_item, lease):
        resumed_leases.append(lease)
        return "resumed"

    result, completed = await service_b.resume_claimed(claim_b, resume_on_worker_b)
    lease_b = resumed_leases[0]
    assert result == "resumed"
    assert completed.state == ContinuationState.SUCCEEDED
    assert lease_b.fencing_token > lease_a.fencing_token
    with pytest.raises(OwnershipLost):
        await service_a.run_control.assert_current(lease_a)
    for operation in (
        service_a.heartbeat,
        service_a.complete,
        lambda value: service_a.fail(value, "STALE"),
    ):
        with pytest.raises(ContinuationConflict):
            await operation(claim_a)


async def test_cancelled_run_never_calls_resume_handler(clean_database):
    service = await _service(clean_database, "wp4-cancelled-run")
    item = await service.create(
        run_id="wp4-cancelled-run", continuation_kind="TEST",
        subject_type="x", subject_id="1", payload={},
    )
    await service.mark_ready(item.continuation_id)
    claimed = await service.claim_ready("worker-a", continuation_id=item.continuation_id)
    await service.run_control.request_cancel(item.run_id, "user cancelled")
    called = False

    async def handler(*_):
        nonlocal called
        called = True

    result, terminal = await service.resume_claimed(claimed, handler)
    assert result is None
    assert called is False
    assert terminal.state == ContinuationState.CANCELLED
