#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""会话记忆持久化模块。"""

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional
from uuid import uuid4


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
        """初始化消息表、索引、摘要表与全文索引。"""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    metadata TEXT
                )
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "memory_scope" not in columns:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN memory_scope TEXT NOT NULL DEFAULT 'direct'"
                )
            if "exchange_id" not in columns:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN exchange_id TEXT"
                )
            if "run_id" not in columns:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN run_id TEXT"
                )
            if "sequence" not in columns:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN sequence INTEGER"
                )
            conn.execute(
                """
                UPDATE messages
                SET memory_scope = 'direct'
                WHERE memory_scope IS NULL OR memory_scope = ''
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
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                USING fts5(
                    content,
                    agent_id UNINDEXED,
                    role UNINDEXED,
                    content='messages',
                    content_rowid='id'
                )
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_ai
                AFTER INSERT ON messages
                BEGIN
                    INSERT INTO messages_fts(rowid, content, agent_id, role)
                    VALUES (new.id, new.content, new.agent_id, new.role);
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_ad
                AFTER DELETE ON messages
                BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content, agent_id, role)
                    VALUES ('delete', old.id, old.content, old.agent_id, old.role);
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_au
                AFTER UPDATE ON messages
                BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content, agent_id, role)
                    VALUES ('delete', old.id, old.content, old.agent_id, old.role);
                    INSERT INTO messages_fts(rowid, content, agent_id, role)
                    VALUES (new.id, new.content, new.agent_id, new.role);
                END
                """
            )

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
