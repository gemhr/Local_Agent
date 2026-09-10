#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run Event Journal 的内存与 PostgreSQL append-only 实现。

Runtime Contract 在换库后保持不变：Journal-first、``(run_id, sequence)``
identity、``event_id`` 唯一、sequence 单调、terminal 不变量、重复幂等识别、
冲突拒绝、canonical digest、安全 payload 边界。

PostgreSQL 并发实现不再复制 SQLite 的 ``BEGIN IMMEDIATE`` 与进程内
``RLock``，而是：

* 用 run 级 ``pg_advisory_xact_lock`` 把同一 Run 的 read-decide-insert
  串行化（锁范围显式且单键，无死锁序）；
* 用 ``(run_id, sequence)`` 主键与 ``event_id`` 唯一约束作为最终 Authority；
* 用 ``event_type = 'RUN_COMPLETED'`` 的 partial unique index 在数据库层
  强制 terminal 唯一。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from core.persistence.database import Database
from core.persistence.errors import DatabaseErrorCode, PersistenceError
from core.persistence.repositories import runtime as runtime_repository
from core.runtime.event_journal import (
    JOURNAL_SCHEMA_VERSION,
    JournalAppendStatus,
    JournalError,
    JournalErrorCode,
    JournalRecord,
    canonical_json,
    validate_read_arguments,
)
from core.runtime.events import (
    RuntimeEvent,
    RuntimeEventType,
)
from core.runtime.observability import (
    NoopRuntimeInfrastructureMetricsHook,
    RuntimeInfrastructureMetricsHook,
)


_TERMINAL_EVENT_TYPE = RuntimeEventType.RUN_COMPLETED

# 约束兜底重试次数：advisory lock 已串行化同一 Run，此重试只覆盖「不同 Run
# 之间 event_id 撞车」这一类跨 Run 竞态。
_MAX_CONSTRAINT_RETRIES = 1


def _append_decision(
    record: JournalRecord,
    *,
    existing_id: JournalRecord | None,
    existing_sequence: JournalRecord | None,
    last_sequence: int | None,
    terminal_sequence: int | None,
) -> JournalAppendStatus | None:
    if existing_id is not None:
        existing_id.verify()
        if (
            existing_id.run_id == record.run_id
            and existing_id.sequence == record.sequence
            and existing_id.is_duplicate_of(record)
        ):
            return JournalAppendStatus.DUPLICATE
        raise JournalError(
            JournalErrorCode.EVENT_ID_CONFLICT,
            "相同 event_id 对应了不同事件内容",
        )
    if existing_sequence is not None:
        existing_sequence.verify()
        raise JournalError(
            JournalErrorCode.SEQUENCE_CONFLICT,
            "Run sequence 已被其他事件占用",
        )
    if terminal_sequence is not None:
        raise JournalError(
            JournalErrorCode.RUN_ALREADY_TERMINAL,
            "Run 已存在终态事件，不能继续追加",
        )
    if last_sequence is not None and record.sequence <= last_sequence:
        raise JournalError(
            JournalErrorCode.OUT_OF_ORDER,
            "未知事件的 sequence 低于或等于当前最大值",
        )
    return None


class InMemoryRunEventJournal:
    """线程安全的进程内 Journal（测试 / 本地装配）；不执行任何 I/O。

    它保持**同步**接口：只有 PostgreSQL Canonical Store 必须是 Awaitable。
    消费者通过 ``core.runtime.awaitable_compat.resolve`` 同时支持两者。
    """

    """线程安全的进程内 Journal，主要用于测试与本地装配。"""

    def __init__(
        self,
        *,
        metrics_hook: RuntimeInfrastructureMetricsHook | None = None,
    ) -> None:
        self._records_by_id: dict[str, JournalRecord] = {}
        self._records_by_run: dict[str, dict[int, JournalRecord]] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._metrics_hook = metrics_hook or NoopRuntimeInfrastructureMetricsHook()

    def append(self, event: RuntimeEvent) -> JournalAppendStatus:
        started = time.perf_counter()
        try:
            status = self._append_impl(event)
        except Exception:
            try:
                self._metrics_hook.journal_append_failed(
                    duration_seconds=time.perf_counter() - started
                )
            except Exception:
                pass
            raise
        try:
            self._metrics_hook.journal_append_succeeded(
                duration_seconds=time.perf_counter() - started,
                duplicate=status is JournalAppendStatus.DUPLICATE,
            )
        except Exception:
            pass
        return status

    def _append_impl(self, event: RuntimeEvent) -> JournalAppendStatus:
        try:
            record = JournalRecord.from_event(event)
        except JournalError:
            raise
        except Exception as exc:
            raise JournalError(
                JournalErrorCode.JOURNAL_APPEND_FAILED,
                "Runtime Event 无法安全写入 Journal",
            ) from exc
        with self._lock:
            self._ensure_open()
            run_records = self._records_by_run.get(record.run_id, {})
            terminal = next(
                (
                    item.sequence
                    for item in run_records.values()
                    if item.event_type == _TERMINAL_EVENT_TYPE
                ),
                None,
            )
            decision = _append_decision(
                record,
                existing_id=self._records_by_id.get(record.event_id),
                existing_sequence=run_records.get(record.sequence),
                last_sequence=max(run_records, default=None),
                terminal_sequence=terminal,
            )
            if decision is not None:
                return decision
            run_records = self._records_by_run.setdefault(record.run_id, {})
            run_records[record.sequence] = record
            self._records_by_id[record.event_id] = record
            return JournalAppendStatus.APPENDED

    def read_after(
        self, run_id: str, sequence: int, limit: int
    ) -> tuple[JournalRecord, ...]:
        validate_read_arguments(run_id, sequence, limit)
        with self._lock:
            self._ensure_open()
            self._verify_run(run_id)
            records = self._records_by_run.get(run_id, {})
            result = tuple(
                records[index]
                for index in sorted(index for index in records if index > sequence)[
                    :limit
                ]
            )
            for record in result:
                record.verify()
            return result

    def get_by_event_id(self, event_id: str) -> JournalRecord | None:
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("event_id 必须是非空字符串")
        with self._lock:
            self._ensure_open()
            record = self._records_by_id.get(event_id)
            if record is not None:
                record.verify()
            return record

    def last_sequence(self, run_id: str) -> int | None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id 必须是非空字符串")
        with self._lock:
            self._ensure_open()
            self._verify_run(run_id)
            records = self._records_by_run.get(run_id, {})
            return max(records, default=None)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise JournalError(
                JournalErrorCode.JOURNAL_APPEND_FAILED,
                "Journal 已关闭",
            )

    def _verify_run(self, run_id: str) -> None:
        records = self._records_by_run.get(run_id, {})
        terminal_sequences = [
            record.sequence
            for record in records.values()
            if record.event_type == _TERMINAL_EVENT_TYPE
        ]
        if len(terminal_sequences) > 1 or (
            terminal_sequences and terminal_sequences[0] != max(records)
        ):
            raise JournalError(
                JournalErrorCode.JOURNAL_CORRUPTED,
                "Journal 的 Run 终态不变量被破坏",
            )
        for record in records.values():
            record.verify()


def journal_row_values(record: JournalRecord) -> dict[str, object]:
    """JournalRecord → PostgreSQL 列值。

    ``safe_payload`` 保存 canonical JSON **文本**：``event_digest`` 是该表的
    持久化身份算法，jsonb 的数值/键规范化会改变 digest source。
    """
    return {
        "journal_schema_version": record.journal_schema_version,
        "event_schema_version": record.event_schema_version,
        "event_id": record.event_id,
        "run_id": record.run_id,
        "trace_id": record.trace_id,
        "sequence": record.sequence,
        "emitted_at": record.emitted_at,
        "journaled_at": record.journaled_at,
        "event_type": record.event_type.value,
        "component": record.component,
        "step_id": record.step_id,
        "step_sequence": record.step_sequence,
        "span_id": record.span_id,
        "parent_span_id": record.parent_span_id,
        "safe_payload": canonical_json(record.safe_payload),
        "payload_digest": record.payload_digest,
        "event_digest": record.event_digest,
    }


def journal_record_from_row(row: Any) -> JournalRecord:
    """PostgreSQL row → JournalRecord；结构损坏时 fail closed。"""
    try:
        record = JournalRecord(
            journal_schema_version=int(row.journal_schema_version),
            event_schema_version=int(row.event_schema_version),
            event_id=str(row.event_id),
            run_id=str(row.run_id),
            trace_id=str(row.trace_id),
            sequence=int(row.sequence),
            emitted_at=_as_utc(row.emitted_at),
            journaled_at=_as_utc(row.journaled_at),
            event_type=RuntimeEventType(str(row.event_type)),
            component=str(row.component),
            step_id=(str(row.step_id) if row.step_id is not None else None),
            step_sequence=(
                int(row.step_sequence) if row.step_sequence is not None else None
            ),
            span_id=(str(row.span_id) if row.span_id is not None else None),
            parent_span_id=(
                str(row.parent_span_id)
                if row.parent_span_id is not None
                else None
            ),
            safe_payload=json.loads(str(row.safe_payload)),
            payload_digest=str(row.payload_digest),
            event_digest=str(row.event_digest),
        )
        record.verify()
        return record
    except JournalError:
        raise
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise JournalError(
            JournalErrorCode.JOURNAL_CORRUPTED,
            "PostgreSQL Journal 记录结构损坏",
        ) from exc


def _as_utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("Journal 时间列必须是 datetime")
    return value


class PostgresRunEventJournal:
    """PostgreSQL Canonical Run Event Journal（append-only，Async）。

    Transaction Owner 是本 Store：它持有 ``database.transaction()`` 并把
    narrow repository 调用放在同一事务内，保证 read-decide-insert 的原子性。
    """

    def __init__(
        self,
        database: Database,
        *,
        metrics_hook: RuntimeInfrastructureMetricsHook | None = None,
        statement_timeout_ms: int | None = None,
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database 必须是 Database")
        self._database = database
        self._metrics_hook = metrics_hook or NoopRuntimeInfrastructureMetricsHook()
        self._statement_timeout_ms = statement_timeout_ms
        self._closed = False

    async def append(self, event: RuntimeEvent) -> JournalAppendStatus:
        started = time.perf_counter()
        try:
            status = await self._append_with_constraint_retry(event)
        except Exception:
            try:
                self._metrics_hook.journal_append_failed(
                    duration_seconds=time.perf_counter() - started
                )
            except Exception:
                pass
            raise
        try:
            self._metrics_hook.journal_append_succeeded(
                duration_seconds=time.perf_counter() - started,
                duplicate=status is JournalAppendStatus.DUPLICATE,
            )
        except Exception:
            pass
        return status

    async def _append_with_constraint_retry(
        self, event: RuntimeEvent
    ) -> JournalAppendStatus:
        attempts = 0
        while True:
            try:
                return await self._append_once(event)
            except IntegrityError:
                # 唯一约束兜底：另一 Run 的并发写入抢先提交。重新读取后在
                # 新事务内重新判定，会得到正确的 DUPLICATE 或 typed conflict。
                attempts += 1
                if attempts > _MAX_CONSTRAINT_RETRIES:
                    raise JournalError(
                        JournalErrorCode.JOURNAL_APPEND_FAILED,
                        "Journal 追加被数据库约束拒绝",
                    ) from None

    async def _append_once(self, event: RuntimeEvent) -> JournalAppendStatus:
        try:
            record = JournalRecord.from_event(event)
        except JournalError:
            raise
        except Exception as exc:
            raise JournalError(
                JournalErrorCode.JOURNAL_APPEND_FAILED,
                "Runtime Event 无法安全写入 Journal",
            ) from exc
        self._ensure_open()
        try:
            async with self._database.transaction(
                statement_timeout_ms=self._statement_timeout_ms
            ) as session:
                await runtime_repository.lock_run_scope(session, record.run_id)
                existing_id_row = (
                    await runtime_repository.select_journal_by_event_id(
                        session, record.event_id
                    )
                )
                existing_sequence_row = (
                    await runtime_repository.select_journal_by_sequence(
                        session, record.run_id, record.sequence
                    )
                )
                last_sequence = (
                    await runtime_repository.select_last_journal_sequence(
                        session, record.run_id
                    )
                )
                terminal_sequence = (
                    await runtime_repository.select_terminal_sequence(
                        session,
                        record.run_id,
                        _TERMINAL_EVENT_TYPE.value,
                    )
                )
                decision = _append_decision(
                    record,
                    existing_id=(
                        journal_record_from_row(existing_id_row)
                        if existing_id_row is not None
                        else None
                    ),
                    existing_sequence=(
                        journal_record_from_row(existing_sequence_row)
                        if existing_sequence_row is not None
                        else None
                    ),
                    last_sequence=last_sequence,
                    terminal_sequence=terminal_sequence,
                )
                if decision is not None:
                    return decision
                await runtime_repository.insert_journal_row(
                    session, journal_row_values(record)
                )
                return JournalAppendStatus.APPENDED
        except JournalError:
            raise
        except IntegrityError:
            raise
        except PersistenceError:
            raise
        except Exception as exc:
            raise JournalError(
                JournalErrorCode.JOURNAL_APPEND_FAILED,
                "PostgreSQL Journal 追加失败",
            ) from exc

    async def read_after(
        self, run_id: str, sequence: int, limit: int
    ) -> tuple[JournalRecord, ...]:
        validate_read_arguments(run_id, sequence, limit)
        self._ensure_open()
        try:
            async with self._database.session() as session:
                await self._verify_terminal_invariant(session, run_id)
                rows = await runtime_repository.select_journal_page(
                    session, run_id, sequence, limit
                )
                return tuple(journal_record_from_row(row) for row in rows)
        except JournalError:
            raise
        except PersistenceError:
            raise
        except Exception as exc:
            raise JournalError(
                JournalErrorCode.JOURNAL_CORRUPTED,
                "PostgreSQL Journal 读取或校验失败",
            ) from exc

    async def get_by_event_id(self, event_id: str) -> JournalRecord | None:
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("event_id 必须是非空字符串")
        self._ensure_open()
        try:
            async with self._database.session() as session:
                row = await runtime_repository.select_journal_by_event_id(
                    session, event_id
                )
                return journal_record_from_row(row) if row is not None else None
        except JournalError:
            raise
        except PersistenceError:
            raise
        except Exception as exc:
            raise JournalError(
                JournalErrorCode.JOURNAL_CORRUPTED,
                "PostgreSQL Journal 读取或校验失败",
            ) from exc

    async def last_sequence(self, run_id: str) -> int | None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id 必须是非空字符串")
        self._ensure_open()
        try:
            async with self._database.session() as session:
                await self._verify_terminal_invariant(session, run_id)
                return await runtime_repository.select_last_journal_sequence(
                    session, run_id
                )
        except JournalError:
            raise
        except PersistenceError:
            raise
        except Exception as exc:
            raise JournalError(
                JournalErrorCode.JOURNAL_CORRUPTED,
                "PostgreSQL Journal 读取或校验失败",
            ) from exc

    async def close(self) -> None:
        # Engine/Pool 的 Owner 是 application lifecycle，不是本 Store。
        self._closed = True

    async def _verify_terminal_invariant(self, session, run_id: str) -> None:
        sequences = await runtime_repository.select_terminal_sequences(
            session, run_id, _TERMINAL_EVENT_TYPE.value
        )
        if len(sequences) > 1:
            raise JournalError(
                JournalErrorCode.JOURNAL_CORRUPTED,
                "PostgreSQL Journal 包含多个 Run 终态事件",
            )
        if not sequences:
            return
        last_sequence = await runtime_repository.select_last_journal_sequence(
            session, run_id
        )
        if last_sequence is None or sequences[0] != last_sequence:
            raise JournalError(
                JournalErrorCode.JOURNAL_CORRUPTED,
                "PostgreSQL Journal 的 Run 终态不是最后事件",
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise JournalError(
                JournalErrorCode.JOURNAL_APPEND_FAILED,
                "Journal 已关闭",
            )


__all__ = [
    "InMemoryRunEventJournal",
    "PostgresRunEventJournal",
    "journal_record_from_row",
    "journal_row_values",
]
