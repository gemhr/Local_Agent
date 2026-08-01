from __future__ import annotations

import asyncio

import pytest

from core.runtime import (
    CancellationSource,
    CheckpointKind,
    CheckpointMode,
    CheckpointStatus,
    ControllableFaultSleeper,
    FaultAction,
    FaultPoint,
    InMemorySnapshotStore,
    SQLiteSnapshotStore,
)
from tests._snapshot_recovery_fault_fixtures import operation_controller
from tests.test_checkpoint_integration import _coordinator


@pytest.mark.asyncio
async def test_before_save_fault_does_not_write_or_recapture_state():
    store = InMemorySnapshotStore()
    coordinator, _, state = _coordinator(store)
    before = state.snapshot_copy()
    result = await coordinator.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=1,
        fault_controller=operation_controller(FaultPoint.SNAPSHOT_BEFORE_SAVE),
    )
    assert result.status is CheckpointStatus.STORE_FAILED
    assert result.safe_error_code == "SNAPSHOT_SAVE_INJECTED_FAILURE"
    assert result.snapshot_id is None
    assert result.snapshot_publication_evidence is not None
    assert result.persisted is False
    assert result.snapshot_publication_evidence.partially_persisted is False
    assert result.partially_persisted is False
    assert result.retry_allowed is False
    assert result.snapshot_publication_evidence.snapshot_version is None
    assert store.list_for_run("run", 10) == ()
    assert state.snapshot_copy() == before


@pytest.mark.asyncio
async def test_before_save_fault_keeps_existing_snapshot_unchanged():
    store = InMemorySnapshotStore()
    coordinator, _, _ = _coordinator(store)
    saved = await coordinator.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=1,
    )
    original = store.get(saved.snapshot_id)
    failed = await coordinator.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=1,
        fault_controller=operation_controller(FaultPoint.SNAPSHOT_BEFORE_SAVE),
    )
    assert failed.status is CheckpointStatus.STORE_FAILED
    assert store.list_for_run("run", 10) == (original,)


@pytest.mark.asyncio
async def test_before_save_delay_responds_to_run_cancellation_without_write():
    store = InMemorySnapshotStore()
    coordinator, source, _ = _coordinator(store)
    sleeper = ControllableFaultSleeper()
    task = asyncio.create_task(
        coordinator.create_checkpoint(
            mode=CheckpointMode.REQUIRE_QUIESCENT,
            checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
            timeout=1,
            fault_controller=operation_controller(
                FaultPoint.SNAPSHOT_BEFORE_SAVE,
                action=FaultAction.DELAY,
                sleeper=sleeper,
            ),
        )
    )
    await asyncio.wait_for(sleeper.entered.wait(), 1)
    source.cancel()
    result = await asyncio.wait_for(task, 1)
    assert result.status is CheckpointStatus.CANCELLED
    assert result.snapshot_publication_evidence is not None
    assert result.persisted is False
    assert result.snapshot_publication_evidence.partially_persisted is False
    assert result.retry_allowed is False
    assert store.list_for_run("run", 10) == ()


@pytest.mark.asyncio
async def test_run_a_snapshot_fault_does_not_affect_run_b_on_shared_sqlite_store(
    tmp_path,
):
    store = SQLiteSnapshotStore(str(tmp_path / "shared-snapshots.db"))
    coordinator_a, _, _ = _coordinator(store, run_id="run-a")
    coordinator_b, _, _ = _coordinator(store, run_id="run-b")
    controller_a = operation_controller(FaultPoint.SNAPSHOT_BEFORE_SAVE)

    failed_a, saved_b = await asyncio.gather(
        coordinator_a.create_checkpoint(
            mode=CheckpointMode.REQUIRE_QUIESCENT,
            checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
            timeout=1,
            fault_controller=controller_a,
        ),
        coordinator_b.create_checkpoint(
            mode=CheckpointMode.REQUIRE_QUIESCENT,
            checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
            timeout=1,
        ),
    )
    controller_a.close()

    assert failed_a.status is CheckpointStatus.STORE_FAILED
    assert saved_b.status is CheckpointStatus.SAVED
    assert store.list_for_run("run-a", 10) == ()
    assert len(store.list_for_run("run-b", 10)) == 1
    assert store.get(saved_b.snapshot_id).run_id == "run-b"
    counter = controller_a.snapshot().counters[0]
    assert (counter.match_count, counter.hit_count) == (1, 1)
    store.close()
