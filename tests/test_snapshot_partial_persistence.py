from __future__ import annotations

import asyncio

import pytest

from core.runtime import (
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
async def test_after_save_fault_reports_partial_persistence_without_second_save():
    store = InMemorySnapshotStore()
    coordinator, _, _ = _coordinator(store)
    result = await coordinator.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=1,
        fault_controller=operation_controller(FaultPoint.SNAPSHOT_AFTER_SAVE),
    )
    assert result.status is CheckpointStatus.STORE_FAILED
    assert result.safe_error_code == "SNAPSHOT_SAVE_PARTIALLY_PERSISTED"
    assert result.snapshot_id is not None
    evidence = result.snapshot_publication_evidence
    assert evidence is not None and evidence.partially_persisted is True
    stored = store.get(result.snapshot_id)
    assert stored is not None
    assert stored.payload_digest == evidence.snapshot_digest
    assert len(store.list_for_run("run", 10)) == 1
    assert store.save(stored).value == "DUPLICATE"


@pytest.mark.asyncio
async def test_after_save_sqlite_commit_survives_close_and_reopen(tmp_path):
    path = tmp_path / "snapshots.db"
    store = SQLiteSnapshotStore(str(path))
    coordinator, _, _ = _coordinator(store)
    result = await coordinator.create_checkpoint(
        mode=CheckpointMode.REQUIRE_QUIESCENT,
        checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
        timeout=1,
        fault_controller=operation_controller(FaultPoint.SNAPSHOT_AFTER_SAVE),
    )
    snapshot_id = result.snapshot_id
    assert result.snapshot_publication_evidence.partially_persisted is True
    store.close()
    reopened = SQLiteSnapshotStore(str(path))
    assert reopened.get(snapshot_id) is not None
    assert len(reopened.list_for_run("run", 10)) == 1
    reopened.close()


@pytest.mark.asyncio
async def test_after_save_delay_cancellation_never_deletes_committed_snapshot():
    store = InMemorySnapshotStore()
    coordinator, source, _ = _coordinator(store)
    sleeper = ControllableFaultSleeper()
    task = asyncio.create_task(
        coordinator.create_checkpoint(
            mode=CheckpointMode.REQUIRE_QUIESCENT,
            checkpoint_kind=CheckpointKind.STEP_BOUNDARY,
            timeout=1,
            fault_controller=operation_controller(
                FaultPoint.SNAPSHOT_AFTER_SAVE,
                action=FaultAction.DELAY,
                sleeper=sleeper,
            ),
        )
    )
    await asyncio.wait_for(sleeper.entered.wait(), 1)
    source.cancel()
    result = await asyncio.wait_for(task, 1)
    assert result.status is CheckpointStatus.CANCELLED
    assert result.snapshot_id is not None
    assert result.snapshot_publication_evidence.partially_persisted is True
    assert store.get(result.snapshot_id) is not None
