#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""基于 Event ID checkpoint 的幂等消费基础设施。"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from core.runtime.event_journal import JournalRecord


class EventConsumptionStatus(str, Enum):
    PROCESSED = "PROCESSED"
    DUPLICATE = "DUPLICATE"


class ConsumerErrorCode(str, Enum):
    OUT_OF_ORDER = "OUT_OF_ORDER"
    CHECKPOINT_CONFLICT = "CHECKPOINT_CONFLICT"
    CHECKPOINT_STORE_FAILED = "CHECKPOINT_STORE_FAILED"


class EventConsumerError(Exception):
    """不包含 Payload、SQL 或本地路径的安全 Consumer 错误。"""

    def __init__(self, error_code: ConsumerErrorCode, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code.value})")


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")


def _require_positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} 必须是正整数且不能是 bool")


@dataclass(frozen=True, slots=True)
class EventConsumptionCheckpoint:
    consumer_id: str
    event_id: str
    run_id: str
    sequence: int
    processed_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.consumer_id, "consumer_id")
        _require_text(self.event_id, "event_id")
        _require_text(self.run_id, "run_id")
        _require_positive_int(self.sequence, "sequence")
        if (
            not isinstance(self.processed_at, datetime)
            or self.processed_at.tzinfo is None
            or self.processed_at.utcoffset() is None
            or self.processed_at.utcoffset().total_seconds() != 0
        ):
            raise ValueError("processed_at 必须是 timezone-aware UTC")


class EventConsumptionCheckpointStore(Protocol):
    def get(
        self, consumer_id: str, event_id: str
    ) -> EventConsumptionCheckpoint | None: ...

    def last_sequence(self, consumer_id: str, run_id: str) -> int | None: ...

    def save(self, checkpoint: EventConsumptionCheckpoint) -> None: ...

    def processing_lock(self, consumer_id: str, run_id: str) -> asyncio.Lock: ...

    def close(self) -> None: ...


class _ProcessingLocks:
    def __init__(self) -> None:
        self._processing_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._processing_locks_guard = threading.Lock()

    def processing_lock(self, consumer_id: str, run_id: str) -> asyncio.Lock:
        _require_text(consumer_id, "consumer_id")
        _require_text(run_id, "run_id")
        key = (consumer_id, run_id)
        with self._processing_locks_guard:
            lock = self._processing_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._processing_locks[key] = lock
            return lock


class InMemoryEventConsumptionCheckpointStore(_ProcessingLocks):
    def __init__(self) -> None:
        super().__init__()
        self._checkpoints: dict[
            tuple[str, str], EventConsumptionCheckpoint
        ] = {}
        self._lock = threading.RLock()
        self._closed = False

    def get(
        self, consumer_id: str, event_id: str
    ) -> EventConsumptionCheckpoint | None:
        _require_text(consumer_id, "consumer_id")
        _require_text(event_id, "event_id")
        with self._lock:
            self._ensure_open()
            return self._checkpoints.get((consumer_id, event_id))

    def last_sequence(self, consumer_id: str, run_id: str) -> int | None:
        _require_text(consumer_id, "consumer_id")
        _require_text(run_id, "run_id")
        with self._lock:
            self._ensure_open()
            values = [
                checkpoint.sequence
                for checkpoint in self._checkpoints.values()
                if checkpoint.consumer_id == consumer_id
                and checkpoint.run_id == run_id
            ]
            return max(values, default=None)

    def save(self, checkpoint: EventConsumptionCheckpoint) -> None:
        if not isinstance(checkpoint, EventConsumptionCheckpoint):
            raise TypeError("checkpoint 必须是 EventConsumptionCheckpoint")
        with self._lock:
            self._ensure_open()
            key = (checkpoint.consumer_id, checkpoint.event_id)
            existing = self._checkpoints.get(key)
            if existing is not None:
                if (
                    existing.run_id == checkpoint.run_id
                    and existing.sequence == checkpoint.sequence
                ):
                    return
                raise EventConsumerError(
                    ConsumerErrorCode.CHECKPOINT_CONFLICT,
                    "Consumer checkpoint 的 event_id 冲突",
                )
            for item in self._checkpoints.values():
                if (
                    item.consumer_id == checkpoint.consumer_id
                    and item.run_id == checkpoint.run_id
                    and item.sequence == checkpoint.sequence
                ):
                    raise EventConsumerError(
                        ConsumerErrorCode.CHECKPOINT_CONFLICT,
                        "Consumer checkpoint 的 sequence 冲突",
                    )
            self._checkpoints[key] = checkpoint

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise EventConsumerError(
                ConsumerErrorCode.CHECKPOINT_STORE_FAILED,
                "Checkpoint Store 已关闭",
            )


class SQLiteEventConsumptionCheckpointStore(_ProcessingLocks):
    """本地 SQLite checkpoint；不存储 Event Payload。"""

    def __init__(self, db_path: str) -> None:
        super().__init__()
        if not isinstance(db_path, str) or not db_path.strip():
            raise ValueError("db_path 必须是非空字符串")
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._connection = sqlite3.connect(
                db_path,
                check_same_thread=False,
                isolation_level=None,
                timeout=30.0,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS event_consumption_checkpoint (
                    consumer_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY (consumer_id, event_id),
                    UNIQUE (consumer_id, run_id, sequence)
                )
                """
            )
        except sqlite3.Error as exc:
            raise EventConsumerError(
                ConsumerErrorCode.CHECKPOINT_STORE_FAILED,
                "SQLite Checkpoint Store 初始化失败",
            ) from exc

    def get(
        self, consumer_id: str, event_id: str
    ) -> EventConsumptionCheckpoint | None:
        _require_text(consumer_id, "consumer_id")
        _require_text(event_id, "event_id")
        with self._lock:
            self._ensure_open()
            try:
                row = self._connection.execute(
                    """
                    SELECT consumer_id, event_id, run_id, sequence, processed_at
                    FROM event_consumption_checkpoint
                    WHERE consumer_id = ? AND event_id = ?
                    """,
                    (consumer_id, event_id),
                ).fetchone()
                return self._from_row(row) if row is not None else None
            except (sqlite3.Error, ValueError, TypeError) as exc:
                raise EventConsumerError(
                    ConsumerErrorCode.CHECKPOINT_STORE_FAILED,
                    "SQLite Checkpoint Store 读取失败",
                ) from exc

    def last_sequence(self, consumer_id: str, run_id: str) -> int | None:
        _require_text(consumer_id, "consumer_id")
        _require_text(run_id, "run_id")
        with self._lock:
            self._ensure_open()
            try:
                row = self._connection.execute(
                    """
                    SELECT MAX(sequence) AS last_sequence
                    FROM event_consumption_checkpoint
                    WHERE consumer_id = ? AND run_id = ?
                    """,
                    (consumer_id, run_id),
                ).fetchone()
                if row is None or row["last_sequence"] is None:
                    return None
                return int(row["last_sequence"])
            except (sqlite3.Error, ValueError, TypeError) as exc:
                raise EventConsumerError(
                    ConsumerErrorCode.CHECKPOINT_STORE_FAILED,
                    "SQLite Checkpoint Store 读取失败",
                ) from exc

    def save(self, checkpoint: EventConsumptionCheckpoint) -> None:
        if not isinstance(checkpoint, EventConsumptionCheckpoint):
            raise TypeError("checkpoint 必须是 EventConsumptionCheckpoint")
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute(
                    """
                    INSERT INTO event_consumption_checkpoint (
                        consumer_id, event_id, run_id, sequence, processed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint.consumer_id,
                        checkpoint.event_id,
                        checkpoint.run_id,
                        checkpoint.sequence,
                        checkpoint.processed_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError:
                existing = self.get(checkpoint.consumer_id, checkpoint.event_id)
                if (
                    existing is not None
                    and existing.run_id == checkpoint.run_id
                    and existing.sequence == checkpoint.sequence
                ):
                    return
                raise EventConsumerError(
                    ConsumerErrorCode.CHECKPOINT_CONFLICT,
                    "SQLite Consumer checkpoint 冲突",
                ) from None
            except sqlite3.Error as exc:
                raise EventConsumerError(
                    ConsumerErrorCode.CHECKPOINT_STORE_FAILED,
                    "SQLite Checkpoint Store 写入失败",
                ) from exc

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._connection.close()
            except sqlite3.Error:
                return

    @staticmethod
    def _from_row(row: sqlite3.Row) -> EventConsumptionCheckpoint:
        return EventConsumptionCheckpoint(
            consumer_id=str(row["consumer_id"]),
            event_id=str(row["event_id"]),
            run_id=str(row["run_id"]),
            sequence=int(row["sequence"]),
            processed_at=datetime.fromisoformat(str(row["processed_at"])),
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise EventConsumerError(
                ConsumerErrorCode.CHECKPOINT_STORE_FAILED,
                "Checkpoint Store 已关闭",
            )


EventHandler = Callable[[JournalRecord], object | Awaitable[object]]


class IdempotentEventConsumer:
    """提供 Event ID 幂等消费基础，但不保证业务副作用 Exactly-once。

    Handler 成功后、Checkpoint 保存前仍存在崩溃窗口，因此 Handler 必须
    自身幂等，或未来与业务状态使用同一个事务。
    """

    def __init__(
        self,
        *,
        consumer_id: str,
        checkpoint_store: EventConsumptionCheckpointStore,
        handler: EventHandler,
    ) -> None:
        _require_text(consumer_id, "consumer_id")
        if not callable(handler):
            raise TypeError("handler 必须可调用")
        self.consumer_id = consumer_id
        self.checkpoint_store = checkpoint_store
        self.handler = handler

    async def consume(
        self, record: JournalRecord
    ) -> EventConsumptionStatus:
        if not isinstance(record, JournalRecord):
            raise TypeError("consume 只接受 JournalRecord")
        record.verify()
        lock = self.checkpoint_store.processing_lock(
            self.consumer_id, record.run_id
        )
        async with lock:
            existing = self.checkpoint_store.get(
                self.consumer_id, record.event_id
            )
            if existing is not None:
                if (
                    existing.run_id != record.run_id
                    or existing.sequence != record.sequence
                ):
                    raise EventConsumerError(
                        ConsumerErrorCode.CHECKPOINT_CONFLICT,
                        "Duplicate Event 与既有 checkpoint 不一致",
                    )
                return EventConsumptionStatus.DUPLICATE
            last_sequence = self.checkpoint_store.last_sequence(
                self.consumer_id, record.run_id
            )
            if last_sequence is not None and record.sequence <= last_sequence:
                raise EventConsumerError(
                    ConsumerErrorCode.OUT_OF_ORDER,
                    "未知 Event 的 sequence 低于或等于消费进度",
                )
            result = self.handler(record)
            if inspect.isawaitable(result):
                await result
            self.checkpoint_store.save(
                EventConsumptionCheckpoint(
                    consumer_id=self.consumer_id,
                    event_id=record.event_id,
                    run_id=record.run_id,
                    sequence=record.sequence,
                    processed_at=datetime.now(UTC),
                )
            )
            return EventConsumptionStatus.PROCESSED
