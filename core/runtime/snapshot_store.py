#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Append-only in-memory and PostgreSQL stores for verified RunSnapshot values."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import threading
from typing import Protocol

from core.persistence.database import Database
from core.persistence.errors import PersistenceError as DatabasePersistenceError
from core.persistence.repositories import runtime as snapshot_repository
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
    async def save(self, snapshot: RunSnapshot) -> SnapshotSaveStatus: ...

    async def get(self, snapshot_id: str) -> RunSnapshot | None: ...

    async def latest(self, run_id: str) -> RunSnapshot | None: ...

    async def list_for_run(
        self, run_id: str, limit: int
    ) -> tuple[RunSnapshot, ...]: ...

    async def close(self) -> None: ...


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


def snapshot_row_to_value(row: object) -> RunSnapshot:
    """PostgreSQL row → RunSnapshot；envelope 不一致时 fail closed。"""
    try:
        version = row.snapshot_schema_version
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("invalid schema version")
        snapshot = snapshot_from_json(str(row.payload_json))
        created_at = row.created_at
        if not isinstance(created_at, datetime):
            raise ValueError("invalid created_at")
        if (
            snapshot.snapshot_schema_version != version
            or snapshot.snapshot_id != str(row.snapshot_id)
            or snapshot.run_id != str(row.run_id)
            or snapshot.created_at != created_at
            or snapshot.payload_digest != str(row.payload_digest)
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


class PostgresSnapshotStore:
    """PostgreSQL Canonical Snapshot store。

    Snapshot 语义不变：仍是**持久检查点证据**，不是 automatic active-run
    recovery authority。identity / version / digest / immutable evidence /
    read semantics / compatibility rejection 全部保留。

    Transaction Owner 是本 Store：``save`` 的 read-then-insert 在同一事务内。
    """

    def __init__(self, database: "Database") -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database
        self._closed = False

    async def save(self, snapshot: RunSnapshot) -> SnapshotSaveStatus:
        verified = _verify_for_store(snapshot)
        payload_json = snapshot_to_json(verified)
        self._ensure_open()
        try:
            async with self._database.transaction() as session:
                existing_row = await snapshot_repository.select_snapshot_by_id(
                    session, verified.snapshot_id
                )
                if existing_row is not None:
                    # 先验证已存字节，再判定 duplicate / conflict。
                    existing = snapshot_row_to_value(existing_row)
                    if existing.payload_digest == verified.payload_digest:
                        return SnapshotSaveStatus.DUPLICATE
                    raise SnapshotStoreError(
                        SnapshotErrorCode.SNAPSHOT_ID_CONFLICT
                    )
                await snapshot_repository.insert_snapshot_row(
                    session,
                    {
                        "snapshot_schema_version": verified.snapshot_schema_version,
                        "snapshot_id": verified.snapshot_id,
                        "run_id": verified.run_id,
                        "created_at": verified.created_at,
                        "payload_json": payload_json,
                        "payload_digest": verified.payload_digest,
                    },
                )
                return SnapshotSaveStatus.SAVED
        except SnapshotStoreError:
            raise
        except DatabasePersistenceError:
            raise
        except Exception:
            raise SnapshotStoreError(
                SnapshotErrorCode.SNAPSHOT_STORE_FAILED
            ) from None

    async def get(self, snapshot_id: str) -> RunSnapshot | None:
        _validate_id(snapshot_id, "snapshot_id")
        self._ensure_open()
        try:
            async with self._database.session() as session:
                row = await snapshot_repository.select_snapshot_by_id(
                    session, snapshot_id
                )
                return snapshot_row_to_value(row) if row is not None else None
        except SnapshotStoreError:
            raise
        except DatabasePersistenceError:
            raise
        except Exception:
            raise SnapshotStoreError(
                SnapshotErrorCode.SNAPSHOT_STORE_FAILED
            ) from None

    async def latest(self, run_id: str) -> RunSnapshot | None:
        _validate_id(run_id, "run_id")
        self._ensure_open()
        try:
            async with self._database.session() as session:
                row = await snapshot_repository.select_latest_snapshot(
                    session, run_id
                )
                return snapshot_row_to_value(row) if row is not None else None
        except SnapshotStoreError:
            raise
        except DatabasePersistenceError:
            raise
        except Exception:
            raise SnapshotStoreError(
                SnapshotErrorCode.SNAPSHOT_STORE_FAILED
            ) from None

    async def list_for_run(
        self, run_id: str, limit: int
    ) -> tuple[RunSnapshot, ...]:
        _validate_id(run_id, "run_id")
        _validate_limit(limit)
        self._ensure_open()
        try:
            async with self._database.session() as session:
                rows = await snapshot_repository.select_snapshots_for_run(
                    session, run_id, limit
                )
                return tuple(snapshot_row_to_value(row) for row in rows)
        except SnapshotStoreError:
            raise
        except DatabasePersistenceError:
            raise
        except Exception:
            raise SnapshotStoreError(
                SnapshotErrorCode.SNAPSHOT_STORE_FAILED
            ) from None

    async def close(self) -> None:
        # Engine/Pool 的 Owner 是 application lifecycle，不是本 Store。
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise SnapshotStoreError(SnapshotErrorCode.SNAPSHOT_STORE_FAILED)


__all__ = [
    "InMemorySnapshotStore",
    "MAX_SNAPSHOT_LIST_LIMIT",
    "PostgresSnapshotStore",
    "SnapshotErrorCode",
    "SnapshotSaveStatus",
    "SnapshotStore",
    "SnapshotStoreError",
    "snapshot_row_to_value",
]
