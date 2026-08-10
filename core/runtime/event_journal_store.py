#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run Event Journal 的内存与 SQLite append-only 实现。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from core.persistence_migration import (
    PERSISTENCE_MIGRATION_FAILED,
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
from core.runtime.event_journal import (
    JOURNAL_SCHEMA_VERSION,
    JournalAppendStatus,
    JournalError,
    JournalErrorCode,
    JournalRecord,
    SUPPORTED_JOURNAL_SCHEMA_VERSIONS,
    canonical_json,
    validate_read_arguments,
)
from core.runtime.events import (
    RUNTIME_EVENT_SCHEMA_VERSION,
    RuntimeEvent,
    RuntimeEventType,
)
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
            # WP1-D：constructor 不再对 existing legacy（缺 span 列）DB 隐式
            # ALTER；legacy physical shape 由 startup preflight 拦截为
            # MIGRATION_REQUIRED，只能由显式 SCRIPT_ROLE migrate 命令迁移。
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
        return _journal_record_from_row(row)

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


# ---------------------------------------------------------------------------
# WP1-D Journal Physical Preflight / Migration（Store-owned，Coordinator 编排）
# ---------------------------------------------------------------------------

# 每个条目 = (name, declared_type, notnull, dflt_value, pk_position)。
_JOURNAL_CURRENT_COLUMNS = (
    ("journal_schema_version", "INTEGER", 1, None, 0),
    ("event_schema_version", "INTEGER", 1, None, 0),
    ("event_id", "TEXT", 1, None, 0),
    ("run_id", "TEXT", 1, None, 1),
    ("trace_id", "TEXT", 1, None, 0),
    ("sequence", "INTEGER", 1, None, 2),
    ("emitted_at", "TEXT", 1, None, 0),
    ("journaled_at", "TEXT", 1, None, 0),
    ("event_type", "TEXT", 1, None, 0),
    ("component", "TEXT", 1, None, 0),
    ("step_id", "TEXT", 0, None, 0),
    ("step_sequence", "INTEGER", 0, None, 0),
    ("span_id", "TEXT", 0, None, 0),
    ("parent_span_id", "TEXT", 0, None, 0),
    ("safe_payload", "TEXT", 1, None, 0),
    ("payload_digest", "TEXT", 1, None, 0),
    ("event_digest", "TEXT", 1, None, 0),
)
_JOURNAL_LEGACY_COLUMNS = tuple(
    entry for entry in _JOURNAL_CURRENT_COLUMNS
    if entry[0] not in {"span_id", "parent_span_id"}
)
_JOURNAL_RUN_TYPE_INDEX = ("idx_runtime_event_journal_run_type", (0, 0, ("run_id", "event_type")))
_SUPPORTED_EVENT_SCHEMA_VERSIONS = frozenset(
    {1, RUNTIME_EVENT_SCHEMA_VERSION}
)


def _norm_type(value) -> str:
    return str(value).upper()


def _norm_default(value) -> object:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]
    return text.lower()


def _journal_table_columns_match(
    conn: sqlite3.Connection, expected: tuple
) -> bool:
    """按列名匹配（非物理顺序）：ALTER ADD COLUMN 追加列到表尾，物理顺序
    不是语义合同；类型/NOT NULL/DEFAULT/PK 位置按列名精确比较。"""
    try:
        actual = conn.execute(
            "PRAGMA table_info(runtime_event_journal)"
        ).fetchall()
    except sqlite3.Error:
        return False
    if len(actual) != len(expected):
        return False
    actual_by_name = {row[1]: row for row in actual}
    for name, type_, notnull, dflt, pk in expected:
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
    return True


def _journal_index_columns(conn: sqlite3.Connection, index_name: str) -> tuple:
    try:
        return tuple(
            r[2] for r in conn.execute(f"PRAGMA index_info({index_name})")
        )
    except sqlite3.Error:
        return ()


def _journal_pk_matches(conn: sqlite3.Connection) -> bool:
    """PRIMARY KEY(run_id, sequence) 必须是 origin='pk' 的 unique autoindex，
    且列序为 (run_id, sequence)。"""
    try:
        rows = conn.execute("PRAGMA index_list(runtime_event_journal)").fetchall()
    except sqlite3.Error:
        return False
    for row in rows:
        # (seq, name, unique, origin, partial)
        if row[3] == "pk" and int(row[2]) == 1:
            if _journal_index_columns(conn, row[1]) == ("run_id", "sequence"):
                return True
    return False


def _journal_event_id_unique_matches(conn: sqlite3.Connection) -> bool:
    """event_id UNIQUE 必须是 origin='u' 的 unique autoindex，列为 (event_id)。"""
    try:
        rows = conn.execute("PRAGMA index_list(runtime_event_journal)").fetchall()
    except sqlite3.Error:
        return False
    for row in rows:
        if row[3] == "u" and int(row[2]) == 1:
            if _journal_index_columns(conn, row[1]) == ("event_id",):
                return True
    return False


def _journal_run_type_index_matches(conn: sqlite3.Connection) -> bool:
    index_name, (unique, partial, columns) = _JOURNAL_RUN_TYPE_INDEX
    try:
        rows = conn.execute("PRAGMA index_list(runtime_event_journal)").fetchall()
    except sqlite3.Error:
        return False
    for row in rows:
        if row[1] != index_name:
            continue
        if int(row[2]) != unique or int(row[4]) != partial:
            return False
        return _journal_index_columns(conn, index_name) == columns
    return False


def _journal_unique_constraint_set(conn: sqlite3.Connection) -> frozenset[tuple[str, ...]]:
    """table-level UNIQUE constraint（origin='u'）列元组集合；不含 PK/named index。
    current contract 必须恰为 {("event_id",)}；额外 UNIQUE（如 trace_id）
    会改变合法 append 语义 → 拒绝。"""
    result: set[tuple[str, ...]] = set()
    try:
        rows = conn.execute("PRAGMA index_list(runtime_event_journal)").fetchall()
    except sqlite3.Error:
        return frozenset()
    for row in rows:
        if row[3] == "u":
            result.add(_journal_index_columns(conn, row[1]))
    return frozenset(result)


def _journal_unique_named_indexes(conn: sqlite3.Connection) -> frozenset[str]:
    """unique named index（origin='c'，unique=1）名称集合；current contract 必须为空。"""
    try:
        return frozenset(
            row[1]
            for row in conn.execute("PRAGMA index_list(runtime_event_journal)").fetchall()
            if row[3] == "c" and int(row[2]) == 1
        )
    except sqlite3.Error:
        return frozenset()


def _detect_journal_shape(conn: sqlite3.Connection) -> str:
    """返回 current / legacy / unknown；基于 deterministic exact physical
    signature：列/类型/NOT NULL/PK(run_id, sequence)/event_id UNIQUE/
    required index/UNIQUE semantic set exact 全部精确匹配。malformed（列名对
    但约束错或存在额外 UNIQUE）→ unknown → UNSUPPORTED（fail closed）。"""
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "runtime_event_journal" not in tables:
        return "unknown"
    if (
        _journal_table_columns_match(conn, _JOURNAL_CURRENT_COLUMNS)
        and _journal_pk_matches(conn)
        and _journal_event_id_unique_matches(conn)
        and _journal_run_type_index_matches(conn)
        and _journal_unique_constraint_set(conn) == frozenset({("event_id",)})
        and _journal_unique_named_indexes(conn) == frozenset()
    ):
        return "current"
    if (
        _journal_table_columns_match(conn, _JOURNAL_LEGACY_COLUMNS)
        and _journal_pk_matches(conn)
        and _journal_event_id_unique_matches(conn)
        and _journal_run_type_index_matches(conn)
        and _journal_unique_constraint_set(conn) == frozenset({("event_id",)})
        and _journal_unique_named_indexes(conn) == frozenset()
    ):
        return "legacy"
    return "unknown"


def _journal_record_from_row(row: sqlite3.Row) -> JournalRecord:
    """Module-level row → JournalRecord；与实例方法同构，供只读 full preflight。"""
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
            parent_span_id=(
                str(row["parent_span_id"]) if row["parent_span_id"] is not None else None
            ),
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


def _journal_row_versions_supported(conn: sqlite3.Connection) -> bool:
    """Distinct record versions 必须 ⊆ 支持集合（journal 1/2，event 1/2）。"""
    try:
        journal_versions = {
            int(row[0])
            for row in conn.execute(
                "SELECT DISTINCT journal_schema_version FROM runtime_event_journal"
            )
        }
        event_versions = {
            int(row[0])
            for row in conn.execute(
                "SELECT DISTINCT event_schema_version FROM runtime_event_journal"
            )
        }
    except sqlite3.Error:
        return False
    return (
        journal_versions <= set(SUPPORTED_JOURNAL_SCHEMA_VERSIONS)
        and event_versions <= _SUPPORTED_EVENT_SCHEMA_VERSIONS
    )


def _journal_verify_all_records_readonly(conn: sqlite3.Connection) -> None:
    """FULL 模式：逐 row 构造 JournalRecord 并 verify digest；校验 per-run
    terminal invariant（至多一个 terminal 且为最后 sequence）。只读。"""
    rows = conn.execute(
        "SELECT * FROM runtime_event_journal ORDER BY run_id, sequence"
    ).fetchall()
    run_sequences: dict[str, list[tuple[int, RuntimeEventType]]] = {}
    for row in rows:
        record = _journal_record_from_row(row)
        run_sequences.setdefault(record.run_id, []).append(
            (record.sequence, record.event_type)
        )
    for run_id, entries in run_sequences.items():
        terminals = [
            sequence
            for sequence, event_type in entries
            if event_type is _TERMINAL_EVENT_TYPE
        ]
        if len(terminals) > 1 or (
            terminals and terminals[0] != max(sequence for sequence, _ in entries)
        ):
            raise PersistenceError(
                PERSISTENCE_PREFLIGHT_FAILED,
                "Journal Run 终态不变量被破坏",
            )


def _journal_failed_result() -> PersistencePreflightResult:
    return PersistencePreflightResult(
        store_id=StoreId.EVENT_JOURNAL,
        status=PreflightStatus.FAILED,
        action=MigrationAction.NONE,
        detected_version="unknown",
        safe_error_code=PERSISTENCE_PREFLIGHT_FAILED,
    )


def journal_preflight(
    db_path: str, *, mode: PreflightMode = PreflightMode.STARTUP
) -> PersistencePreflightResult:
    """Read-only Journal preflight；绝不在 preflight 中创建或修改 DB。"""
    if not os.path.exists(db_path):
        return PersistencePreflightResult(
            store_id=StoreId.EVENT_JOURNAL,
            status=PreflightStatus.NEW,
            action=MigrationAction.INITIALIZE,
            detected_version="absent",
            target_version="current",
        )
    try:
        sqlite_quick_check(db_path)
    except PersistenceError:
        return _journal_failed_result()
    try:
        conn = open_read_only(db_path)
    except PersistenceError:
        return _journal_failed_result()
    try:
        shape = _detect_journal_shape(conn)
        versions_supported = _journal_row_versions_supported(conn)
        if shape == "current":
            if not versions_supported:
                return PersistencePreflightResult(
                    store_id=StoreId.EVENT_JOURNAL,
                    status=PreflightStatus.UNSUPPORTED,
                    action=MigrationAction.NONE,
                    detected_version="current",
                    target_version="current",
                    safe_error_code=PERSISTENCE_SCHEMA_UNSUPPORTED,
                )
            if mode is PreflightMode.FULL:
                _journal_verify_all_records_readonly(conn)
            return PersistencePreflightResult(
                store_id=StoreId.EVENT_JOURNAL,
                status=PreflightStatus.CURRENT,
                action=MigrationAction.NONE,
                detected_version="current",
                target_version="current",
            )
        if shape == "legacy":
            if not versions_supported:
                return PersistencePreflightResult(
                    store_id=StoreId.EVENT_JOURNAL,
                    status=PreflightStatus.UNSUPPORTED,
                    action=MigrationAction.NONE,
                    detected_version="legacy",
                    target_version="current",
                    safe_error_code=PERSISTENCE_SCHEMA_UNSUPPORTED,
                )
            return PersistencePreflightResult(
                store_id=StoreId.EVENT_JOURNAL,
                status=PreflightStatus.MIGRATION_REQUIRED,
                action=MigrationAction.MIGRATE,
                detected_version="legacy",
                target_version="current",
            )
        return PersistencePreflightResult(
            store_id=StoreId.EVENT_JOURNAL,
            status=PreflightStatus.UNSUPPORTED,
            action=MigrationAction.NONE,
            detected_version="unknown",
            target_version="current",
            safe_error_code=PERSISTENCE_SCHEMA_UNSUPPORTED,
        )
    except PersistenceError:
        return _journal_failed_result()
    except (JournalError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        return _journal_failed_result()
    finally:
        conn.close()


def journal_migrate(db_path: str) -> None:
    """显式 Journal migration：单 transaction 内 revalidate exact legacy shape，
    ADD 两个 nullable span 列 + 确保 index；绝不 UPDATE/DELETE 历史 row，
    绝不重写 version / digest / payload。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        shape = _detect_journal_shape(conn)
        if shape != "legacy":
            raise PersistenceError(
                PERSISTENCE_MIGRATION_FAILED,
                "Journal 迁移前置校验未通过：只接受 exact legacy（缺 span 列）shape",
            )
        if not _journal_row_versions_supported(conn):
            raise PersistenceError(
                PERSISTENCE_MIGRATION_FAILED,
                "Journal 历史 row 版本不受支持，拒绝迁移",
            )
        conn.execute("ALTER TABLE runtime_event_journal ADD COLUMN span_id TEXT")
        conn.execute(
            "ALTER TABLE runtime_event_journal ADD COLUMN parent_span_id TEXT"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runtime_event_journal_run_type "
            "ON runtime_event_journal(run_id, event_type)"
        )
        # §23：COMMIT 前必须通过 strict current physical validation；
        # 失败 ROLLBACK，绝不提交非 exact current shape。
        if _detect_journal_shape(conn) != "current":
            raise PersistenceError(
                PERSISTENCE_MIGRATION_FAILED,
                "Journal 迁移后 physical signature 校验未通过，拒绝提交",
            )
        conn.execute("COMMIT")
    except PersistenceError:
        _journal_migrate_rollback(conn)
        raise
    except sqlite3.Error:
        _journal_migrate_rollback(conn)
        raise PersistenceError(PERSISTENCE_MIGRATION_FAILED) from None
    finally:
        conn.close()


def _journal_migrate_rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
