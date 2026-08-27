#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""会话记忆持久化模块。"""

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional
from uuid import uuid4

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


class MemoryExchangeErrorCode:
    DUPLICATE_EXCHANGE = "DUPLICATE_EXCHANGE"
    INVALID_ARGUMENT = "MEMORY_EXCHANGE_INVALID_ARGUMENT"
    EXCHANGE_FAILED = "MEMORY_EXCHANGE_FAILED"


class MemoryExchangeError(RuntimeError):
    """类型化 Memory exchange 错误；不暴露 SQL/路径/正文。"""

    def __init__(self, error_code: str, safe_message: str) -> None:
        self.error_code = error_code
        self.safe_message = safe_message
        super().__init__(f"{safe_message} (error_code={error_code})")

    def __repr__(self) -> str:
        return (
            f"MemoryExchangeError(error_code={self.error_code!r}, "
            f"safe_message={self.safe_message!r})"
        )


class MemoryManager:
    """封装基于 SQLite 的消息存储、摘要与搜索能力。"""

    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        fault_controller: Optional[Any] = None,
    ) -> None:
        """初始化消息数据库。

        Args:
            db_path: 可选的数据库文件路径。
        """
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        default_dir = os.path.join(project_root, "data", "database")
        os.makedirs(default_dir, exist_ok=True)
        self.db_path = db_path or os.path.join(default_dir, "agent_memory.db")
        from core.runtime.fault_injection import FaultInjectionController

        if fault_controller is not None and not isinstance(
            fault_controller, FaultInjectionController
        ):
            raise TypeError("fault_controller 必须是 FaultInjectionController 或 None")
        self._fault_controller = fault_controller
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """创建带默认 PRAGMA 设置的数据库连接。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """显式单连接事务：成功 COMMIT，任何失败 ROLLBACK。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        """初始化当前 v2 消息库 schema。

        WP1-D / WP1-B：constructor 不再对 existing DB 执行任何隐式 schema
        修改（包括隐式缺列 ALTER 与隐式建表）；只允许两种状态：
        - 全新（无表）DB → 直接创建完整 v2 schema（含 Long-term Memory 结构）；
        - 已通过 startup preflight 的 DB → 保持原状，不做 IF NOT EXISTS 建表。
        v1 / legacy / ambiguous DB 由 startup preflight 拦截（MIGRATION_REQUIRED /
        UNSUPPORTED），schema mutation 只属于显式 SCRIPT_ROLE migrate 命令。
        """
        with self._connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not tables:
                _create_current_memory_schema(conn)
                conn.execute("PRAGMA user_version = 2")

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Dict[str, Any]:
        """将单条数据库记录转换为消息字典。"""
        metadata = row["metadata"]
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "memory_scope": row["memory_scope"] if "memory_scope" in row.keys() else "direct",
            "role": row["role"],
            "content": row["content"],
            "timestamp": row["timestamp"],
            "metadata": json.loads(metadata) if metadata else {},
        }

    def add_message(
        self,
        agent_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        memory_scope: str = "direct",
    ) -> int:
        """写入一条新消息。

        Args:
            agent_id: 智能体标识。
            role: 消息角色。
            content: 消息正文。
            metadata: 可选的元数据。
            memory_scope: 记忆作用域，`direct` 或 `orchestration`。

        Returns:
            int: 新消息的数据库主键。
        """
        meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages (agent_id, role, content, metadata, memory_scope)
                VALUES (?, ?, ?, ?, ?)
                """,
                (agent_id, role, content, meta_json, memory_scope),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _validate_exchange_args(
        *,
        agent_id: str,
        memory_scope: str,
        user_message: str,
        assistant_message: str,
    ) -> None:
        for value, name in (
            (agent_id, "agent_id"),
            (memory_scope, "memory_scope"),
            (user_message, "user_message"),
            (assistant_message, "assistant_message"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise MemoryExchangeError(
                    MemoryExchangeErrorCode.INVALID_ARGUMENT,
                    f"{name} 必须是非空字符串",
                )

    def append_exchange_atomic(
        self,
        agent_id: str,
        memory_scope: str,
        user_message: str,
        assistant_message: str,
        run_id: Optional[str] = None,
        exchange_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """在一个 SQLite 事务中原子提交 user + assistant exchange。

        幂等键：``exchange_id`` 或 ``run_id``（二者其一必须稳定提供）。
        同一 Run 只能提交一次；重复提交抛出 ``DUPLICATE_EXCHANGE``，
        绝不会重发用户正文。任一行写入失败整体回滚。
        """
        self._validate_exchange_args(
            agent_id=agent_id,
            memory_scope=memory_scope,
            user_message=user_message,
            assistant_message=assistant_message,
        )
        if run_id is not None and (
            not isinstance(run_id, str) or not run_id.strip()
        ):
            raise MemoryExchangeError(
                MemoryExchangeErrorCode.INVALID_ARGUMENT,
                "run_id 必须是非空字符串",
            )
        if exchange_id is not None and (
            not isinstance(exchange_id, str) or not exchange_id.strip()
        ):
            raise MemoryExchangeError(
                MemoryExchangeErrorCode.INVALID_ARGUMENT,
                "exchange_id 必须是非空字符串",
            )
        if run_id is None and exchange_id is None:
            raise MemoryExchangeError(
                MemoryExchangeErrorCode.INVALID_ARGUMENT,
                "append_exchange_atomic 必须提供 run_id 或 exchange_id",
            )
        final_exchange_id = exchange_id or run_id or uuid4().hex
        from core.runtime.fault_injection import evaluate_sync_fault
        from core.runtime.fault_injection_contract import FaultPoint

        evaluate_sync_fault(
            self._fault_controller,
            point=FaultPoint.MEMORY_BEFORE_EXCHANGE_BEGIN,
            component="memory_manager",
            run_id=run_id,
            operation_kind="EXCHANGE_BEGIN",
        )
        with self._transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO message_exchanges
                        (exchange_id, run_id, agent_id, memory_scope, state)
                    VALUES (?, ?, ?, ?, 'PENDING')
                    """,
                    (final_exchange_id, run_id, agent_id, memory_scope),
                )
            except sqlite3.IntegrityError:
                raise MemoryExchangeError(
                    MemoryExchangeErrorCode.DUPLICATE_EXCHANGE,
                    "该 Run 的 exchange 已提交，拒绝重复写入",
                ) from None
            evaluate_sync_fault(
                self._fault_controller,
                point=FaultPoint.MEMORY_BEFORE_USER_INSERT,
                component="memory_manager",
                run_id=run_id,
                operation_kind="USER_INSERT",
            )
            user_cursor = conn.execute(
                """
                INSERT INTO messages
                    (agent_id, role, content, metadata, memory_scope,
                     exchange_id, run_id, sequence)
                VALUES (?, 'user', ?, NULL, ?, ?, ?, 0)
                """,
                (agent_id, user_message, memory_scope, final_exchange_id, run_id),
            )
            user_message_id = int(user_cursor.lastrowid)
            evaluate_sync_fault(
                self._fault_controller,
                point=FaultPoint.MEMORY_BEFORE_ASSISTANT_INSERT,
                component="memory_manager",
                run_id=run_id,
                operation_kind="ASSISTANT_INSERT",
            )
            assistant_cursor = conn.execute(
                """
                INSERT INTO messages
                    (agent_id, role, content, metadata, memory_scope,
                     exchange_id, run_id, sequence)
                VALUES (?, 'assistant', ?, NULL, ?, ?, ?, 1)
                """,
                (
                    agent_id,
                    assistant_message,
                    memory_scope,
                    final_exchange_id,
                    run_id,
                ),
            )
            assistant_message_id = int(assistant_cursor.lastrowid)
            evaluate_sync_fault(
                self._fault_controller,
                point=FaultPoint.MEMORY_BEFORE_EXCHANGE_COMMIT,
                component="memory_manager",
                run_id=run_id,
                operation_kind="EXCHANGE_COMMIT",
            )
            conn.execute(
                """
                UPDATE message_exchanges
                SET state = 'COMMITTED',
                    user_message_id = ?,
                    assistant_message_id = ?
                WHERE exchange_id = ?
                """,
                (user_message_id, assistant_message_id, final_exchange_id),
            )
        return {
            "exchange_id": final_exchange_id,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
        }

    @staticmethod
    def _committed_exchange_filter(
        table_alias: str = "m",
        join_prefix: str = "me",
    ) -> str:
        """历史读取只返回 legacy 消息或已 COMMITTED exchange 的消息。"""
        return (
            f"({table_alias}.exchange_id IS NULL OR "
            f"{join_prefix}.state = 'COMMITTED')"
        )

    @staticmethod
    def _committed_exchange_join() -> str:
        return (
            " LEFT JOIN message_exchanges me "
            "ON me.exchange_id = m.exchange_id"
        )

    def count_messages(self, agent_id: str, memory_scope: Optional[str] = "direct") -> int:
        """统计某个智能体在指定作用域内的消息数量。"""
        with self._connect() as conn:
            if memory_scope is None:
                row = conn.execute(
                    "SELECT COUNT(1) AS total FROM messages WHERE agent_id = ?",
                    (agent_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(1) AS total
                    FROM messages
                    WHERE agent_id = ? AND memory_scope = ?
                    """,
                    (agent_id, memory_scope),
                ).fetchone()
        return int(row["total"]) if row else 0

    def get_chat_history(
        self,
        agent_id: str,
        limit: int = 10,
        offset: int = 0,
        ascending: bool = False,
        memory_scope: Optional[str] = "direct",
    ) -> List[Dict[str, Any]]:
        """分页读取某个智能体的历史消息。"""
        order = "ASC" if ascending else "DESC"
        with self._connect() as conn:
            if memory_scope is None:
                rows = conn.execute(
                    f"""
                    SELECT m.id, m.agent_id, m.memory_scope, m.role,
                           m.content, m.timestamp, m.metadata
                    FROM messages m
                    {self._committed_exchange_join()}
                    WHERE m.agent_id = ?
                    AND {self._committed_exchange_filter()}
                    ORDER BY timestamp {order}, id {order}
                    LIMIT ? OFFSET ?
                    """,
                    (agent_id, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT m.id, m.agent_id, m.memory_scope, m.role,
                           m.content, m.timestamp, m.metadata
                    FROM messages m
                    {self._committed_exchange_join()}
                    WHERE m.agent_id = ? AND m.memory_scope = ?
                    AND {self._committed_exchange_filter()}
                    ORDER BY timestamp {order}, id {order}
                    LIMIT ? OFFSET ?
                    """,
                    (agent_id, memory_scope, limit, offset),
                ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def get_summary_record(self, agent_id: str) -> Dict[str, Any]:
        """返回指定智能体的滚动摘要记录。"""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT agent_id, summary, last_message_id, updated_at
                FROM conversation_summaries
                WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
        if row is None:
            return {"agent_id": agent_id, "summary": "", "last_message_id": 0, "updated_at": ""}
        return dict(row)

    def get_all_summaries(self) -> List[Dict[str, Any]]:
        """返回所有滚动摘要记录。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT agent_id, summary, last_message_id, updated_at
                FROM conversation_summaries
                ORDER BY updated_at DESC, agent_id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def save_summary(self, agent_id: str, summary: str, last_message_id: int) -> None:
        """写入或更新指定智能体的滚动摘要。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_summaries(agent_id, summary, last_message_id, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(agent_id) DO UPDATE SET
                    summary = excluded.summary,
                    last_message_id = excluded.last_message_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (agent_id, summary, last_message_id),
            )

    def get_messages_for_summary(
        self,
        agent_id: str,
        after_id: int,
        before_id: int,
        memory_scope: Optional[str] = "direct",
    ) -> List[Dict[str, Any]]:
        """读取用于生成摘要的消息片段。"""
        if before_id <= after_id:
            return []
        with self._connect() as conn:
            if memory_scope is None:
                rows = conn.execute(
                    f"""
                    SELECT m.id, m.agent_id, m.memory_scope, m.role,
                           m.content, m.timestamp, m.metadata
                    FROM messages m
                    {self._committed_exchange_join()}
                    WHERE m.agent_id = ? AND m.id > ? AND m.id <= ?
                    AND {self._committed_exchange_filter()}
                    ORDER BY m.id ASC
                    """,
                    (agent_id, after_id, before_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT m.id, m.agent_id, m.memory_scope, m.role,
                           m.content, m.timestamp, m.metadata
                    FROM messages m
                    {self._committed_exchange_join()}
                    WHERE m.agent_id = ? AND m.memory_scope = ?
                      AND m.id > ? AND m.id <= ?
                    AND {self._committed_exchange_filter()}
                    ORDER BY m.id ASC
                    """,
                    (agent_id, memory_scope, after_id, before_id),
                ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def search_messages(
        self,
        keyword: str,
        limit: int = 50,
        memory_scope: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """按关键词搜索消息。"""
        query = " ".join(keyword.split()).strip()
        if not query:
            return []

        with self._connect() as conn:
            try:
                if memory_scope is None:
                    rows = conn.execute(
                        f"""
                        SELECT m.id, m.agent_id, m.memory_scope, m.role, m.content, m.timestamp, m.metadata
                        FROM messages_fts f
                        JOIN messages m ON m.id = f.rowid
                        LEFT JOIN message_exchanges me ON me.exchange_id = m.exchange_id
                        WHERE messages_fts MATCH ?
                        AND (m.exchange_id IS NULL OR me.state = 'COMMITTED')
                        ORDER BY m.timestamp DESC, m.id DESC
                        LIMIT ?
                        """,
                        (query, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"""
                        SELECT m.id, m.agent_id, m.memory_scope, m.role, m.content, m.timestamp, m.metadata
                        FROM messages_fts f
                        JOIN messages m ON m.id = f.rowid
                        LEFT JOIN message_exchanges me ON me.exchange_id = m.exchange_id
                        WHERE messages_fts MATCH ? AND m.memory_scope = ?
                        AND (m.exchange_id IS NULL OR me.state = 'COMMITTED')
                        ORDER BY m.timestamp DESC, m.id DESC
                        LIMIT ?
                        """,
                        (query, memory_scope, limit),
                    ).fetchall()
            except sqlite3.OperationalError:
                if memory_scope is None:
                    rows = conn.execute(
                        f"""
                        SELECT m.id, m.agent_id, m.memory_scope, m.role,
                               m.content, m.timestamp, m.metadata
                        FROM messages m
                        {self._committed_exchange_join()}
                        WHERE m.content LIKE ?
                        AND {self._committed_exchange_filter()}
                        ORDER BY m.timestamp DESC, m.id DESC
                        LIMIT ?
                        """,
                        (f"%{keyword}%", limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"""
                        SELECT m.id, m.agent_id, m.memory_scope, m.role,
                               m.content, m.timestamp, m.metadata
                        FROM messages m
                        {self._committed_exchange_join()}
                        WHERE m.content LIKE ? AND m.memory_scope = ?
                        AND {self._committed_exchange_filter()}
                        ORDER BY m.timestamp DESC, m.id DESC
                        LIMIT ?
                        """,
                        (f"%{keyword}%", memory_scope, limit),
                    ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def get_all_messages(
        self,
        limit: int = 500,
        memory_scope: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """返回最近一批持久化消息。"""
        with self._connect() as conn:
            if memory_scope is None:
                rows = conn.execute(
                    f"""
                    SELECT m.id, m.agent_id, m.memory_scope, m.role,
                           m.content, m.timestamp, m.metadata
                    FROM messages m
                    {self._committed_exchange_join()}
                    WHERE {self._committed_exchange_filter()}
                    ORDER BY m.timestamp DESC, m.id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT m.id, m.agent_id, m.memory_scope, m.role,
                           m.content, m.timestamp, m.metadata
                    FROM messages m
                    {self._committed_exchange_join()}
                    WHERE m.memory_scope = ?
                    AND {self._committed_exchange_filter()}
                    ORDER BY m.timestamp DESC, m.id DESC
                    LIMIT ?
                    """,
                    (memory_scope, limit),
                ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def delete_messages(self, message_ids: List[int]) -> Dict[str, List[str]]:
        """批量删除指定消息，并返回需要刷新的智能体。

        只有直接会话记忆会触发聊天窗口历史刷新与摘要失效；
        委派记忆属于内部轨迹，不应污染用户直聊界面。
        """
        if not message_ids:
            return {"affected_agent_ids": [], "refresh_agent_ids": []}

        placeholders = ",".join("?" for _ in message_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT agent_id, memory_scope
                FROM messages
                WHERE id IN ({placeholders})
                """,
                message_ids,
            ).fetchall()
            conn.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", message_ids)
            affected_agent_ids = sorted({row["agent_id"] for row in rows})
            refresh_agent_ids = sorted(
                {
                    row["agent_id"]
                    for row in rows
                    if row["memory_scope"] == "direct"
                }
            )
            if refresh_agent_ids:
                summary_placeholders = ",".join("?" for _ in refresh_agent_ids)
                conn.execute(
                    f"DELETE FROM conversation_summaries WHERE agent_id IN ({summary_placeholders})",
                    refresh_agent_ids,
                )

        return {
            "affected_agent_ids": affected_agent_ids,
            "refresh_agent_ids": refresh_agent_ids,
        }

    def clear_all_memory(self) -> None:
        """清空全部消息、摘要和全文索引。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM conversation_summaries")
            conn.execute("DELETE FROM messages_fts")


# ---------------------------------------------------------------------------
# WP1-D Persistence Preflight / Migration（Store-owned，Coordinator 只编排）
# ---------------------------------------------------------------------------

MEMORY_SCHEMA_VERSION = 2

# 每个条目 = (name, declared_type, notnull, dflt_value, pk_position)。
# dflt_value 比较时做大小写/引号规范化（见 _norm_default），避免格式漂移误判。
_MESSAGES_CURRENT_COLUMNS = (
    ("id", "INTEGER", 0, None, 1),
    ("agent_id", "TEXT", 1, None, 0),
    ("role", "TEXT", 1, None, 0),
    ("content", "TEXT", 1, None, 0),
    ("timestamp", "DATETIME", 0, "CURRENT_TIMESTAMP", 0),
    ("metadata", "TEXT", 0, None, 0),
    ("memory_scope", "TEXT", 1, "'direct'", 0),
    ("exchange_id", "TEXT", 0, None, 0),
    ("run_id", "TEXT", 0, None, 0),
    ("sequence", "INTEGER", 0, None, 0),
)
_MESSAGES_LEGACY_COLUMNS = (
    ("id", "INTEGER", 0, None, 1),
    ("agent_id", "TEXT", 1, None, 0),
    ("role", "TEXT", 1, None, 0),
    ("content", "TEXT", 1, None, 0),
    ("timestamp", "DATETIME", 0, "CURRENT_TIMESTAMP", 0),
    ("metadata", "TEXT", 0, None, 0),
)
_SUMMARY_COLUMNS = (
    ("agent_id", "TEXT", 0, None, 1),
    ("summary", "TEXT", 1, None, 0),
    ("last_message_id", "INTEGER", 1, "0", 0),
    ("updated_at", "DATETIME", 0, "CURRENT_TIMESTAMP", 0),
)
_EXCHANGES_COLUMNS = (
    ("exchange_id", "TEXT", 0, None, 1),
    ("run_id", "TEXT", 0, None, 0),
    ("agent_id", "TEXT", 1, None, 0),
    ("memory_scope", "TEXT", 1, "'direct'", 0),
    ("state", "TEXT", 1, None, 0),
    ("user_message_id", "INTEGER", 0, None, 0),
    ("assistant_message_id", "INTEGER", 0, None, 0),
    ("created_at", "DATETIME", 0, "CURRENT_TIMESTAMP", 0),
)
# index name → (unique, partial, columns)
_MESSAGES_INDEXES = {
    "idx_messages_agent_scope_time": (0, 0, ("agent_id", "memory_scope", "timestamp", "id")),
    "idx_messages_agent_time": (0, 0, ("agent_id", "timestamp", "id")),
    "idx_messages_scope_time": (0, 0, ("memory_scope", "timestamp", "id")),
    "idx_messages_timestamp": (0, 0, ("timestamp", "id")),
    "idx_messages_exchange_role": (1, 1, ("exchange_id", "role")),
    "idx_exchanges_state": (0, 0, ("state",)),
}
_LEGACY_INDEXES = {
    "idx_messages_agent_time": (0, 0, ("agent_id", "timestamp", "id")),
    "idx_messages_timestamp": (0, 0, ("timestamp", "id")),
}
_IDX_EXCHANGE_ROLE_PREDICATE = "exchange_id IS NOT NULL"

# Long-term Memory 独立结构（Advanced Memory Domain，不属于 Conversation
# History / Short-term Context）。WP1 v1 只保存 SEMANTIC record。
# created_at / updated_at 以 TEXT 保存 UTC ISO8601（由 Domain boundary 产生）。
_LONG_TERM_MEMORY_COLUMNS = (
    ("memory_id", "TEXT", 0, None, 1),
    ("memory_type", "TEXT", 1, None, 0),
    ("status", "TEXT", 1, None, 0),
    ("agent_id", "TEXT", 1, None, 0),
    ("memory_scope", "TEXT", 1, None, 0),
    ("canonical_text", "TEXT", 1, None, 0),
    ("payload", "TEXT", 1, None, 0),
    ("logical_key", "TEXT", 0, None, 0),
    ("origin_type", "TEXT", 1, None, 0),
    ("origin_run_id", "TEXT", 1, None, 0),
    ("origin_exchange_id", "TEXT", 1, None, 0),
    ("origin_agent_id", "TEXT", 1, None, 0),
    ("origin_memory_scope", "TEXT", 1, None, 0),
    ("formation_method", "TEXT", 0, None, 0),
    ("created_at", "TEXT", 1, None, 0),
    ("updated_at", "TEXT", 1, None, 0),
    ("superseded_by_memory_id", "TEXT", 0, None, 0),
)
_LONG_TERM_MEMORY_INDEXES = {
    "idx_long_term_memory_agent_scope": (0, 0, ("agent_id", "memory_scope")),
}
_LONG_TERM_MEMORY_DDL = (
    "CREATE TABLE IF NOT EXISTS long_term_memory ("
    "memory_id TEXT PRIMARY KEY, "
    "memory_type TEXT NOT NULL, "
    "status TEXT NOT NULL, "
    "agent_id TEXT NOT NULL, "
    "memory_scope TEXT NOT NULL, "
    "canonical_text TEXT NOT NULL, "
    "payload TEXT NOT NULL, "
    "logical_key TEXT, "
    "origin_type TEXT NOT NULL, "
    "origin_run_id TEXT NOT NULL, "
    "origin_exchange_id TEXT NOT NULL, "
    "origin_agent_id TEXT NOT NULL, "
    "origin_memory_scope TEXT NOT NULL, "
    "formation_method TEXT, "
    "created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, "
    "superseded_by_memory_id TEXT"
    ")"
)
_LONG_TERM_MEMORY_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_long_term_memory_agent_scope "
    "ON long_term_memory(agent_id, memory_scope)"
)

# FTS / trigger 的 CREATE 语句事实 SQL（_create_current_memory_schema 与
# signature 校验共用同一常量，杜绝漂移；sqlite_master 存储时去掉 IF NOT EXISTS）。
_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
    "content, agent_id UNINDEXED, role UNINDEXED, "
    "content='messages', content_rowid='id')"
)
_TRIGGER_AI_DDL = (
    "CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages "
    "BEGIN INSERT INTO messages_fts(rowid, content, agent_id, role) "
    "VALUES (new.id, new.content, new.agent_id, new.role); END"
)
_TRIGGER_AD_DDL = (
    "CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages "
    "BEGIN INSERT INTO messages_fts(messages_fts, rowid, content, agent_id, role) "
    "VALUES ('delete', old.id, old.content, old.agent_id, old.role); END"
)
_TRIGGER_AU_DDL = (
    "CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages "
    "BEGIN INSERT INTO messages_fts(messages_fts, rowid, content, agent_id, role) "
    "VALUES ('delete', old.id, old.content, old.agent_id, old.role); "
    "INSERT INTO messages_fts(rowid, content, agent_id, role) "
    "VALUES (new.id, new.content, new.agent_id, new.role); END"
)
_EXPECTED_TRIGGERS = {
    "messages_ai": _TRIGGER_AI_DDL,
    "messages_ad": _TRIGGER_AD_DDL,
    "messages_au": _TRIGGER_AU_DDL,
}


def _canonical_sql(sql: str) -> str:
    """最小 quote-aware canonicalization（scanner，非完整 SQL parser）。

    - 字面量之外：折叠空白、去 IF NOT EXISTS、小写关键字/标识符、
      去双引号标识符引号、去括号旁空白；
    - 单引号字符串字面量内：**原样保留**（大小写与 '' 转义语义不改变）。

    `'delete'` 与 `'DELETE'` 的 canonical 不同（语义敏感）；`'abc''def'`
    的转义引号不会被错误提前关闭 literal。
    """
    pieces: list[tuple[str, str]] = []  # ("O", outside) | ("L", literal)
    outside: list[str] = []
    literal: list[str] = []
    index = 0
    length = len(sql)
    in_literal = False
    while index < length:
        ch = sql[index]
        if in_literal:
            if ch == "'":
                if index + 1 < length and sql[index + 1] == "'":
                    literal.append("''")
                    index += 2
                    continue
                in_literal = False
                pieces.append(("L", "".join(literal)))
                literal = []
            else:
                literal.append(ch)
            index += 1
            continue
        if ch == "'":
            if outside:
                pieces.append(("O", "".join(outside)))
                outside = []
            in_literal = True
            literal = []
            index += 1
            continue
        outside.append(ch)
        index += 1
    if in_literal:
        pieces.append(("L", "".join(literal)))
    elif outside:
        pieces.append(("O", "".join(outside)))

    normalized: list[str] = []
    for kind, text in pieces:
        if kind == "L":
            normalized.append("'" + text + "'")
            continue
        if not text:
            continue
        collapsed = text.replace('"', "")
        collapsed = collapsed.lower()
        collapsed = collapsed.replace("if not exists ", "")
        collapsed = re.sub(r"\s*\(\s*", "(", collapsed)
        collapsed = re.sub(r"\s*\)\s*", ")", collapsed)
        collapsed = " ".join(collapsed.split())
        if collapsed:
            normalized.append(collapsed)
    return " ".join(normalized)


def _norm_type(value) -> str:
    return str(value).upper()


def _norm_default(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]
    return text.lower()


def _table_columns_match(
    conn: sqlite3.Connection, table: str, expected: tuple
) -> bool:
    """按 (name, type, notnull, dflt, pk) 精确比较 PRAGMA table_info 输出。

    按列名匹配而非物理顺序：SQLite 查询按名引用列，且 ALTER ADD COLUMN 会
    把新列追加到表尾，物理顺序不是语义合同。
    """
    try:
        actual = conn.execute(f"PRAGMA table_info({table})").fetchall()
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


def _index_definition(
    conn: sqlite3.Connection, table: str, index_name: str
) -> Optional[tuple]:
    """返回 (unique, partial, columns)；index 不存在返回 None。"""
    try:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    except sqlite3.Error:
        return None
    match = None
    for row in rows:
        if row[1] == index_name:
            match = row
            break
    if match is None:
        return None
    try:
        columns = tuple(
            r[2] for r in conn.execute(f"PRAGMA index_info({index_name})")
        )
    except sqlite3.Error:
        return None
    return (int(match[2]), int(match[4]), columns)


def _partial_index_predicate(conn: sqlite3.Connection, index_name: str) -> Optional[str]:
    """从 sqlite_master.sql 提取 partial index 的 WHERE 谓词 canonical 形式。"""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    marker = row[0].upper().rfind(" WHERE ")
    if marker == -1:
        return None
    return _canonical_sql(row[0][marker + len(" WHERE "):])


def _index_matches(
    conn: sqlite3.Connection,
    table: str,
    index_name: str,
    *,
    unique: int,
    partial: int,
    columns: tuple,
    predicate: Optional[str] = None,
) -> bool:
    definition = _index_definition(conn, table, index_name)
    if definition is None:
        return False
    actual_unique, actual_partial, actual_columns = definition
    if actual_unique != unique or actual_partial != partial:
        return False
    if actual_columns != columns:
        return False
    if predicate is not None:
        if _partial_index_predicate(conn, index_name) != _canonical_sql(predicate):
            return False
    return True


def _trigger_names(conn: sqlite3.Connection) -> frozenset[str]:
    try:
        return frozenset(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        )
    except sqlite3.Error:
        return frozenset()


def _unique_constraint_set(
    conn: sqlite3.Connection, table: str
) -> frozenset[tuple[str, ...]]:
    """table-level UNIQUE constraint（origin='u'）的列元组集合。

    不含 PK（origin='pk'）与 named index（origin='c'）；不依赖 autoindex 名。
    该集合决定数据库允许写入的数据集合，必须 exact。
    """
    result: set[tuple[str, ...]] = set()
    try:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    except sqlite3.Error:
        return frozenset()
    for row in rows:
        # (seq, name, unique, origin, partial)
        if row[3] == "u":
            try:
                columns = tuple(
                    r[2] for r in conn.execute(f"PRAGMA index_info({row[1]})")
                )
            except sqlite3.Error:
                return frozenset()
            result.add(columns)
    return frozenset(result)


def _unique_named_indexes(
    conn: sqlite3.Connection, table: str
) -> frozenset[str]:
    """unique named index（origin='c'，unique=1）名称集合。

    额外 unique named index 会改变合法数据集合，必须 exact 校验；
    普通 non-unique named index 不改变业务约束，不在本集合内。
    """
    try:
        return frozenset(
            row[1]
            for row in conn.execute(f"PRAGMA index_list({table})")
            if row[3] == "c" and int(row[2]) == 1
        )
    except sqlite3.Error:
        return frozenset()


def _trigger_matches(conn: sqlite3.Connection, name: str, expected_ddl: str) -> bool:
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (name,),
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None or row[0] is None:
        return False
    return _canonical_sql(row[0]) == _canonical_sql(expected_ddl)


def _fts_matches(conn: sqlite3.Connection, expected_ddl: str) -> bool:
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            ("messages_fts",),
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None or row[0] is None:
        return False
    return _canonical_sql(row[0]) == _canonical_sql(expected_ddl)


def _memory_v1_core_holds(conn: sqlite3.Connection) -> bool:
    """v1 core physical signature：messages(current columns) +
    conversation_summaries + message_exchanges + required indexes + FTS +
    triggers 全部精确匹配，且 UNIQUE semantic constraint set exact。

    v2（current）与 v1 都要求该 core 精确成立；差异只在
    `long_term_memory` 是否存在。
    """
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if not {"messages", "conversation_summaries", "message_exchanges"} <= tables:
        return False
    if not _table_columns_match(conn, "messages", _MESSAGES_CURRENT_COLUMNS):
        return False
    if not _table_columns_match(conn, "conversation_summaries", _SUMMARY_COLUMNS):
        return False
    if not _table_columns_match(conn, "message_exchanges", _EXCHANGES_COLUMNS):
        return False
    # semantic UNIQUE set exact：
    # messages / conversation_summaries 无 UNIQUE；message_exchanges 恰有 run_id UNIQUE。
    if _unique_constraint_set(conn, "messages") != frozenset():
        return False
    if _unique_constraint_set(conn, "conversation_summaries") != frozenset():
        return False
    if _unique_constraint_set(conn, "message_exchanges") != frozenset({("run_id",)}):
        return False
    # unique named index 只允许 allowlisted idx_messages_exchange_role。
    if _unique_named_indexes(conn, "messages") != frozenset(
        {"idx_messages_exchange_role"}
    ):
        return False
    if _unique_named_indexes(conn, "conversation_summaries") != frozenset():
        return False
    if _unique_named_indexes(conn, "message_exchanges") != frozenset():
        return False
    for name, (unique, partial, columns) in _MESSAGES_INDEXES.items():
        table = "messages" if name != "idx_exchanges_state" else "message_exchanges"
        predicate = (
            _IDX_EXCHANGE_ROLE_PREDICATE
            if name == "idx_messages_exchange_role"
            else None
        )
        if not _index_matches(
            conn, table, name, unique=unique, partial=partial,
            columns=columns, predicate=predicate,
        ):
            return False
    if not _fts_matches(conn, _FTS_DDL):
        return False
    for name, ddl in _EXPECTED_TRIGGERS.items():
        if not _trigger_matches(conn, name, ddl):
            return False
    return True


def _memory_current_signature_holds(conn: sqlite3.Connection) -> bool:
    """完整 current v2 physical signature：v1 core 精确成立 +
    `long_term_memory` 独立结构精确存在（列/约束/索引），且无额外 UNIQUE
    semantic constraint。"""
    if not _memory_v1_core_holds(conn):
        return False
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "long_term_memory" not in tables:
        return False
    if not _table_columns_match(conn, "long_term_memory", _LONG_TERM_MEMORY_COLUMNS):
        return False
    # memory_id 是 PK（origin='pk'），不属于 origin='u'；long_term_memory
    # 不允许额外 table-level UNIQUE constraint。
    if _unique_constraint_set(conn, "long_term_memory") != frozenset():
        return False
    if _unique_named_indexes(conn, "long_term_memory") != frozenset():
        return False
    for name, (unique, partial, columns) in _LONG_TERM_MEMORY_INDEXES.items():
        if not _index_matches(
            conn, "long_term_memory", name, unique=unique, partial=partial,
            columns=columns,
        ):
            return False
    return True


def _memory_v1_signature_holds(conn: sqlite3.Connection) -> bool:
    """v1 physical signature：v1 core 精确成立 + `long_term_memory` 缺席。

    这是 v1→v2 migration 的已批准 from-state。"""
    if not _memory_v1_core_holds(conn):
        return False
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    return "long_term_memory" not in tables


def _memory_legacy_signature_holds(conn: sqlite3.Connection) -> bool:
    """唯一 allowlisted pre-additive legacy signature：messages 恰为 6 基础列 +
    conversation_summaries 精确 + message_exchanges/FTS/triggers 缺席 +
    两个 legacy 索引精确存在 + 无任何 UNIQUE semantic constraint
    （额外 UNIQUE → 不再是 frozen historical legacy shape → UNSUPPORTED）。"""
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "messages" not in tables or "conversation_summaries" not in tables:
        return False
    if "message_exchanges" in tables or "messages_fts" in tables:
        return False
    if _trigger_names(conn) & frozenset({"messages_ai", "messages_ad", "messages_au"}):
        return False
    if not _table_columns_match(conn, "messages", _MESSAGES_LEGACY_COLUMNS):
        return False
    if not _table_columns_match(conn, "conversation_summaries", _SUMMARY_COLUMNS):
        return False
    if _unique_constraint_set(conn, "messages") != frozenset():
        return False
    if _unique_constraint_set(conn, "conversation_summaries") != frozenset():
        return False
    if _unique_named_indexes(conn, "messages") != frozenset():
        return False
    if _unique_named_indexes(conn, "conversation_summaries") != frozenset():
        return False
    for name, (unique, partial, columns) in _LEGACY_INDEXES.items():
        if not _index_matches(
            conn, "messages", name, unique=unique, partial=partial, columns=columns
        ):
            return False
    return True


def _create_long_term_memory_schema(conn: sqlite3.Connection) -> None:
    """创建 Long-term Memory 独立结构（v2 新增；全部 IF NOT EXISTS）。

    只用于全新 v2 初始化与显式 v1→v2 migration；不会被 constructor 对
    已有 DB 隐式调用。
    """
    conn.execute(_LONG_TERM_MEMORY_DDL)
    conn.execute(_LONG_TERM_MEMORY_INDEX_DDL)


def _create_current_memory_schema(conn: sqlite3.Connection) -> None:
    """创建当前 Memory v2 schema（全部 IF NOT EXISTS，additive-safe）。

    messages 表按当前完整列集合定义；本函数同时是全新 DB 初始化与
    legacy migrate 共用的事实 SQL 来源。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT,
            memory_scope TEXT NOT NULL DEFAULT 'direct',
            exchange_id TEXT,
            run_id TEXT,
            sequence INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_summaries (
            agent_id TEXT PRIMARY KEY,
            summary TEXT NOT NULL,
            last_message_id INTEGER NOT NULL DEFAULT 0,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_agent_scope_time "
        "ON messages(agent_id, memory_scope, timestamp DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_agent_time "
        "ON messages(agent_id, timestamp DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_scope_time "
        "ON messages(memory_scope, timestamp DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_timestamp "
        "ON messages(timestamp DESC, id DESC)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_exchange_role "
        "ON messages(exchange_id, role) WHERE exchange_id IS NOT NULL"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_exchanges (
            exchange_id TEXT PRIMARY KEY,
            run_id TEXT UNIQUE,
            agent_id TEXT NOT NULL,
            memory_scope TEXT NOT NULL DEFAULT 'direct',
            state TEXT NOT NULL,
            user_message_id INTEGER,
            assistant_message_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_exchanges_state "
        "ON message_exchanges(state)"
    )
    conn.execute(_FTS_DDL)
    conn.execute(_TRIGGER_AI_DDL)
    conn.execute(_TRIGGER_AD_DDL)
    conn.execute(_TRIGGER_AU_DDL)
    _create_long_term_memory_schema(conn)


def _detect_memory_shape(conn: sqlite3.Connection) -> str:
    """返回 current / v1 / legacy / unknown；基于 deterministic exact
    physical signature（列/类型/NOT NULL/DEFAULT/PK/索引列/唯一性/partial
    谓词/FTS/trigger 全部精确匹配）。malformed / ambiguous → unknown（由调用方
    判 UNSUPPORTED，fail closed）。"""
    if _memory_current_signature_holds(conn):
        return "current"
    if _memory_v1_signature_holds(conn):
        return "v1"
    if _memory_legacy_signature_holds(conn):
        return "legacy"
    return "unknown"


def _memory_failed_result() -> PersistencePreflightResult:
    return PersistencePreflightResult(
        store_id=StoreId.MEMORY,
        status=PreflightStatus.FAILED,
        action=MigrationAction.NONE,
        detected_version="unknown",
        safe_error_code=PERSISTENCE_PREFLIGHT_FAILED,
    )


def memory_preflight(
    db_path: str, *, mode: PreflightMode = PreflightMode.STARTUP
) -> PersistencePreflightResult:
    """Read-only Memory preflight。

    FULL 模式追加 bounded read probe（COUNT，不暴露正文）。绝不在 preflight
    中创建或修改 DB 文件。
    """
    if not os.path.exists(db_path):
        return PersistencePreflightResult(
            store_id=StoreId.MEMORY,
            status=PreflightStatus.NEW,
            action=MigrationAction.INITIALIZE,
            detected_version="absent",
            target_version=str(MEMORY_SCHEMA_VERSION),
        )
    try:
        sqlite_quick_check(db_path)
    except PersistenceError:
        return _memory_failed_result()
    try:
        conn = open_read_only(db_path)
    except PersistenceError:
        return _memory_failed_result()
    try:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        shape = _detect_memory_shape(conn)
        if mode is PreflightMode.FULL and shape in {"current", "v1", "legacy"}:
            conn.execute("SELECT COUNT(1) FROM messages").fetchone()
            conn.execute(
                "SELECT COUNT(1) FROM conversation_summaries"
            ).fetchone()
            if shape == "current":
                conn.execute(
                    "SELECT COUNT(1) FROM long_term_memory"
                ).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
        return _memory_failed_result()
    finally:
        conn.close()

    if shape == "unknown":
        # truly empty valid SQLite file（无任何表）→ new v2 initialization
        try:
            probe = open_read_only(db_path)
            try:
                tables = {
                    row[0]
                    for row in probe.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                probe.close()
        except PersistenceError:
            return _memory_failed_result()
        if not tables:
            return PersistencePreflightResult(
                store_id=StoreId.MEMORY,
                status=PreflightStatus.NEW,
                action=MigrationAction.INITIALIZE,
                detected_version="0",
                target_version=str(MEMORY_SCHEMA_VERSION),
            )

    if user_version == MEMORY_SCHEMA_VERSION and shape == "current":
        return PersistencePreflightResult(
            store_id=StoreId.MEMORY,
            status=PreflightStatus.CURRENT,
            action=MigrationAction.NONE,
            detected_version=str(user_version),
            target_version=str(MEMORY_SCHEMA_VERSION),
        )
    if user_version == MEMORY_SCHEMA_VERSION:
        return PersistencePreflightResult(
            store_id=StoreId.MEMORY,
            status=PreflightStatus.UNSUPPORTED,
            action=MigrationAction.NONE,
            detected_version=str(user_version),
            target_version=str(MEMORY_SCHEMA_VERSION),
            safe_error_code=PERSISTENCE_SCHEMA_UNSUPPORTED,
        )
    if user_version == 1 and shape == "v1":
        return PersistencePreflightResult(
            store_id=StoreId.MEMORY,
            status=PreflightStatus.MIGRATION_REQUIRED,
            action=MigrationAction.MIGRATE,
            detected_version="1",
            target_version=str(MEMORY_SCHEMA_VERSION),
        )
    if user_version == 1:
        return PersistencePreflightResult(
            store_id=StoreId.MEMORY,
            status=PreflightStatus.UNSUPPORTED,
            action=MigrationAction.NONE,
            detected_version=str(user_version),
            target_version=str(MEMORY_SCHEMA_VERSION),
            safe_error_code=PERSISTENCE_SCHEMA_UNSUPPORTED,
        )
    if user_version == 0 and shape == "current":
        return PersistencePreflightResult(
            store_id=StoreId.MEMORY,
            status=PreflightStatus.MIGRATION_REQUIRED,
            action=MigrationAction.MIGRATE,
            detected_version="0",
            target_version=str(MEMORY_SCHEMA_VERSION),
        )
    if user_version == 0 and shape == "v1":
        return PersistencePreflightResult(
            store_id=StoreId.MEMORY,
            status=PreflightStatus.MIGRATION_REQUIRED,
            action=MigrationAction.MIGRATE,
            detected_version="v1-0",
            target_version=str(MEMORY_SCHEMA_VERSION),
        )
    if user_version == 0 and shape == "legacy":
        return PersistencePreflightResult(
            store_id=StoreId.MEMORY,
            status=PreflightStatus.MIGRATION_REQUIRED,
            action=MigrationAction.MIGRATE,
            detected_version="legacy-0",
            target_version=str(MEMORY_SCHEMA_VERSION),
        )
    if user_version == 0:
        return PersistencePreflightResult(
            store_id=StoreId.MEMORY,
            status=PreflightStatus.UNSUPPORTED,
            action=MigrationAction.NONE,
            detected_version=str(user_version),
            target_version=str(MEMORY_SCHEMA_VERSION),
            safe_error_code=PERSISTENCE_SCHEMA_UNSUPPORTED,
        )
    if user_version > MEMORY_SCHEMA_VERSION:
        return PersistencePreflightResult(
            store_id=StoreId.MEMORY,
            status=PreflightStatus.UNSUPPORTED,
            action=MigrationAction.NONE,
            detected_version=str(user_version),
            target_version=str(MEMORY_SCHEMA_VERSION),
            safe_error_code=PERSISTENCE_SCHEMA_UNSUPPORTED,
        )
    return PersistencePreflightResult(
        store_id=StoreId.MEMORY,
        status=PreflightStatus.UNSUPPORTED,
        action=MigrationAction.NONE,
        detected_version=str(user_version),
        target_version=str(MEMORY_SCHEMA_VERSION),
        safe_error_code=PERSISTENCE_SCHEMA_UNSUPPORTED,
    )


def memory_migrate(db_path: str) -> None:
    """显式 Memory migration：单 Store transaction，只允许已批准 from-state。

    - current-unversioned（user_version=0 + 完整 v2 current shape）→ 版本 2 adoption；
    - v1（user_version=0/1 + 完整 v1 shape，无 long_term_memory）→ additive 新增
      Long-term Memory 结构 + 版本 2；
    - 唯一 allowlisted pre-additive legacy shape → additive columns + backfill +
      approved tables/indexes/FTS/triggers + Long-term Memory + 版本 2。
    任何其他 from-state 抛 PERSISTENCE_MIGRATION_FAILED 且不修改。绝不改业务 row 正文。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        shape = _detect_memory_shape(conn)
        if user_version == 0 and shape == "current":
            conn.execute("PRAGMA user_version = 2")
            conn.execute("COMMIT")
            return
        if user_version in (0, 1) and shape == "v1":
            _create_long_term_memory_schema(conn)
            if _detect_memory_shape(conn) != "current":
                raise PersistenceError(
                    PERSISTENCE_MIGRATION_FAILED,
                    "Memory v1→v2 迁移后 physical signature 校验未通过，拒绝提交",
                )
            conn.execute("PRAGMA user_version = 2")
            conn.execute("COMMIT")
            return
        if user_version == 0 and shape == "legacy":
            conn.execute(
                "ALTER TABLE messages ADD COLUMN memory_scope TEXT NOT NULL DEFAULT 'direct'"
            )
            conn.execute("ALTER TABLE messages ADD COLUMN exchange_id TEXT")
            conn.execute("ALTER TABLE messages ADD COLUMN run_id TEXT")
            conn.execute("ALTER TABLE messages ADD COLUMN sequence INTEGER")
            conn.execute(
                "UPDATE messages SET memory_scope = 'direct' "
                "WHERE memory_scope IS NULL OR memory_scope = ''"
            )
            _create_current_memory_schema(conn)
            # §16：设置 user_version 前必须通过 exact current v2 physical signature
            # 校验；任何 wrong trigger/index/constraint 都不提交版本 2。
            if _detect_memory_shape(conn) != "current":
                raise PersistenceError(
                    PERSISTENCE_MIGRATION_FAILED,
                    "Memory 迁移后 physical signature 校验未通过，拒绝提交",
                )
            conn.execute("PRAGMA user_version = 2")
            conn.execute("COMMIT")
            return
        raise PersistenceError(
            PERSISTENCE_MIGRATION_FAILED,
            "Memory 迁移前置校验未通过：只接受 current-unversioned、v1 或 allowlisted legacy",
        )
    except PersistenceError:
        _rollback_safely(conn)
        raise
    except sqlite3.Error:
        _rollback_safely(conn)
        raise PersistenceError(PERSISTENCE_MIGRATION_FAILED) from None
    finally:
        conn.close()


def _rollback_safely(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
