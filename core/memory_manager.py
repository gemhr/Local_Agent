#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""会话记忆持久化模块。"""

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional


class MemoryManager:
    """封装基于 SQLite 的消息存储与检索能力。"""

    def __init__(self, db_path: Optional[str] = None) -> None:
        """初始化消息数据库。"""
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
        """初始化消息表、索引、摘要表、全文搜索表和触发器。"""
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
                "CREATE INDEX IF NOT EXISTS idx_messages_agent_time "
                "ON messages(agent_id, timestamp DESC, id DESC)"
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
        """将查询结果中的单行记录转换为消息字典。"""
        metadata = row["metadata"]
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
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
    ) -> int:
        """写入一条新消息。"""
        meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO messages (agent_id, role, content, metadata) VALUES (?, ?, ?, ?)",
                (agent_id, role, content, meta_json),
            )
            return int(cursor.lastrowid)

    def count_messages(self, agent_id: str) -> int:
        """统计某个智能体当前的消息数量。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(1) AS total FROM messages WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        return int(row["total"]) if row else 0

    def get_chat_history(
        self,
        agent_id: str,
        limit: int = 10,
        offset: int = 0,
        ascending: bool = False,
    ) -> List[Dict[str, Any]]:
        """分页读取某个智能体的历史消息。"""
        order = "ASC" if ascending else "DESC"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, agent_id, role, content, timestamp, metadata
                FROM messages
                WHERE agent_id = ?
                ORDER BY timestamp {order}, id {order}
                LIMIT ? OFFSET ?
                """,
                (agent_id, limit, offset),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def get_summary_record(self, agent_id: str) -> Dict[str, Any]:
        """返回指定智能体的摘要记录。"""
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
    ) -> List[Dict[str, Any]]:
        """读取用于生成摘要的消息片段。"""
        if before_id <= after_id:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, agent_id, role, content, timestamp, metadata
                FROM messages
                WHERE agent_id = ? AND id > ? AND id <= ?
                ORDER BY id ASC
                """,
                (agent_id, after_id, before_id),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def search_messages(self, keyword: str, limit: int = 50) -> List[Dict[str, Any]]:
        """按关键词搜索消息。"""
        query = " ".join(keyword.split()).strip()
        if not query:
            return []

        with self._connect() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT m.id, m.agent_id, m.role, m.content, m.timestamp, m.metadata
                    FROM messages_fts f
                    JOIN messages m ON m.id = f.rowid
                    WHERE messages_fts MATCH ?
                    ORDER BY m.timestamp DESC, m.id DESC
                    LIMIT ?
                    """,
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = conn.execute(
                    """
                    SELECT id, agent_id, role, content, timestamp, metadata
                    FROM messages
                    WHERE content LIKE ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                    """,
                    (f"%{keyword}%", limit),
                ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def get_all_messages(self, limit: int = 500) -> List[Dict[str, Any]]:
        """返回最近一批持久化消息。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, agent_id, role, content, timestamp, metadata
                FROM messages
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def delete_messages(self, message_ids: List[int]) -> None:
        """批量删除指定消息。

        由于滚动摘要依赖完整的历史消息序列，只要某个智能体有消息被删除，
        就直接丢弃该智能体的摘要缓存，避免后续推理继续引用已删除内容。
        """
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT agent_id FROM messages WHERE id IN ({placeholders})",
                message_ids,
            ).fetchall()
            conn.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", message_ids)
            agent_ids = [row["agent_id"] for row in rows]
            if agent_ids:
                summary_placeholders = ",".join("?" for _ in agent_ids)
                conn.execute(
                    f"DELETE FROM conversation_summaries WHERE agent_id IN ({summary_placeholders})",
                    agent_ids,
                )

    def clear_all_memory(self) -> None:
        """清空全部消息、摘要和全文索引。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM conversation_summaries")
            conn.execute("DELETE FROM messages_fts")
