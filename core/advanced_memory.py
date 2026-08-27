#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Advanced Long-term Memory Domain + SQLite Persistence Foundation（WP1-B）。

Conversation History（`messages` / `message_exchanges` /
`conversation_summaries` / `messages_fts`）与 Long-term Memory 是两个独立
Domain；本模块只实现 Long-term Memory 的 typed record、validation 与窄
persistence boundary。不接入 Formation / Retrieval / Context Injection。

- 公共 create 只接受 `SEMANTIC` + `ACTIVE`；`SUPERSEDED` / `FORGOTTEN`
  只是 lifecycle-capable persistence vocabulary（WP3 前不做状态转换）。
- `memory_id` 由应用生成、immutable、opaque；不由 content / logical_key /
  row order / auto-increment 推导。
- SQLite canonical record 是唯一 Source of Truth；本 WP 不实现任何 derived
  index。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional
from uuid import uuid4

LONG_TERM_MEMORY_TABLE = "long_term_memory"


class MemoryType(str, Enum):
    """WP1 v1 只冻结 SEMANTIC；EPISODIC 由 WP6 显式扩展，PROCEDURAL 不入 enum。"""

    SEMANTIC = "SEMANTIC"


class MemoryStatus(str, Enum):
    """Lifecycle-capable vocabulary。WP1 公共 create 只允许创建 ACTIVE。"""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    FORGOTTEN = "FORGOTTEN"


class MemoryErrorCode:
    INVALID_ARGUMENT = "MEMORY_INVALID_ARGUMENT"
    UNSUPPORTED_TYPE = "MEMORY_UNSUPPORTED_TYPE"
    UNSUPPORTED_STATUS = "MEMORY_UNSUPPORTED_STATUS"
    PUBLIC_CREATE_ACTIVE_ONLY = "MEMORY_PUBLIC_CREATE_ACTIVE_ONLY"
    DUPLICATE_CONFLICT = "MEMORY_DUPLICATE_CONFLICT"
    INVALID_SUPERSEDE_SELF = "MEMORY_INVALID_SUPERSEDE_SELF"
    NOT_FOUND = "MEMORY_NOT_FOUND"
    PERSISTENCE_FAILED = "MEMORY_PERSISTENCE_FAILED"


_MEMORY_ERROR_MESSAGES = {
    MemoryErrorCode.INVALID_ARGUMENT: "invalid advanced memory argument",
    MemoryErrorCode.UNSUPPORTED_TYPE: "unsupported memory type",
    MemoryErrorCode.UNSUPPORTED_STATUS: "unsupported memory status",
    MemoryErrorCode.PUBLIC_CREATE_ACTIVE_ONLY: "public create only accepts ACTIVE records",
    MemoryErrorCode.DUPLICATE_CONFLICT: "memory_id already exists with a different record",
    MemoryErrorCode.INVALID_SUPERSEDE_SELF: "superseded_by_memory_id must not reference itself",
    MemoryErrorCode.NOT_FOUND: "advanced memory record not found",
    MemoryErrorCode.PERSISTENCE_FAILED: "advanced memory persistence failed",
}


class MemoryDomainError(RuntimeError):
    """类型化 Advanced Memory domain 错误；不暴露 SQL / 路径 / 正文。"""

    def __init__(self, error_code: str, safe_message: Optional[str] = None) -> None:
        self.error_code = error_code
        self.safe_message = safe_message or _MEMORY_ERROR_MESSAGES[error_code]
        super().__init__(f"{self.safe_message} (error_code={error_code})")

    def __repr__(self) -> str:
        return f"MemoryDomainError(error_code={self.error_code!r})"


def _require_non_empty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryDomainError(
            MemoryErrorCode.INVALID_ARGUMENT, f"{name} 必须是非空字符串"
        )
    return value


def _require_utc(value: Any, name: str) -> None:
    if not isinstance(value, datetime):
        raise MemoryDomainError(
            MemoryErrorCode.INVALID_ARGUMENT, f"{name} 必须是 datetime"
        )
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise MemoryDomainError(
            MemoryErrorCode.INVALID_ARGUMENT, f"{name} 必须是带时区的 UTC 时间"
        )


def _parse_enum(enum_cls: type, value: Any, error_code: str, message: str):
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        raise MemoryDomainError(error_code, message) from None


@dataclass(frozen=True)
class MemoryOrigin:
    """最低 provenance：真实 run / exchange / entry-agent / scope 来源。

    只保存业务可归因来源；不保存 raw prompt、CoT、provider exception、
    文件路径或 tool payload。
    """

    origin_type: str
    origin_run_id: str
    origin_exchange_id: str
    origin_agent_id: str
    origin_memory_scope: str
    formation_method: Optional[str] = None

    def __post_init__(self) -> None:
        _require_non_empty(self.origin_type, "origin_type")
        _require_non_empty(self.origin_run_id, "origin_run_id")
        _require_non_empty(self.origin_exchange_id, "origin_exchange_id")
        _require_non_empty(self.origin_agent_id, "origin_agent_id")
        _require_non_empty(self.origin_memory_scope, "origin_memory_scope")
        if self.formation_method is not None:
            _require_non_empty(self.formation_method, "formation_method")


@dataclass(frozen=True)
class SemanticMemoryRecord:
    """不可变 SEMANTIC Long-term Memory record。

    - `memory_id`：应用生成 opaque identity，创建后不可变；
    - `canonical_text` + `payload`：同一条事实的两种表示；
    - `status`：WP1 公共 create 只允许 ACTIVE；SUPERSEDED / FORGOTTEN 仅作
      lifecycle-capable persistence vocabulary（经 test-only fixture 验证）；
    - `superseded_by_memory_id`：唯一预留 relation，不允许 self-reference。
    """

    memory_id: str
    agent_id: str
    memory_scope: str
    canonical_text: str
    payload: Dict[str, Any]
    origin: MemoryOrigin
    memory_type: MemoryType = MemoryType.SEMANTIC
    status: MemoryStatus = MemoryStatus.ACTIVE
    logical_key: Optional[str] = None
    superseded_by_memory_id: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _require_non_empty(self.memory_id, "memory_id")
        _require_non_empty(self.agent_id, "agent_id")
        _require_non_empty(self.memory_scope, "memory_scope")
        _require_non_empty(self.canonical_text, "canonical_text")
        object.__setattr__(
            self,
            "memory_type",
            _parse_enum(
                MemoryType,
                self.memory_type,
                MemoryErrorCode.UNSUPPORTED_TYPE,
                "v1 只支持 SEMANTIC memory_type",
            ),
        )
        object.__setattr__(
            self,
            "status",
            _parse_enum(
                MemoryStatus,
                self.status,
                MemoryErrorCode.UNSUPPORTED_STATUS,
                "未知 memory status",
            ),
        )
        if not isinstance(self.payload, dict):
            raise MemoryDomainError(
                MemoryErrorCode.INVALID_ARGUMENT, "payload 必须是 JSON object"
            )
        try:
            json.dumps(self.payload, ensure_ascii=False)
        except (TypeError, ValueError):
            raise MemoryDomainError(
                MemoryErrorCode.INVALID_ARGUMENT,
                "payload 必须是可 JSON 序列化的 object",
            ) from None
        if not isinstance(self.origin, MemoryOrigin):
            raise MemoryDomainError(
                MemoryErrorCode.INVALID_ARGUMENT, "origin 必须是 MemoryOrigin"
            )
        if self.logical_key is not None:
            _require_non_empty(self.logical_key, "logical_key")
        if self.superseded_by_memory_id is not None:
            _require_non_empty(self.superseded_by_memory_id, "superseded_by_memory_id")
            if self.superseded_by_memory_id == self.memory_id:
                raise MemoryDomainError(
                    MemoryErrorCode.INVALID_SUPERSEDE_SELF,
                    "superseded_by_memory_id 不能引用自身",
                )
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")


def _canonical_json(payload: Dict[str, Any]) -> str:
    """确定性 JSON 序列化：sort_keys + 紧凑分隔符，避免 key 顺序漂移。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


# 静态 SQL 常量：scanner 要求 execute 语句必须是字面量或模块级常量。
_SQL_SELECT_LTM_AGENT = (
    "SELECT * FROM long_term_memory WHERE agent_id = ? "
    "ORDER BY created_at DESC, memory_id ASC"
)
_SQL_SELECT_LTM_AGENT_SCOPE = (
    "SELECT * FROM long_term_memory WHERE agent_id = ? AND memory_scope = ? "
    "ORDER BY created_at DESC, memory_id ASC"
)
_SQL_SELECT_LTM_AGENT_ACTIVE = (
    "SELECT * FROM long_term_memory WHERE agent_id = ? AND status = ? "
    "ORDER BY created_at DESC, memory_id ASC"
)
_SQL_SELECT_LTM_AGENT_SCOPE_ACTIVE = (
    "SELECT * FROM long_term_memory WHERE agent_id = ? AND memory_scope = ? "
    "AND status = ? ORDER BY created_at DESC, memory_id ASC"
)


class AdvancedMemoryStore:
    """Long-term Memory 窄 persistence boundary → SQLite。

    只提供最小 create / read / query foundation；不提供状态转换、检索、
    supersede 或 forget 操作（属于 WP3+）。

    该 store 不拥有 schema truth：它要求 `db_path` 指向已由 MemoryManager
    初始化（或已通过显式 migration）的 v2 数据库；Conversation API 与
    Advanced Memory API 共享同一 SQLite 文件，但属不同 Domain。
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        default_dir = os.path.join(project_root, "data", "database")
        os.makedirs(default_dir, exist_ok=True)
        self.db_path = db_path or os.path.join(default_dir, "agent_memory.db")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """只读操作连接：完成即 commit 并关闭。"""
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

    # ------------------------------------------------------------------
    # Create（public：仅 ACTIVE SEMANTIC；单事务原子）
    # ------------------------------------------------------------------

    def create(self, record: SemanticMemoryRecord) -> SemanticMemoryRecord:
        """原子创建一条 ACTIVE SEMANTIC Long-term Memory。

        幂等 contract（按 `memory_id`）：
        - 同一 `memory_id` + 完全相同 canonical business record → 返回已存在记录；
        - 同一 `memory_id` + 任一业务字段不同 → typed reject，绝不覆盖旧 row。
        不按 content / logical_key 自动 dedup（属于 WP2/WP3 policy）。
        """
        if not isinstance(record, SemanticMemoryRecord):
            raise TypeError("create 需要 SemanticMemoryRecord")
        if record.memory_type is not MemoryType.SEMANTIC:
            raise MemoryDomainError(
                MemoryErrorCode.UNSUPPORTED_TYPE,
                "v1 公共 create 只接受 SEMANTIC memory_type",
            )
        if record.status is not MemoryStatus.ACTIVE:
            raise MemoryDomainError(
                MemoryErrorCode.PUBLIC_CREATE_ACTIVE_ONLY,
                "公共 create 只允许创建 ACTIVE record",
            )
        if record.superseded_by_memory_id is not None:
            raise MemoryDomainError(
                MemoryErrorCode.INVALID_ARGUMENT,
                "ACTIVE record 不能携带 superseded_by_memory_id",
            )
        try:
            with self._transaction() as conn:
                existing = self._fetch_row(conn, record.memory_id)
                if existing is not None:
                    if self._same_business_record(existing, record):
                        return self._row_to_record(existing)
                    raise MemoryDomainError(
                        MemoryErrorCode.DUPLICATE_CONFLICT,
                        "memory_id 已存在且 canonical record 不同，拒绝覆盖",
                    )
                self._insert_row(conn, record)
                return record
        except MemoryDomainError:
            raise
        except sqlite3.Error:
            raise MemoryDomainError(
                MemoryErrorCode.PERSISTENCE_FAILED
            ) from None

    def _insert_row(self, conn: sqlite3.Connection, record: SemanticMemoryRecord) -> None:
        conn.execute(
            """
            INSERT INTO long_term_memory (
                memory_id, memory_type, status, agent_id, memory_scope,
                canonical_text, payload, logical_key,
                origin_type, origin_run_id, origin_exchange_id,
                origin_agent_id, origin_memory_scope, formation_method,
                created_at, updated_at, superseded_by_memory_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.memory_id,
                record.memory_type.value,
                record.status.value,
                record.agent_id,
                record.memory_scope,
                record.canonical_text,
                _canonical_json(record.payload),
                record.logical_key,
                record.origin.origin_type,
                record.origin.origin_run_id,
                record.origin.origin_exchange_id,
                record.origin.origin_agent_id,
                record.origin.origin_memory_scope,
                record.origin.formation_method,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
                record.superseded_by_memory_id,
            ),
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_by_memory_id(self, memory_id: str) -> SemanticMemoryRecord:
        """按 stable `memory_id` 读取 canonical record（status-inclusive）。

        这是显式 identity 读取，用于 inspection / deterministic tests；
        不是面向 retrieval 的默认查询。
        """
        _require_non_empty(memory_id, "memory_id")
        try:
            with self._connect() as conn:
                row = self._fetch_row(conn, memory_id)
        except sqlite3.Error:
            raise MemoryDomainError(
                MemoryErrorCode.PERSISTENCE_FAILED
            ) from None
        if row is None:
            raise MemoryDomainError(
                MemoryErrorCode.NOT_FOUND, "advanced memory record not found"
            )
        return self._row_to_record(row)

    def list_by_agent(
        self,
        agent_id: str,
        *,
        memory_scope: Optional[str] = None,
        active_only: bool = True,
    ) -> List[SemanticMemoryRecord]:
        """按真实 agent partition 的基础读取。

        - 默认 `active_only=True`：面向未来 retrieval，只返回 ACTIVE；
        - `active_only=False`：status-inclusive，用于 lifecycle inspection /
          deterministic tests，不构成 retrieval。
        """
        _require_non_empty(agent_id, "agent_id")
        if memory_scope is not None:
            _require_non_empty(memory_scope, "memory_scope")
        if not isinstance(active_only, bool):
            raise TypeError("active_only 必须是 bool")
        try:
            with self._connect() as conn:
                if memory_scope is not None and active_only:
                    rows = conn.execute(
                        _SQL_SELECT_LTM_AGENT_SCOPE_ACTIVE,
                        [agent_id, memory_scope, MemoryStatus.ACTIVE.value],
                    ).fetchall()
                elif memory_scope is not None:
                    rows = conn.execute(
                        _SQL_SELECT_LTM_AGENT_SCOPE, [agent_id, memory_scope]
                    ).fetchall()
                elif active_only:
                    rows = conn.execute(
                        _SQL_SELECT_LTM_AGENT_ACTIVE,
                        [agent_id, MemoryStatus.ACTIVE.value],
                    ).fetchall()
                else:
                    rows = conn.execute(
                        _SQL_SELECT_LTM_AGENT, [agent_id]
                    ).fetchall()
        except sqlite3.Error:
            raise MemoryDomainError(
                MemoryErrorCode.PERSISTENCE_FAILED
            ) from None
        return [self._row_to_record(row) for row in rows]

    # ------------------------------------------------------------------
    # row <-> record mapping / comparison
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_row(
        conn: sqlite3.Connection, memory_id: str
    ) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM long_term_memory WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()

    @staticmethod
    def _same_business_record(
        row: sqlite3.Row, record: SemanticMemoryRecord
    ) -> bool:
        """幂等比较：canonical business 字段全部相同才算 equivalent。

        timestamps 与 lifecycle relation 也是已持久化 Business Contract；调用方
        重试时必须复用完整 record。任一字段不同都不是幂等重放。
        """
        if row["memory_type"] != record.memory_type.value:
            return False
        if row["status"] != record.status.value:
            return False
        if row["agent_id"] != record.agent_id:
            return False
        if row["memory_scope"] != record.memory_scope:
            return False
        if row["canonical_text"] != record.canonical_text:
            return False
        try:
            if json.loads(row["payload"]) != record.payload:
                return False
        except (TypeError, ValueError):
            return False
        if row["logical_key"] != record.logical_key:
            return False
        if row["origin_type"] != record.origin.origin_type:
            return False
        if row["origin_run_id"] != record.origin.origin_run_id:
            return False
        if row["origin_exchange_id"] != record.origin.origin_exchange_id:
            return False
        if row["origin_agent_id"] != record.origin.origin_agent_id:
            return False
        if row["origin_memory_scope"] != record.origin.origin_memory_scope:
            return False
        if row["formation_method"] != record.origin.formation_method:
            return False
        if row["created_at"] != record.created_at.isoformat():
            return False
        if row["updated_at"] != record.updated_at.isoformat():
            return False
        if row["superseded_by_memory_id"] != record.superseded_by_memory_id:
            return False
        return True

    def _row_to_record(self, row: sqlite3.Row) -> SemanticMemoryRecord:
        return SemanticMemoryRecord(
            memory_id=row["memory_id"],
            memory_type=MemoryType(row["memory_type"]),
            status=MemoryStatus(row["status"]),
            agent_id=row["agent_id"],
            memory_scope=row["memory_scope"],
            canonical_text=row["canonical_text"],
            payload=json.loads(row["payload"]),
            logical_key=row["logical_key"],
            origin=MemoryOrigin(
                origin_type=row["origin_type"],
                origin_run_id=row["origin_run_id"],
                origin_exchange_id=row["origin_exchange_id"],
                origin_agent_id=row["origin_agent_id"],
                origin_memory_scope=row["origin_memory_scope"],
                formation_method=row["formation_method"],
            ),
            created_at=_parse_utc(row["created_at"]),
            updated_at=_parse_utc(row["updated_at"]),
            superseded_by_memory_id=row["superseded_by_memory_id"],
        )


__all__ = [
    "AdvancedMemoryStore",
    "LONG_TERM_MEMORY_TABLE",
    "MemoryDomainError",
    "MemoryErrorCode",
    "MemoryOrigin",
    "MemoryStatus",
    "MemoryType",
    "SemanticMemoryRecord",
]
