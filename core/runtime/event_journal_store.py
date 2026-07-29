#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run Event Journal 的内存与 SQLite append-only 实现。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from core.runtime.event_journal import (
    JOURNAL_SCHEMA_VERSION,
    JournalAppendStatus,
    JournalError,
    JournalErrorCode,
    JournalRecord,
    canonical_json,
    validate_read_arguments,
)
from core.runtime.events import RuntimeEvent, RuntimeEventType
from core.runtime.observability import (
    NoopRuntimeInfrastructureMetricsHook,
    RuntimeInfrastructureMetricsHook,
)


_TERMINAL_EVENT_TYPE = RuntimeEventType.RUN_COMPLETED


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
            and existing_id.event_digest == record.event_digest
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


class SQLiteRunEventJournal:
    """基于单个本地 SQLite 文件的事务型 append-only Journal。"""

    def __init__(
        self,
        db_path: str,
        *,
        metrics_hook: RuntimeInfrastructureMetricsHook | None = None,
    ) -> None:
        if not isinstance(db_path, str) or not db_path.strip():
            raise ValueError("db_path 必须是非空字符串")
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._metrics_hook = metrics_hook or NoopRuntimeInfrastructureMetricsHook()
        try:
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
                CREATE TABLE IF NOT EXISTS runtime_event_journal (
                    journal_schema_version INTEGER NOT NULL,
                    event_schema_version INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    emitted_at TEXT NOT NULL,
                    journaled_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    component TEXT NOT NULL,
                    step_id TEXT,
                    step_sequence INTEGER,
                    span_id TEXT,
                    parent_span_id TEXT,
                    safe_payload TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    event_digest TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                )
                """
            )
            columns = {row[1] for row in self._connection.execute("PRAGMA table_info(runtime_event_journal)")}
            for column in ("span_id", "parent_span_id"):
                if column not in columns:
                    self._connection.execute(f"ALTER TABLE runtime_event_journal ADD COLUMN {column} TEXT")
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_runtime_event_journal_run_type
                ON runtime_event_journal(run_id, event_type)
                """
            )
        except sqlite3.Error as exc:
            raise JournalError(
                JournalErrorCode.JOURNAL_APPEND_FAILED,
                "SQLite Journal 初始化失败",
            ) from exc

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
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing_id = self._select_one(
                    "SELECT * FROM runtime_event_journal WHERE event_id = ?",
                    (record.event_id,),
                )
                existing_sequence = self._select_one(
                    """
                    SELECT * FROM runtime_event_journal
                    WHERE run_id = ? AND sequence = ?
                    """,
                    (record.run_id, record.sequence),
                )
                last_row = self._connection.execute(
                    """
                    SELECT MAX(sequence) AS last_sequence
                    FROM runtime_event_journal WHERE run_id = ?
                    """,
                    (record.run_id,),
                ).fetchone()
                terminal_row = self._connection.execute(
                    """
                    SELECT sequence FROM runtime_event_journal
                    WHERE run_id = ? AND event_type = ?
                    ORDER BY sequence LIMIT 1
                    """,
                    (record.run_id, _TERMINAL_EVENT_TYPE.value),
                ).fetchone()
                decision = _append_decision(
                    record,
                    existing_id=(
                        self._record_from_row(existing_id)
                        if existing_id is not None
                        else None
                    ),
                    existing_sequence=(
                        self._record_from_row(existing_sequence)
                        if existing_sequence is not None
                        else None
                    ),
                    last_sequence=(
                        int(last_row["last_sequence"])
                        if last_row is not None
                        and last_row["last_sequence"] is not None
                        else None
                    ),
                    terminal_sequence=(
                        int(terminal_row["sequence"])
                        if terminal_row is not None
                        else None
                    ),
                )
                if decision is not None:
                    self._connection.execute("COMMIT")
                    return decision
                self._connection.execute(
                    """
                    INSERT INTO runtime_event_journal (
                        journal_schema_version, event_schema_version, event_id,
                        run_id, trace_id, sequence, emitted_at, journaled_at,
                        event_type, component, step_id, step_sequence, span_id, parent_span_id,
                        safe_payload, payload_digest, event_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.journal_schema_version,
                        record.event_schema_version,
                        record.event_id,
                        record.run_id,
                        record.trace_id,
                        record.sequence,
                        record.emitted_at.isoformat(),
                        record.journaled_at.isoformat(),
                        record.event_type.value,
                        record.component,
                        record.step_id,
                        record.step_sequence,
                        record.span_id,
                        record.parent_span_id,
                        canonical_json(record.safe_payload),
                        record.payload_digest,
                        record.event_digest,
                    ),
                )
                self._connection.execute("COMMIT")
                return JournalAppendStatus.APPENDED
            except JournalError:
                self._rollback_safely()
                raise
            except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
                self._rollback_safely()
                raise JournalError(
                    JournalErrorCode.JOURNAL_APPEND_FAILED,
                    "SQLite Journal 追加失败",
                ) from exc

    def read_after(
        self, run_id: str, sequence: int, limit: int
    ) -> tuple[JournalRecord, ...]:
        validate_read_arguments(run_id, sequence, limit)
        with self._lock:
            self._ensure_open()
            try:
                self._verify_terminal_invariant(run_id)
                rows = self._connection.execute(
                    """
                    SELECT * FROM runtime_event_journal
                    WHERE run_id = ? AND sequence > ?
                    ORDER BY sequence ASC LIMIT ?
                    """,
                    (run_id, sequence, limit),
                ).fetchall()
                return tuple(self._record_from_row(row) for row in rows)
            except JournalError:
                raise
            except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise JournalError(
                    JournalErrorCode.JOURNAL_CORRUPTED,
                    "SQLite Journal 读取或校验失败",
                ) from exc

    def get_by_event_id(self, event_id: str) -> JournalRecord | None:
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("event_id 必须是非空字符串")
        with self._lock:
            self._ensure_open()
            try:
                row = self._select_one(
                    "SELECT * FROM runtime_event_journal WHERE event_id = ?",
                    (event_id,),
                )
                return self._record_from_row(row) if row is not None else None
            except JournalError:
                raise
            except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise JournalError(
                    JournalErrorCode.JOURNAL_CORRUPTED,
                    "SQLite Journal 读取或校验失败",
                ) from exc

    def last_sequence(self, run_id: str) -> int | None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id 必须是非空字符串")
        with self._lock:
            self._ensure_open()
            try:
                self._verify_terminal_invariant(run_id)
                row = self._connection.execute(
                    """
                    SELECT MAX(sequence) AS last_sequence
                    FROM runtime_event_journal WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
                if row is None or row["last_sequence"] is None:
                    return None
                return int(row["last_sequence"])
            except JournalError:
                raise
            except (sqlite3.Error, ValueError, TypeError) as exc:
                raise JournalError(
                    JournalErrorCode.JOURNAL_CORRUPTED,
                    "SQLite Journal 读取或校验失败",
                ) from exc

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._connection.close()
            except sqlite3.Error:
                # close 是幂等清理边界，不向外暴露本地数据库细节。
                return

    def _select_one(
        self, statement: str, parameters: tuple[object, ...]
    ) -> sqlite3.Row | None:
        return self._connection.execute(statement, parameters).fetchone()

    def _record_from_row(self, row: sqlite3.Row) -> JournalRecord:
        try:
            record = JournalRecord(
                journal_schema_version=int(row["journal_schema_version"]),
                event_schema_version=int(row["event_schema_version"]),
                event_id=str(row["event_id"]),
                run_id=str(row["run_id"]),
                trace_id=str(row["trace_id"]),
                sequence=int(row["sequence"]),
                emitted_at=datetime.fromisoformat(str(row["emitted_at"])),
                journaled_at=datetime.fromisoformat(str(row["journaled_at"])),
                event_type=RuntimeEventType(str(row["event_type"])),
                component=str(row["component"]),
                step_id=(
                    str(row["step_id"]) if row["step_id"] is not None else None
                ),
                step_sequence=(
                    int(row["step_sequence"])
                    if row["step_sequence"] is not None
                    else None
                ),
                span_id=(str(row["span_id"]) if row["span_id"] is not None else None),
                parent_span_id=(str(row["parent_span_id"]) if row["parent_span_id"] is not None else None),
                safe_payload=json.loads(str(row["safe_payload"])),
                payload_digest=str(row["payload_digest"]),
                event_digest=str(row["event_digest"]),
            )
            record.verify()
            return record
        except JournalError:
            raise
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise JournalError(
                JournalErrorCode.JOURNAL_CORRUPTED,
                "SQLite Journal 记录结构损坏",
            ) from exc

    def _verify_terminal_invariant(self, run_id: str) -> None:
        rows = self._connection.execute(
            """
            SELECT sequence FROM runtime_event_journal
            WHERE run_id = ? AND event_type = ?
            ORDER BY sequence
            """,
            (run_id, _TERMINAL_EVENT_TYPE.value),
        ).fetchall()
        if len(rows) > 1:
            raise JournalError(
                JournalErrorCode.JOURNAL_CORRUPTED,
                "SQLite Journal 包含多个 Run 终态事件",
            )
        if rows:
            last_row = self._connection.execute(
                """
                SELECT MAX(sequence) AS last_sequence
                FROM runtime_event_journal WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if (
                last_row is None
                or int(rows[0]["sequence"]) != int(last_row["last_sequence"])
            ):
                raise JournalError(
                    JournalErrorCode.JOURNAL_CORRUPTED,
                    "SQLite Journal 的 Run 终态不是最后事件",
                )

    def _ensure_open(self) -> None:
        if self._closed:
            raise JournalError(
                JournalErrorCode.JOURNAL_APPEND_FAILED,
                "Journal 已关闭",
            )

    def _rollback_safely(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:
            return
