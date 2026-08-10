#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Append-only in-memory and SQLite stores for verified RunSnapshot values."""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
import sqlite3
import threading
from typing import Protocol

from core.persistence_migration import (
    PERSISTENCE_PREFLIGHT_FAILED,
    PERSISTENCE_SCHEMA_UNSUPPORTED,
    MigrationAction,
    PersistenceError,
    PersistencePreflightResult,
    PreflightMode,
    PreflightStatus,
    StoreId,
    open_read_only,
    sqlite_quick_check,
)
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
        return _snapshot_row_to_value(row)

    def _ensure_open(self) -> None:
        if self._closed:
            raise SnapshotStoreError(SnapshotErrorCode.SNAPSHOT_STORE_FAILED)

    def _rollback_safely(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:
            return


# ---------------------------------------------------------------------------
# WP1-D Snapshot Preflight（validation-only；无 migration / writeback / adoption）
# ---------------------------------------------------------------------------

# 每个条目 = (name, declared_type, notnull, dflt_value, pk_position)。
_SNAPSHOT_CURRENT_COLUMNS = (
    ("snapshot_schema_version", "INTEGER", 1, None, 0),
    ("snapshot_id", "TEXT", 1, None, 1),
    ("run_id", "TEXT", 1, None, 0),
    ("created_at", "TEXT", 1, None, 0),
    ("payload_json", "TEXT", 1, None, 0),
    ("payload_digest", "TEXT", 1, None, 0),
)
_SNAPSHOT_RUN_CREATED_INDEX = (
    "idx_runtime_snapshots_run_created",
    (0, 0, ("run_id", "created_at")),
)
_SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS = frozenset({1})


def _norm_type(value) -> str:
    return str(value).upper()


def _norm_default(value) -> object:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]
    return text.lower()


def _snapshot_shape_current(conn: sqlite3.Connection) -> bool:
    """deterministic exact current physical signature：列/类型/NOT NULL/
    snapshot_id PRIMARY KEY/required index 定义全部精确匹配。"""
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "runtime_snapshots" not in tables:
        return False
    try:
        actual = conn.execute("PRAGMA table_info(runtime_snapshots)").fetchall()
    except sqlite3.Error:
        return False
    if len(actual) != len(_SNAPSHOT_CURRENT_COLUMNS):
        return False
    actual_by_name = {row[1]: row for row in actual}
    for name, type_, notnull, dflt, pk in _SNAPSHOT_CURRENT_COLUMNS:
        row = actual_by_name.get(name)
        if row is None:
            return False
        if _norm_type(row[2]) != _norm_type(type_):
            return False
        if int(row[3]) != int(notnull):
            return False
        if _norm_default(row[4]) != _norm_default(dflt):
            return False
        if int(row[5]) != int(pk):
            return False
    index_name, (unique, partial, columns) = _SNAPSHOT_RUN_CREATED_INDEX
    try:
        rows = conn.execute("PRAGMA index_list(runtime_snapshots)").fetchall()
    except sqlite3.Error:
        return False
    for row in rows:
        if row[1] != index_name:
            continue
        if int(row[2]) != unique or int(row[4]) != partial:
            return False
        try:
            actual_columns = tuple(
                r[2] for r in conn.execute(f"PRAGMA index_info({index_name})")
            )
        except sqlite3.Error:
            return False
        return actual_columns == columns
    return False


def _snapshot_unique_constraint_set(
    conn: sqlite3.Connection,
) -> frozenset[tuple[str, ...]]:
    """table-level UNIQUE constraint（origin='u'）列元组集合；current contract
    必须为空。额外 UNIQUE(run_id) 会阻止同一 Run 保存多个 Snapshot → 拒绝。"""
    result: set[tuple[str, ...]] = set()
    try:
        rows = conn.execute("PRAGMA index_list(runtime_snapshots)").fetchall()
    except sqlite3.Error:
        return frozenset()
    for row in rows:
        if row[3] == "u":
            try:
                result.add(
                    tuple(
                        r[2]
                        for r in conn.execute(f"PRAGMA index_info({row[1]})")
                    )
                )
            except sqlite3.Error:
                return frozenset()
    return frozenset(result)


def _snapshot_unique_named_indexes(conn: sqlite3.Connection) -> frozenset[str]:
    """unique named index（origin='c'，unique=1）名称集合；current contract 必须为空。"""
    try:
        return frozenset(
            row[1]
            for row in conn.execute("PRAGMA index_list(runtime_snapshots)").fetchall()
            if row[3] == "c" and int(row[2]) == 1
        )
    except sqlite3.Error:
        return frozenset()


def _snapshot_shape_current(conn: sqlite3.Connection) -> bool:
    """deterministic exact current physical signature：列/类型/NOT NULL/
    snapshot_id PRIMARY KEY/required index 定义/UNIQUE semantic set exact
    全部精确匹配。"""
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "runtime_snapshots" not in tables:
        return False
    if _snapshot_unique_constraint_set(conn) != frozenset():
        return False
    if _snapshot_unique_named_indexes(conn) != frozenset():
        return False
    try:
        actual = conn.execute("PRAGMA table_info(runtime_snapshots)").fetchall()
    except sqlite3.Error:
        return False
    if len(actual) != len(_SNAPSHOT_CURRENT_COLUMNS):
        return False
    actual_by_name = {row[1]: row for row in actual}
    for name, type_, notnull, dflt, pk in _SNAPSHOT_CURRENT_COLUMNS:
        row = actual_by_name.get(name)
        if row is None:
            return False
        if _norm_type(row[2]) != _norm_type(type_):
            return False
        if int(row[3]) != int(notnull):
            return False
        if _norm_default(row[4]) != _norm_default(dflt):
            return False
        if int(row[5]) != int(pk):
            return False
    index_name, (unique, partial, columns) = _SNAPSHOT_RUN_CREATED_INDEX
    try:
        rows = conn.execute("PRAGMA index_list(runtime_snapshots)").fetchall()
    except sqlite3.Error:
        return False
    for row in rows:
        if row[1] != index_name:
            continue
        if int(row[2]) != unique or int(row[4]) != partial:
            return False
        try:
            actual_columns = tuple(
                r[2] for r in conn.execute(f"PRAGMA index_info({index_name})")
            )
        except sqlite3.Error:
            return False
        return actual_columns == columns
    return False


def _snapshot_row_to_value(row: sqlite3.Row) -> RunSnapshot:
    """Module-level row → RunSnapshot；与实例方法同构，供只读 full preflight。"""
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


def _snapshot_row_versions_supported(conn: sqlite3.Connection) -> bool:
    try:
        versions = {
            int(row[0])
            for row in conn.execute(
                "SELECT DISTINCT snapshot_schema_version FROM runtime_snapshots"
            )
        }
    except sqlite3.Error:
        return False
    return versions <= _SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS


def snapshot_preflight(
    db_path: str, *, mode: PreflightMode = PreflightMode.STARTUP
) -> PersistencePreflightResult:
    """Read-only Snapshot preflight（仅在 enabled 时由 Coordinator 调用）。

    无 migration；未知版本 / 未知 shape fail closed；FULL 模式逐 row 校验
    v1 envelope/digest。绝不写回、绝不创建 v0、绝不触发 Recovery execution。
    """
    if not os.path.exists(db_path):
        return PersistencePreflightResult(
            store_id=StoreId.SNAPSHOT,
            status=PreflightStatus.NEW,
            action=MigrationAction.INITIALIZE,
            detected_version="absent",
            target_version="1",
        )
    try:
        sqlite_quick_check(db_path)
    except PersistenceError:
        return PersistencePreflightResult(
            store_id=StoreId.SNAPSHOT,
            status=PreflightStatus.FAILED,
            action=MigrationAction.NONE,
            detected_version="unknown",
            safe_error_code=PERSISTENCE_PREFLIGHT_FAILED,
        )
    try:
        conn = open_read_only(db_path)
    except PersistenceError:
        return PersistencePreflightResult(
            store_id=StoreId.SNAPSHOT,
            status=PreflightStatus.FAILED,
            action=MigrationAction.NONE,
            detected_version="unknown",
            safe_error_code=PERSISTENCE_PREFLIGHT_FAILED,
        )
    try:
        if not _snapshot_shape_current(conn):
            return PersistencePreflightResult(
                store_id=StoreId.SNAPSHOT,
                status=PreflightStatus.UNSUPPORTED,
                action=MigrationAction.NONE,
                detected_version="unknown",
                target_version="1",
                safe_error_code=PERSISTENCE_SCHEMA_UNSUPPORTED,
            )
        if not _snapshot_row_versions_supported(conn):
            return PersistencePreflightResult(
                store_id=StoreId.SNAPSHOT,
                status=PreflightStatus.UNSUPPORTED,
                action=MigrationAction.NONE,
                detected_version="unsupported-version",
                target_version="1",
                safe_error_code=PERSISTENCE_SCHEMA_UNSUPPORTED,
            )
        if mode is PreflightMode.FULL:
            rows = conn.execute(
                "SELECT * FROM runtime_snapshots"
            ).fetchall()
            for row in rows:
                _snapshot_row_to_value(row)
        return PersistencePreflightResult(
            store_id=StoreId.SNAPSHOT,
            status=PreflightStatus.CURRENT,
            action=MigrationAction.NONE,
            detected_version="1",
            target_version="1",
        )
    except SnapshotStoreError as exc:
        if exc.error_code is SnapshotErrorCode.SNAPSHOT_SCHEMA_UNSUPPORTED:
            return PersistencePreflightResult(
                store_id=StoreId.SNAPSHOT,
                status=PreflightStatus.UNSUPPORTED,
                action=MigrationAction.NONE,
                detected_version="unsupported-version",
                target_version="1",
                safe_error_code=PERSISTENCE_SCHEMA_UNSUPPORTED,
            )
        return PersistencePreflightResult(
            store_id=StoreId.SNAPSHOT,
            status=PreflightStatus.FAILED,
            action=MigrationAction.NONE,
            detected_version="1",
            safe_error_code=PERSISTENCE_PREFLIGHT_FAILED,
        )
    except sqlite3.Error:
        return PersistencePreflightResult(
            store_id=StoreId.SNAPSHOT,
            status=PreflightStatus.FAILED,
            action=MigrationAction.NONE,
            detected_version="unknown",
            safe_error_code=PERSISTENCE_PREFLIGHT_FAILED,
        )
    finally:
        conn.close()


__all__ = [
    "InMemorySnapshotStore",
    "MAX_SNAPSHOT_LIST_LIMIT",
    "SQLiteSnapshotStore",
    "SnapshotErrorCode",
    "SnapshotSaveStatus",
    "SnapshotStore",
    "SnapshotStoreError",
]
