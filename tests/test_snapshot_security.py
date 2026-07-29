import json
import sqlite3

import pytest

from core.runtime.snapshot_serialization import snapshot_to_json
from core.runtime.snapshot_store import (
    SQLiteSnapshotStore,
    SnapshotErrorCode,
    SnapshotStoreError,
)
from test_snapshot_contract import make_snapshot


SENSITIVE_MARKERS = (
    "SECRET_PROMPT_TEXT",
    "MODEL_OUTPUT_SECRET",
    "TOOL_ARGUMENT_SECRET",
    "TOOL_OUTPUT_SECRET",
    "RAG_CHUNK_SECRET",
    "MEMORY_SECRET",
    r"C:\Users\private-user\kb",
    "provider-secret-error",
)


def test_snapshot_json_repr_and_sqlite_payload_do_not_contain_sensitive_markers(
    tmp_path,
):
    path = tmp_path / "snapshots.sqlite3"
    sensitive_text = "|".join(SENSITIVE_MARKERS)
    snapshot = make_snapshot(sensitive_text=sensitive_text)
    serialized = snapshot_to_json(snapshot)
    store = SQLiteSnapshotStore(str(path))
    store.save(snapshot)
    with sqlite3.connect(path) as connection:
        payload = connection.execute(
            "SELECT payload_json FROM runtime_snapshots"
        ).fetchone()[0]
    combined = serialized + payload + repr(snapshot)
    for marker in SENSITIVE_MARKERS:
        assert marker not in combined
    store.close()


def test_sqlite_corruption_fails_closed_with_safe_error(tmp_path):
    path = tmp_path / "private-user" / "snapshots.sqlite3"
    snapshot = make_snapshot()
    store = SQLiteSnapshotStore(str(path))
    store.save(snapshot)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE runtime_snapshots SET payload_json = ? WHERE snapshot_id = ?",
            ('{"payload":"TOOL_OUTPUT_SECRET"}', snapshot.snapshot_id),
        )
    with pytest.raises(SnapshotStoreError) as caught:
        store.get(snapshot.snapshot_id)
    assert caught.value.error_code is SnapshotErrorCode.SNAPSHOT_CORRUPTED
    message = str(caught.value)
    assert "TOOL_OUTPUT_SECRET" not in message
    assert str(path) not in message
    store.close()


def test_sqlite_unsupported_schema_is_typed(tmp_path):
    path = tmp_path / "snapshots.sqlite3"
    snapshot = make_snapshot()
    store = SQLiteSnapshotStore(str(path))
    store.save(snapshot)
    with sqlite3.connect(path) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM runtime_snapshots"
            ).fetchone()[0]
        )
        payload["snapshot_schema_version"] = 999
        connection.execute(
            """
            UPDATE runtime_snapshots
            SET snapshot_schema_version = ?, payload_json = ?
            WHERE snapshot_id = ?
            """,
            (999, json.dumps(payload), snapshot.snapshot_id),
        )
    with pytest.raises(SnapshotStoreError) as caught:
        store.get(snapshot.snapshot_id)
    assert caught.value.error_code is SnapshotErrorCode.SNAPSHOT_SCHEMA_UNSUPPORTED
    store.close()
