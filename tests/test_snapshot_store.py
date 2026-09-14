from dataclasses import replace

import pytest

from core.runtime.snapshot_store import (
    InMemorySnapshotStore,
    SnapshotErrorCode,
    SnapshotSaveStatus,
    SnapshotStoreError,
)
from test_snapshot_contract import make_snapshot


def test_save_get_latest_list_duplicate_conflict_and_close():
    store = InMemorySnapshotStore()
    first = make_snapshot()
    second = make_snapshot(snapshot_id="snapshot-2")
    assert store.save(first) is SnapshotSaveStatus.SAVED
    assert store.save(first) is SnapshotSaveStatus.DUPLICATE
    assert store.save(second) is SnapshotSaveStatus.SAVED
    assert store.get(first.snapshot_id) == first
    assert store.latest(first.run_id) == second
    assert store.list_for_run(first.run_id, 1) == (second,)
    conflict = replace(first, payload_digest=second.payload_digest)
    with pytest.raises(SnapshotStoreError) as caught:
        store.save(conflict)
    assert caught.value.error_code is SnapshotErrorCode.SNAPSHOT_CORRUPTED
    store.close()
    store.close()
    with pytest.raises(SnapshotStoreError) as closed:
        store.get(first.snapshot_id)
    assert closed.value.error_code is SnapshotErrorCode.SNAPSHOT_STORE_FAILED


def test_same_id_different_valid_content_is_conflict():
    store = InMemorySnapshotStore()
    first = make_snapshot()
    other = make_snapshot(snapshot_id=first.snapshot_id, run_id="run-2")
    store.save(first)
    with pytest.raises(SnapshotStoreError) as caught:
        store.save(other)
    assert caught.value.error_code is SnapshotErrorCode.SNAPSHOT_ID_CONFLICT
