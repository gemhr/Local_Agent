from __future__ import annotations

import asyncio

import pytest

from core.runtime import (
    CancellationSource,
    FaultAction,
    FaultBlocker,
    FaultPoint,
    InMemoryRunEventJournal,
    InMemorySnapshotStore,
    RecoveryReason,
    RecoveryStatus,
    RecoveryValidator,
    RunStartedPayload,
    RuntimeEventType,
)
from tests._recovery_fixtures import (
    recovery_plan,
    recovery_snapshot,
    runtime_event,
)
from tests._snapshot_recovery_fault_fixtures import operation_controller


class CountingSnapshotStore(InMemorySnapshotStore):
    def __init__(self) -> None:
        super().__init__()
        self.get_count = 0

    def get(self, snapshot_id):
        self.get_count += 1
        return super().get(snapshot_id)


class CountingJournal(InMemoryRunEventJournal):
    def __init__(self) -> None:
        super().__init__()
        self.last_count = 0
        self.read_count = 0

    def last_sequence(self, run_id):
        self.last_count += 1
        return super().last_sequence(run_id)

    def read_after(self, run_id, sequence, limit):
        self.read_count += 1
        return super().read_after(run_id, sequence, limit)


def validator_fixture(*, with_tail: bool = False):
    snapshot = recovery_snapshot()
    store = CountingSnapshotStore()
    store.save(snapshot)
    journal = CountingJournal()
    if with_tail:
        journal.append(
            runtime_event(
                1,
                RuntimeEventType.RUN_STARTED,
                RunStartedPayload("RUNNING"),
            )
        )
    return snapshot, store, journal, RecoveryValidator(
        snapshot_store=store, journal=journal
    )


def test_snapshot_before_read_fault_never_reads_store_or_journal():
    snapshot, store, journal, validator = validator_fixture()
    assessment = validator.validate(
        snapshot_id=snapshot.snapshot_id,
        current_plan=recovery_plan(),
        fault_controller=operation_controller(FaultPoint.SNAPSHOT_BEFORE_READ),
    )
    assert assessment.status is RecoveryStatus.FAILED
    assert assessment.reasons == (RecoveryReason.SNAPSHOT_READ_FAILED,)
    assert assessment.run_id is None
    assert (store.get_count, journal.last_count, journal.read_count) == (0, 0, 0)


def test_before_tail_fault_preserves_snapshot_identity_and_skips_journal():
    snapshot, store, journal, validator = validator_fixture()
    assessment = validator.validate(
        snapshot_id=snapshot.snapshot_id,
        current_plan=recovery_plan(),
        fault_controller=operation_controller(
            FaultPoint.RECOVERY_BEFORE_TAIL_READ
        ),
    )
    assert assessment.status is RecoveryStatus.FAILED
    assert assessment.reasons == (
        RecoveryReason.JOURNAL_TAIL_READ_NOT_EXECUTED,
    )
    assert assessment.run_id == snapshot.run_id
    assert assessment.snapshot_sequence == snapshot.last_journal_sequence
    assert (store.get_count, journal.last_count, journal.read_count) == (1, 0, 0)


def test_after_tail_fault_keeps_read_count_but_returns_no_recovery_decision():
    snapshot, store, journal, validator = validator_fixture(with_tail=True)
    assessment = validator.validate(
        snapshot_id=snapshot.snapshot_id,
        current_plan=recovery_plan(),
        fault_controller=operation_controller(
            FaultPoint.RECOVERY_AFTER_TAIL_READ
        ),
    )
    assert assessment.status is RecoveryStatus.FAILED
    assert assessment.reasons == (RecoveryReason.RECOVERY_VALIDATION_FAILED,)
    assert assessment.journal_last_sequence == 1
    assert journal.read_count == 1
    assert assessment.reduced_projection is None
    assert assessment.tool_decisions == ()
    assert not assessment.automatic_resume_supported


def test_disabled_recovery_controller_is_exact_assessment_parity():
    snapshot, _, _, validator = validator_fixture(with_tail=True)
    normal = validator.validate(
        snapshot_id=snapshot.snapshot_id, current_plan=recovery_plan()
    )
    controller = operation_controller(
        FaultPoint.RECOVERY_AFTER_TAIL_READ, enabled=False
    )
    disabled = validator.validate(
        snapshot_id=snapshot.snapshot_id,
        current_plan=recovery_plan(),
        fault_controller=controller,
    )
    assert disabled == normal
    counter = controller.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (0, 0)


def test_recovery_operation_fault_is_not_cached_by_shared_validator():
    snapshot, _, _, validator = validator_fixture()
    failed = validator.validate(
        snapshot_id=snapshot.snapshot_id,
        current_plan=recovery_plan(),
        fault_controller=operation_controller(
            FaultPoint.RECOVERY_BEFORE_TAIL_READ
        ),
    )
    normal = validator.validate(
        snapshot_id=snapshot.snapshot_id,
        current_plan=recovery_plan(),
    )
    assert failed.status is RecoveryStatus.FAILED
    assert normal.status is RecoveryStatus.RESUMABLE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "point",
    [
        FaultPoint.SNAPSHOT_BEFORE_READ,
        FaultPoint.RECOVERY_BEFORE_TAIL_READ,
        FaultPoint.RECOVERY_AFTER_TAIL_READ,
    ],
)
async def test_recovery_block_responds_to_cancellation_without_replay_or_mutation(
    point,
):
    snapshot, _, _, validator = validator_fixture(
        with_tail=point is FaultPoint.RECOVERY_AFTER_TAIL_READ
    )
    before = snapshot.payload_digest
    blocker = FaultBlocker(timeout_seconds=2)
    controller = operation_controller(
        point,
        action=FaultAction.BLOCK_UNTIL_RELEASED,
        blocker=blocker,
    )
    source = CancellationSource()
    task = asyncio.create_task(
        asyncio.to_thread(
            validator.validate,
            snapshot_id=snapshot.snapshot_id,
            current_plan=recovery_plan(),
            fault_controller=controller,
            cancellation_token=source.token,
        )
    )
    while not blocker.entered.is_set():
        await asyncio.sleep(0)
    source.cancel()
    assessment = await asyncio.wait_for(task, 1)
    blocker.close()
    assert assessment.status is RecoveryStatus.FAILED
    assert assessment.reasons == (
        RecoveryReason.RECOVERY_VALIDATION_CANCELLED,
    )
    assert not assessment.automatic_resume_supported
    assert not assessment.model_replay_allowed
    assert not assessment.tool_replay_allowed
    assert not assessment.retrieval_replay_allowed
    assert snapshot.payload_digest == before
