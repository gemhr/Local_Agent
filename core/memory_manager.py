#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""会话记忆持久化模块。"""

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional


class MemoryManager:
    """封装基于 SQLite 的消息存储、摘要与搜索能力。"""

    def __init__(self, db_path: Optional[str] = None) -> None:
        """初始化消息数据库。

        Args:
            db_path: 可选的数据库文件路径。
        """
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        default_dir = os.path.join(project_root, "data", "database")
        os.makedirs(default_dir, exist_ok=True)
        self.db_path = db_path or os.path.join(default_dir, "agent_memory.db")
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
                    SELECT id, agent_id, memory_scope, role, content, timestamp, metadata
                    FROM messages
                    WHERE agent_id = ?
                    ORDER BY timestamp {order}, id {order}
                    LIMIT ? OFFSET ?
                    """,
                    (agent_id, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT id, agent_id, memory_scope, role, content, timestamp, metadata
                    FROM messages
                    WHERE agent_id = ? AND memory_scope = ?
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
                    """
                    SELECT id, agent_id, memory_scope, role, content, timestamp, metadata
                    FROM messages
                    WHERE agent_id = ? AND id > ? AND id <= ?
                    ORDER BY id ASC
                    """,
                    (agent_id, after_id, before_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, agent_id, memory_scope, role, content, timestamp, metadata
                    FROM messages
                    WHERE agent_id = ? AND memory_scope = ? AND id > ? AND id <= ?
                    ORDER BY id ASC
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
                        """
                        SELECT m.id, m.agent_id, m.memory_scope, m.role, m.content, m.timestamp, m.metadata
                        FROM messages_fts f
                        JOIN messages m ON m.id = f.rowid
                        WHERE messages_fts MATCH ?
                        ORDER BY m.timestamp DESC, m.id DESC
                        LIMIT ?
                        """,
                        (query, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT m.id, m.agent_id, m.memory_scope, m.role, m.content, m.timestamp, m.metadata
                        FROM messages_fts f
                        JOIN messages m ON m.id = f.rowid
                        WHERE messages_fts MATCH ? AND m.memory_scope = ?
                        ORDER BY m.timestamp DESC, m.id DESC
                        LIMIT ?
                        """,
                        (query, memory_scope, limit),
                    ).fetchall()
            except sqlite3.OperationalError:
                if memory_scope is None:
                    rows = conn.execute(
                        """
                        SELECT id, agent_id, memory_scope, role, content, timestamp, metadata
                        FROM messages
                        WHERE content LIKE ?
                        ORDER BY timestamp DESC, id DESC
                        LIMIT ?
                        """,
                        (f"%{keyword}%", limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT id, agent_id, memory_scope, role, content, timestamp, metadata
                        FROM messages
                        WHERE content LIKE ? AND memory_scope = ?
                        ORDER BY timestamp DESC, id DESC
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
                    """
                    SELECT id, agent_id, memory_scope, role, content, timestamp, metadata
                    FROM messages
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, agent_id, memory_scope, role, content, timestamp, metadata
                    FROM messages
                    WHERE memory_scope = ?
                    ORDER BY timestamp DESC, id DESC
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
