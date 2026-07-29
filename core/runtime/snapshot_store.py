#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Append-only in-memory and SQLite stores for verified RunSnapshot values."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
import sqlite3
import threading
from typing import Protocol

from core.runtime.snapshot_contract import (
    RunSnapshot,
    UnsupportedSnapshotSchemaError,
)
from core.runtime.snapshot_serialization import snapshot_from_json, snapshot_to_json


MAX_SNAPSHOT_LIST_LIMIT = 1000


class SnapshotSaveStatus(str, Enum):
    SAVED = "SAVED"
    DUPLICATE = "DUPLICATE"


class SnapshotErrorCode(str, Enum):
    SNAPSHOT_ID_CONFLICT = "SNAPSHOT_ID_CONFLICT"
    SNAPSHOT_CORRUPTED = "SNAPSHOT_CORRUPTED"
    SNAPSHOT_SCHEMA_UNSUPPORTED = "SNAPSHOT_SCHEMA_UNSUPPORTED"
    SNAPSHOT_STORE_FAILED = "SNAPSHOT_STORE_FAILED"


_SAFE_ERROR_MESSAGES = {
    SnapshotErrorCode.SNAPSHOT_ID_CONFLICT: "snapshot ID conflicts with stored content",
    SnapshotErrorCode.SNAPSHOT_CORRUPTED: "snapshot integrity verification failed",
    SnapshotErrorCode.SNAPSHOT_SCHEMA_UNSUPPORTED: "snapshot schema is unsupported",
    SnapshotErrorCode.SNAPSHOT_STORE_FAILED: "snapshot store operation failed",
}


class SnapshotStoreError(RuntimeError):
    """A safe typed error that never includes payload, SQL or filesystem paths."""

    def __init__(self, error_code: SnapshotErrorCode) -> None:
        self.error_code = error_code
        self.safe_message = _SAFE_ERROR_MESSAGES[error_code]
        super().__init__(f"{self.safe_message} (error_code={error_code.value})")


class SnapshotStore(Protocol):
    def save(self, snapshot: RunSnapshot) -> SnapshotSaveStatus: ...

    def get(self, snapshot_id: str) -> RunSnapshot | None: ...

    def latest(self, run_id: str) -> RunSnapshot | None: ...

    def list_for_run(self, run_id: str, limit: int) -> tuple[RunSnapshot, ...]: ...

    def close(self) -> None: ...


def _validate_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _validate_limit(limit: object) -> int:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_SNAPSHOT_LIST_LIMIT
    ):
        raise ValueError(
            f"limit must be an integer between 1 and {MAX_SNAPSHOT_LIST_LIMIT}"
        )
    return limit


def _verify_for_store(snapshot: object) -> RunSnapshot:
    if not isinstance(snapshot, RunSnapshot):
        raise TypeError("snapshot must be a RunSnapshot")
    try:
        snapshot.verify_digest()
    except Exception:
        raise SnapshotStoreError(SnapshotErrorCode.SNAPSHOT_CORRUPTED) from None
    return snapshot


class InMemorySnapshotStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, RunSnapshot] = {}
        self._closed = False

    def save(self, snapshot: RunSnapshot) -> SnapshotSaveStatus:
        verified = _verify_for_store(snapshot)
        with self._lock:
            self._ensure_open()
            existing = self._records.get(verified.snapshot_id)
            if existing is not None:
                _verify_for_store(existing)
                if existing.payload_digest == verified.payload_digest:
                    return SnapshotSaveStatus.DUPLICATE
                raise SnapshotStoreError(SnapshotErrorCode.SNAPSHOT_ID_CONFLICT)
            self._records[verified.snapshot_id] = verified
            return SnapshotSaveStatus.SAVED

    def get(self, snapshot_id: str) -> RunSnapshot | None:
        _validate_id(snapshot_id, "snapshot_id")
        with self._lock:
            self._ensure_open()
            value = self._records.get(snapshot_id)
            return _verify_for_store(value) if value is not None else None

    def latest(self, run_id: str) -> RunSnapshot | None:
        values = self.list_for_run(run_id, MAX_SNAPSHOT_LIST_LIMIT)
        return values[0] if values else None

    def list_for_run(self, run_id: str, limit: int) -> tuple[RunSnapshot, ...]:
        _validate_id(run_id, "run_id")
        _validate_limit(limit)
        with self._lock:
            self._ensure_open()
            values = [item for item in self._records.values() if item.run_id == run_id]
            values.sort(
                key=lambda item: (item.created_at, item.snapshot_id), reverse=True
            )
            return tuple(_verify_for_store(item) for item in values[:limit])

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise SnapshotStoreError(SnapshotErrorCode.SNAPSHOT_STORE_FAILED)


class SQLiteSnapshotStore:
    """A local append-only store using one atomic transaction per save."""

    def __init__(self, db_path: str) -> None:
        if not isinstance(db_path, str) or not db_path.strip():
            raise ValueError("db_path must be a non-empty string")
        self._lock = threading.RLock()
        self._closed = False
        try:
            if db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                db_path,
                check_same_thread=False,
                isolation_level=None,
                timeout=30.0,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_snapshots (
                    snapshot_schema_version INTEGER NOT NULL,
                    snapshot_id TEXT NOT NULL PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runtime_snapshots_run_created
                ON runtime_snapshots(run_id, created_at DESC)
                """
            )
        except (OSError, sqlite3.Error):
            raise SnapshotStoreError(
                SnapshotErrorCode.SNAPSHOT_STORE_FAILED
            ) from None

    def save(self, snapshot: RunSnapshot) -> SnapshotSaveStatus:
        verified = _verify_for_store(snapshot)
        payload_json = snapshot_to_json(verified)
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT * FROM runtime_snapshots WHERE snapshot_id = ?",
                    (verified.snapshot_id,),
                ).fetchone()
                if row is not None:
                    # Verify stored bytes before deciding duplicate or conflict.
                    existing = self._snapshot_from_row(row)
                    self._connection.execute("COMMIT")
                    if existing.payload_digest == verified.payload_digest:
                        return SnapshotSaveStatus.DUPLICATE
                    raise SnapshotStoreError(
                        SnapshotErrorCode.SNAPSHOT_ID_CONFLICT
                    )
                self._connection.execute(
                    """
                    INSERT INTO runtime_snapshots (
                        snapshot_schema_version, snapshot_id, run_id, created_at,
                        payload_json, payload_digest
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        verified.snapshot_schema_version,
                        verified.snapshot_id,
                        verified.run_id,
                        verified.created_at.isoformat(),
                        payload_json,
                        verified.payload_digest,
                    ),
                )
                self._connection.execute("COMMIT")
                return SnapshotSaveStatus.SAVED
            except SnapshotStoreError:
                self._rollback_safely()
                raise
            except (sqlite3.Error, ValueError, TypeError):
                self._rollback_safely()
                raise SnapshotStoreError(
                    SnapshotErrorCode.SNAPSHOT_STORE_FAILED
                ) from None

    def get(self, snapshot_id: str) -> RunSnapshot | None:
        _validate_id(snapshot_id, "snapshot_id")
        with self._lock:
            self._ensure_open()
            try:
                row = self._connection.execute(
                    "SELECT * FROM runtime_snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()
                return self._snapshot_from_row(row) if row is not None else None
            except SnapshotStoreError:
                raise
            except sqlite3.Error:
                raise SnapshotStoreError(
                    SnapshotErrorCode.SNAPSHOT_STORE_FAILED
                ) from None

    def latest(self, run_id: str) -> RunSnapshot | None:
        _validate_id(run_id, "run_id")
        with self._lock:
            self._ensure_open()
            try:
                row = self._connection.execute(
                    """
                    SELECT * FROM runtime_snapshots
                    WHERE run_id = ?
                    ORDER BY created_at DESC, snapshot_id DESC
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                return self._snapshot_from_row(row) if row is not None else None
            except SnapshotStoreError:
                raise
            except sqlite3.Error:
                raise SnapshotStoreError(
                    SnapshotErrorCode.SNAPSHOT_STORE_FAILED
                ) from None

    def list_for_run(self, run_id: str, limit: int) -> tuple[RunSnapshot, ...]:
        _validate_id(run_id, "run_id")
        _validate_limit(limit)
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(
                    """
                    SELECT * FROM runtime_snapshots
                    WHERE run_id = ?
                    ORDER BY created_at DESC, snapshot_id DESC
                    LIMIT ?
                    """,
                    (run_id, limit),
                ).fetchall()
                return tuple(self._snapshot_from_row(row) for row in rows)
            except SnapshotStoreError:
                raise
            except sqlite3.Error:
                raise SnapshotStoreError(
                    SnapshotErrorCode.SNAPSHOT_STORE_FAILED
                ) from None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._connection.close()
            except sqlite3.Error:
                return

    def _snapshot_from_row(self, row: sqlite3.Row) -> RunSnapshot:
        try:
            version = row["snapshot_schema_version"]
            if isinstance(version, bool) or not isinstance(version, int):
                raise ValueError("invalid schema version")
            snapshot = snapshot_from_json(str(row["payload_json"]))
            if (
                snapshot.snapshot_schema_version != version
                or snapshot.snapshot_id != str(row["snapshot_id"])
                or snapshot.run_id != str(row["run_id"])
                or snapshot.created_at.isoformat() != str(row["created_at"])
                or snapshot.payload_digest != str(row["payload_digest"])
            ):
                raise ValueError("row envelope mismatch")
            return snapshot
        except UnsupportedSnapshotSchemaError:
            raise SnapshotStoreError(
                SnapshotErrorCode.SNAPSHOT_SCHEMA_UNSUPPORTED
            ) from None
        except SnapshotStoreError:
            raise
        except Exception:
            raise SnapshotStoreError(
                SnapshotErrorCode.SNAPSHOT_CORRUPTED
            ) from None

    def _ensure_open(self) -> None:
        if self._closed:
            raise SnapshotStoreError(SnapshotErrorCode.SNAPSHOT_STORE_FAILED)

    def _rollback_safely(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:
            return


__all__ = [
    "InMemorySnapshotStore",
    "MAX_SNAPSHOT_LIST_LIMIT",
    "SQLiteSnapshotStore",
    "SnapshotErrorCode",
    "SnapshotSaveStatus",
    "SnapshotStore",
    "SnapshotStoreError",
]
