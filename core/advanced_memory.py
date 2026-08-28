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
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
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
    MALFORMED_KEYED_PAYLOAD = "MEMORY_MALFORMED_KEYED_PAYLOAD"
    FORGET_INVALID_TARGET = "MEMORY_FORGET_INVALID_TARGET"


_MEMORY_ERROR_MESSAGES = {
    MemoryErrorCode.INVALID_ARGUMENT: "invalid advanced memory argument",
    MemoryErrorCode.UNSUPPORTED_TYPE: "unsupported memory type",
    MemoryErrorCode.UNSUPPORTED_STATUS: "unsupported memory status",
    MemoryErrorCode.PUBLIC_CREATE_ACTIVE_ONLY: "public create only accepts ACTIVE records",
    MemoryErrorCode.DUPLICATE_CONFLICT: "memory_id already exists with a different record",
    MemoryErrorCode.INVALID_SUPERSEDE_SELF: "superseded_by_memory_id must not reference itself",
    MemoryErrorCode.NOT_FOUND: "advanced memory record not found",
    MemoryErrorCode.PERSISTENCE_FAILED: "advanced memory persistence failed",
    MemoryErrorCode.MALFORMED_KEYED_PAYLOAD: (
        "keyed history row payload must be exactly {\"value\": scalar}"
    ),
    MemoryErrorCode.FORGET_INVALID_TARGET: (
        "forget target logical_key is not a valid existing partition key"
    ),
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
# WP3-B keyed lifecycle partition read（status-inclusive；deterministic order）。
_SQL_SELECT_LTM_PARTITION = (
    "SELECT * FROM long_term_memory WHERE agent_id = ? AND memory_scope = ? "
    "AND memory_type = ? AND logical_key = ? "
    "ORDER BY created_at ASC, memory_id ASC"
)
# WP4-B retrieval 窄读 primitive：固定 ACTIVE+SEMANTIC+agent/scope partition，
# bounded LIMIT；SQL 不接受任意 where/order/prompt 参数。
_SQL_SELECT_LTM_ACTIVE_SEMANTIC_SCOPE = (
    "SELECT * FROM long_term_memory WHERE agent_id = ? AND memory_scope = ? "
    "AND memory_type = ? AND status = ? "
    "ORDER BY created_at DESC, memory_id ASC LIMIT ?"
)
# WP3-B forget targeting allowlist：同 agent/scope/type 下现有 distinct key。
_SQL_SELECT_LTM_DISTINCT_KEYS = (
    "SELECT DISTINCT logical_key FROM long_term_memory "
    "WHERE agent_id = ? AND memory_scope = ? AND memory_type = ? "
    "AND logical_key IS NOT NULL ORDER BY logical_key ASC"
)
_SQL_UPDATE_LTM_SUPERSEDE = (
    "UPDATE long_term_memory SET status = ?, superseded_by_memory_id = ?, "
    "updated_at = ? WHERE memory_id = ?"
)
_SQL_UPDATE_LTM_RELATION = (
    "UPDATE long_term_memory SET superseded_by_memory_id = ?, updated_at = ? "
    "WHERE memory_id = ?"
)
_SQL_UPDATE_LTM_REDACT = (
    "UPDATE long_term_memory SET status = ?, canonical_text = ?, payload = ?, "
    "superseded_by_memory_id = ?, updated_at = ? WHERE memory_id = ?"
)


#: Schema v2 固定的 forget tombstone representation（WP3-B 冻结）。
FORGET_TOMBSTONE_TEXT = "[FORGOTTEN]"


class LifecycleOperation(str, Enum):
    """WP3-B keyed Semantic Memory 生命周期决策操作。"""

    INSERT = "INSERT"
    NO_CHANGE = "NO_CHANGE"
    SUPERSEDE = "SUPERSEDE"
    FORGET = "FORGET"


@dataclass(frozen=True)
class ActiveSemanticScopeRead:
    """WP4-B 窄读 primitive 的只读结果。

    ``records`` 是已通过 domain validation 的 ACTIVE SEMANTIC rows；
    ``malformed_count`` 是无法安全投影而被丢弃的 row 数（safe evidence，
    不含正文）。本类型只是读取投影，不是新的 Authority。
    """

    records: Tuple[SemanticMemoryRecord, ...]
    malformed_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.records, tuple) or any(
            not isinstance(r, SemanticMemoryRecord) for r in self.records
        ):
            raise MemoryDomainError(
                MemoryErrorCode.INVALID_ARGUMENT,
                "records 必须是 SemanticMemoryRecord tuple",
            )
        if isinstance(self.malformed_count, bool) or not isinstance(
            self.malformed_count, int
        ):
            raise MemoryDomainError(
                MemoryErrorCode.INVALID_ARGUMENT,
                "malformed_count 必须是非负整数",
            )
        if self.malformed_count < 0:
            raise MemoryDomainError(
                MemoryErrorCode.INVALID_ARGUMENT,
                "malformed_count 必须是非负整数",
            )


@dataclass(frozen=True)
class MemoryTransition:
    """单条 row 的状态迁移证据（bounded；deterministic）。"""

    memory_id: str
    before_status: str
    after_status: str


@dataclass(frozen=True)
class LifecycleResolutionResult:
    """WP3-B typed lifecycle outcome。

    ``outcome`` 是 safe lifecycle outcome（``OK`` / ``NOT_FOUND`` /
    ``ALREADY_FORGOTTEN``），与 ``MemoryStatus`` 分离：NOT_FOUND 不伪装成
    某条记录的状态。business Authority 始终是 SQLite row；本结果只是观察。
    """

    operation: LifecycleOperation
    outcome: str
    candidate_outcome: Optional[str]
    winner_memory_id: Optional[str]
    new_memory_id: Optional[str]
    affected_transitions: Tuple[MemoryTransition, ...]
    affected_count: int
    ids_truncated: bool
    omitted_count: int
    safe_reason: str
    safe_error_code: Optional[str]
    resolution_duration_ms: int
    mutation_duration_ms: int


@dataclass(frozen=True)
class _LifecyclePlan:
    """resolver 产出的窄 mutation plan；Store 在 BEGIN IMMEDIATE 内校验并应用。

    不携带正文 / payload / query；只引用 memory_id、status、relation 与
    tombstone 行为。Store 不在此重新实现 lifecycle policy。
    """

    operation: LifecycleOperation
    outcome: str
    candidate_outcome: Optional[str]
    insert: Optional[SemanticMemoryRecord]
    winner_memory_id: Optional[str]
    supersede_rows: Tuple[str, ...]
    repoint_rows: Tuple[str, ...]
    forget_rows: Tuple[str, ...]
    mutation_timestamp: Optional[datetime]
    transitions: Tuple[MemoryTransition, ...]


def _scalar_kind(value: object) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return "other"


def typed_values_equal(a: object, b: object) -> bool:
    """WP3-B 冻结的 deterministic typed equality（只比较 payload["value"]）。

    - string：只 trim 首尾 whitespace；case-sensitive；
    - int：非 bool int，值相同；
    - float：finite float，值相同；
    - bool：strict bool；
    - cross-type：一律不同（因此 1 != 1.0，True != 1）。
    """
    ka, kb = _scalar_kind(a), _scalar_kind(b)
    if ka != kb:
        return False
    if ka == "str":
        return a.strip() == b.strip()
    if ka == "float":
        return math.isfinite(a) and math.isfinite(b) and a == b
    return a == b


def _extract_scalar(payload: object) -> Optional[Tuple[object, str]]:
    """payload 精确为 ``{"value": <scalar>}`` 时返回 ``(value, kind)``，否则 None。"""
    if not isinstance(payload, dict):
        return None
    if set(payload.keys()) != {"value"}:
        return None
    value = payload["value"]
    kind = _scalar_kind(value)
    if kind == "other":
        return None
    if kind == "float" and not math.isfinite(value):
        return None
    return value, kind


def _normalize_candidate_string_value(record: SemanticMemoryRecord) -> SemanticMemoryRecord:
    """新 string candidate 在 authoritative record preparation 前 trim。"""
    value = record.payload.get("value")
    if isinstance(value, str):
        return replace(record, payload={"value": value.strip()})
    return record


def _is_safe_forget_tombstone(row: sqlite3.Row) -> bool:
    if row["status"] != MemoryStatus.FORGOTTEN.value:
        return False
    if row["canonical_text"] != FORGET_TOMBSTONE_TEXT:
        return False
    try:
        if json.loads(row["payload"]) != {}:
            return False
    except (TypeError, ValueError):
        return False
    if row["superseded_by_memory_id"] is not None:
        return False
    return True


class MemoryLifecycleResolver:
    """WP3-B 唯一 keyed Semantic Memory lifecycle decision Owner。

    职责（纯函数）：typed equality、winner selection、lifecycle decision、
    窄 mutation plan 准备。不执行 SQL、不拥有 connection、不实现另一套
    persistence；AdvancedMemoryStore 负责在 BEGIN IMMEDIATE 事务内
    read / validate / apply。

    v1 conflict partition：
        (agent_id, memory_scope, memory_type=SEMANTIC, logical_key)
    """

    # -- remember resolution -------------------------------------------------

    @classmethod
    def resolve_remember(
        cls,
        candidate: SemanticMemoryRecord,
        rows: Sequence[sqlite3.Row],
        *,
        mutation_time: datetime,
    ) -> _LifecyclePlan:
        """基于 partition snapshot（status-inclusive）对 candidate 决策。"""
        if candidate.logical_key is None:
            return _LifecyclePlan(
                operation=LifecycleOperation.INSERT,
                outcome="OK",
                candidate_outcome="PERSISTED",
                insert=_normalize_candidate_string_value(candidate),
                winner_memory_id=candidate.memory_id,
                supersede_rows=(),
                repoint_rows=(),
                forget_rows=(),
                mutation_timestamp=None,
                transitions=(),
            )
        candidate_scalar = _extract_scalar(candidate.payload)
        if candidate_scalar is None:
            raise MemoryDomainError(
                MemoryErrorCode.INVALID_ARGUMENT,
                "candidate payload 必须是精确 {\"value\": scalar}",
            )
        candidate_value, _ = candidate_scalar
        # 历史 keyed row（非 FORGOTTEN）必须满足精确 {"value": scalar}，否则
        # typed fail closed（零 mutation）。FORGOTTEN tombstone 是 redacted row，
        # 不参与 remember 比较，也不允许被当作畸形正文阻塞新事实。
        non_forgotten = [r for r in rows if r["status"] != MemoryStatus.FORGOTTEN.value]
        scalar_by_id: dict[str, object] = {}
        for row in non_forgotten:
            try:
                scalar = _extract_scalar(json.loads(row["payload"]))
            except (TypeError, ValueError):
                scalar = None
            if scalar is None:
                raise MemoryDomainError(
                    MemoryErrorCode.MALFORMED_KEYED_PAYLOAD,
                    "keyed 历史 row payload 必须是精确 {\"value\": scalar}",
                )
            scalar_by_id[row["memory_id"]] = scalar[0]
        active = [r for r in non_forgotten if r["status"] == MemoryStatus.ACTIVE.value]
        if not active:
            winner = candidate.memory_id
            repoint = tuple(
                r["memory_id"]
                for r in non_forgotten
                if r["status"] == MemoryStatus.SUPERSEDED.value
                and r["superseded_by_memory_id"] != winner
            )
            return _LifecyclePlan(
                operation=LifecycleOperation.INSERT,
                outcome="OK",
                candidate_outcome="PERSISTED",
                insert=_normalize_candidate_string_value(candidate),
                winner_memory_id=winner,
                supersede_rows=(),
                repoint_rows=repoint,
                forget_rows=(),
                mutation_timestamp=mutation_time if repoint else None,
                transitions=tuple(
                    MemoryTransition(
                        mid,
                        MemoryStatus.SUPERSEDED.value,
                        MemoryStatus.SUPERSEDED.value,
                    )
                    for mid in repoint
                ),
            )
        equivalent = [
            r
            for r in active
            if typed_values_equal(scalar_by_id[r["memory_id"]], candidate_value)
        ]
        if not equivalent:
            # candidate 成为新 ACTIVE winner；全部旧 ACTIVE 被取代并直接指向它。
            winner = candidate.memory_id
            supersede = tuple(r["memory_id"] for r in active)
            repoint = tuple(
                r["memory_id"]
                for r in non_forgotten
                if r["status"] == MemoryStatus.SUPERSEDED.value
                and r["superseded_by_memory_id"] != winner
            )
            transitions = tuple(
                MemoryTransition(r["memory_id"], MemoryStatus.ACTIVE.value, MemoryStatus.SUPERSEDED.value)
                for r in active
            ) + tuple(
                MemoryTransition(
                    mid,
                    MemoryStatus.SUPERSEDED.value,
                    MemoryStatus.SUPERSEDED.value,
                )
                for mid in repoint
            )
            return _LifecyclePlan(
                operation=LifecycleOperation.SUPERSEDE,
                outcome="OK",
                candidate_outcome="PERSISTED",
                insert=_normalize_candidate_string_value(candidate),
                winner_memory_id=winner,
                supersede_rows=supersede,
                repoint_rows=repoint,
                forget_rows=(),
                mutation_timestamp=mutation_time,
                transitions=transitions,
            )
        # 至少一个等价 ACTIVE：deterministic existing winner
        winner_row = min(equivalent, key=lambda r: (r["created_at"], r["memory_id"]))
        winner = winner_row["memory_id"]
        other_active = tuple(
            r["memory_id"] for r in active if r["memory_id"] != winner
        )
        repoint = tuple(
            r["memory_id"]
            for r in non_forgotten
            if r["status"] == MemoryStatus.SUPERSEDED.value
            and r["superseded_by_memory_id"] != winner
        )
        is_clean = (
            len(equivalent) == 1
            and len(active) == 1
            and not other_active
            and not repoint
        )
        if is_clean:
            return _LifecyclePlan(
                operation=LifecycleOperation.NO_CHANGE,
                outcome="OK",
                candidate_outcome="NO_CHANGE",
                insert=None,
                winner_memory_id=winner,
                supersede_rows=(),
                repoint_rows=(),
                forget_rows=(),
                mutation_timestamp=None,
                transitions=(),
            )
        transitions = tuple(
            MemoryTransition(mid, MemoryStatus.ACTIVE.value, MemoryStatus.SUPERSEDED.value)
            for mid in other_active
        ) + tuple(
            MemoryTransition(
                mid,
                MemoryStatus.SUPERSEDED.value,
                MemoryStatus.SUPERSEDED.value,
            )
            for mid in repoint
        )
        return _LifecyclePlan(
            operation=LifecycleOperation.SUPERSEDE,
            outcome="OK",
            candidate_outcome="NO_CHANGE",
            insert=None,
            winner_memory_id=winner,
            supersede_rows=other_active,
            repoint_rows=repoint,
            forget_rows=(),
            mutation_timestamp=mutation_time,
            transitions=transitions,
        )

    # -- forget resolution ----------------------------------------------------

    @classmethod
    def resolve_forget(
        cls,
        rows: Sequence[sqlite3.Row],
        *,
        mutation_time: datetime,
    ) -> _LifecyclePlan:
        """对目标 partition 全历史版本决策 FORGET（all-version redaction）。"""
        if not rows:
            return _LifecyclePlan(
                operation=LifecycleOperation.FORGET,
                outcome="NOT_FOUND",
                candidate_outcome=None,
                insert=None,
                winner_memory_id=None,
                supersede_rows=(),
                repoint_rows=(),
                forget_rows=(),
                mutation_timestamp=None,
                transitions=(),
            )
        forget_ids: List[str] = []
        transitions: List[MemoryTransition] = []
        for r in rows:
            if _is_safe_forget_tombstone(r):
                continue
            before = r["status"]
            forget_ids.append(r["memory_id"])
            transitions.append(
                MemoryTransition(r["memory_id"], before, MemoryStatus.FORGOTTEN.value)
            )
        if not forget_ids:
            return _LifecyclePlan(
                operation=LifecycleOperation.NO_CHANGE,
                outcome="ALREADY_FORGOTTEN",
                candidate_outcome=None,
                insert=None,
                winner_memory_id=None,
                supersede_rows=(),
                repoint_rows=(),
                forget_rows=(),
                mutation_timestamp=None,
                transitions=(),
            )
        return _LifecyclePlan(
            operation=LifecycleOperation.FORGET,
            outcome="OK",
            candidate_outcome=None,
            insert=None,
            winner_memory_id=None,
            supersede_rows=(),
            repoint_rows=(),
            forget_rows=tuple(forget_ids),
            mutation_timestamp=mutation_time,
            transitions=tuple(transitions),
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

    def list_active_semantic_for_scope(
        self,
        agent_id: str,
        memory_scope: str,
        *,
        candidate_limit: int,
    ) -> ActiveSemanticScopeRead:
        """WP4-B retrieval 窄读 primitive（Store 不做 ranking / scoring）。

        固定约束：``agent_id`` exact、``memory_scope`` exact、
        ``memory_type=SEMANTIC``、``status=ACTIVE``，并带 bounded
        ``candidate_limit``。SQL 不接受任意 WHERE / ORDER BY / prompt 参数。
        无法安全投影的 malformed 历史 row 被丢弃并计入 ``malformed_count``，
        不阻塞其余候选。FORGOTTEN / SUPERSEDED row 由 SQL 谓词天然排除。
        """
        _require_non_empty(agent_id, "agent_id")
        _require_non_empty(memory_scope, "memory_scope")
        if isinstance(candidate_limit, bool) or not isinstance(
            candidate_limit, int
        ) or candidate_limit <= 0:
            raise MemoryDomainError(
                MemoryErrorCode.INVALID_ARGUMENT,
                "candidate_limit 必须是正整数",
            )
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    _SQL_SELECT_LTM_ACTIVE_SEMANTIC_SCOPE,
                    [
                        agent_id,
                        memory_scope,
                        MemoryType.SEMANTIC.value,
                        MemoryStatus.ACTIVE.value,
                        candidate_limit,
                    ],
                ).fetchall()
        except sqlite3.Error:
            raise MemoryDomainError(
                MemoryErrorCode.PERSISTENCE_FAILED
            ) from None
        records: List[SemanticMemoryRecord] = []
        malformed = 0
        for row in rows:
            try:
                records.append(self._row_to_record(row))
            except (MemoryDomainError, ValueError, TypeError):
                # malformed 历史 row：drop candidate，safe evidence，不阻塞其余候选。
                malformed += 1
        return ActiveSemanticScopeRead(
            records=tuple(records), malformed_count=malformed
        )

    # ------------------------------------------------------------------
    # WP3-B Lifecycle（keyed resolution + forget；BEGIN IMMEDIATE 内闭环）
    # ------------------------------------------------------------------

    def resolve_semantic(
        self, candidate: SemanticMemoryRecord
    ) -> LifecycleResolutionResult:
        """keyed Semantic Memory lifecycle resolution，单事务原子。

        BEGIN IMMEDIATE 内：partition read → resolver 决策 → plan 校验 →
        apply → post-state 校验 → COMMIT。任何一步失败 ROLLBACK ALL。

        - ``logical_key=None``：一律 INSERT；
        - keyed：按 ``(agent_id, memory_scope, SEMANTIC, logical_key)`` partition
          做 typed equality / winner / NO_CHANGE / SUPERSEDE。
        """
        if not isinstance(candidate, SemanticMemoryRecord):
            raise TypeError("resolve_semantic 需要 SemanticMemoryRecord")
        if candidate.memory_type is not MemoryType.SEMANTIC:
            raise MemoryDomainError(
                MemoryErrorCode.UNSUPPORTED_TYPE,
                "v1 lifecycle 只处理 SEMANTIC memory_type",
            )
        if candidate.status is not MemoryStatus.ACTIVE:
            raise MemoryDomainError(
                MemoryErrorCode.PUBLIC_CREATE_ACTIVE_ONLY,
                "lifecycle candidate 必须是 ACTIVE",
            )
        if candidate.superseded_by_memory_id is not None:
            raise MemoryDomainError(
                MemoryErrorCode.INVALID_ARGUMENT,
                "ACTIVE candidate 不能携带 superseded_by_memory_id",
            )
        candidate = _normalize_candidate_string_value(candidate)
        resolution_started = time.monotonic()
        try:
            with self._transaction() as conn:
                existing = self._fetch_row(conn, candidate.memory_id)
                if existing is not None:
                    if self._same_business_record(existing, candidate):
                        return self._build_lifecycle_result(
                            LifecycleOperation.NO_CHANGE,
                            "OK",
                            "REUSED",
                            winner_memory_id=candidate.memory_id,
                            new_memory_id=candidate.memory_id,
                            transitions=(),
                            affected_count=0,
                            safe_reason="IDEMPOTENT_REUSE",
                            safe_error_code=None,
                            resolution_started=resolution_started,
                            mutation_duration_ms=0,
                        )
                    raise MemoryDomainError(
                        MemoryErrorCode.DUPLICATE_CONFLICT,
                        "memory_id 已存在且 canonical record 不同，拒绝覆盖",
                    )
                if candidate.logical_key is None:
                    rows: Tuple[sqlite3.Row, ...] = ()
                else:
                    rows = self._select_partition(
                        conn,
                        candidate.agent_id,
                        candidate.memory_scope,
                        candidate.logical_key,
                    )
                plan = MemoryLifecycleResolver.resolve_remember(
                    candidate, rows, mutation_time=datetime.now(UTC)
                )
                self._validate_plan(plan, candidate, rows)
                mutation_started = time.monotonic()
                self._apply_plan(conn, plan)
                mutation_duration_ms = max(
                    0, int((time.monotonic() - mutation_started) * 1000)
                )
                self._validate_post_state(conn, candidate, plan)
                return self._build_lifecycle_result(
                    plan.operation,
                    plan.outcome,
                    plan.candidate_outcome,
                    winner_memory_id=plan.winner_memory_id,
                    new_memory_id=(
                        plan.insert.memory_id if plan.insert is not None else None
                    ),
                    transitions=plan.transitions,
                    affected_count=(
                        (1 if plan.insert is not None else 0)
                        + len(plan.supersede_rows)
                        + len(plan.repoint_rows)
                    ),
                    safe_reason=(
                        "NO_CHANGE"
                        if plan.operation is LifecycleOperation.NO_CHANGE
                        else plan.operation.value
                    ),
                    safe_error_code=None,
                    resolution_started=resolution_started,
                    mutation_duration_ms=mutation_duration_ms,
                )
        except MemoryDomainError:
            raise
        except sqlite3.Error:
            raise MemoryDomainError(MemoryErrorCode.PERSISTENCE_FAILED) from None

    def forget_semantic_partition(
        self,
        *,
        agent_id: str,
        memory_scope: str,
        logical_key: str,
        mutation_time: Optional[datetime] = None,
    ) -> LifecycleResolutionResult:
        """显式 forget 一个 logical slot 的全部历史版本（单事务原子）。

        目标 partition 的 read + all-version redaction + status update +
        relation cleanup 全在同一个 BEGIN IMMEDIATE 事务内；任一 row 失败
        ROLLBACK ALL。exact key 从未存在 → ``NOT_FOUND`` outcome，零 mutation。
        """
        _require_non_empty(agent_id, "agent_id")
        _require_non_empty(memory_scope, "memory_scope")
        _require_non_empty(logical_key, "logical_key")
        if mutation_time is None:
            mutation_time = datetime.now(UTC)
        else:
            _require_utc(mutation_time, "mutation_time")
        resolution_started = time.monotonic()
        try:
            with self._transaction() as conn:
                rows = self._select_partition(conn, agent_id, memory_scope, logical_key)
                plan = MemoryLifecycleResolver.resolve_forget(
                    rows, mutation_time=mutation_time
                )
                self._validate_plan(plan, None, rows)
                mutation_started = time.monotonic()
                self._apply_plan(conn, plan)
                mutation_duration_ms = max(
                    0, int((time.monotonic() - mutation_started) * 1000)
                )
                self._validate_post_state(conn, None, plan)
                return self._build_lifecycle_result(
                    plan.operation,
                    plan.outcome,
                    plan.candidate_outcome,
                    winner_memory_id=plan.winner_memory_id,
                    new_memory_id=(
                        plan.insert.memory_id if plan.insert is not None else None
                    ),
                    transitions=plan.transitions,
                    affected_count=len(plan.forget_rows),
                    safe_reason=(
                        "ALREADY_FORGOTTEN"
                        if plan.outcome == "ALREADY_FORGOTTEN"
                        else "NOT_FOUND"
                        if plan.outcome == "NOT_FOUND"
                        else "FORGET"
                    ),
                    safe_error_code=None,
                    resolution_started=resolution_started,
                    mutation_duration_ms=mutation_duration_ms,
                )
        except MemoryDomainError:
            raise
        except sqlite3.Error:
            raise MemoryDomainError(MemoryErrorCode.PERSISTENCE_FAILED) from None

    def list_logical_keys(
        self,
        agent_id: str,
        memory_scope: str,
        *,
        max_keys: int = 64,
    ) -> List[str]:
        """WP3-B forget targeting 用现有 logical-key allowlist（lifecycle lookup，
        不是 retrieval）：同 agent/scope/type 下 distinct existing key。

        仅用于 exact membership 校验；不返回正文 / payload / 排序语义 / 注入
        Context。超过 ``max_keys`` 时 fail closed（返回空），宁可不 forget 也
        不 fuzzy delete。
        """
        _require_non_empty(agent_id, "agent_id")
        _require_non_empty(memory_scope, "memory_scope")
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    _SQL_SELECT_LTM_DISTINCT_KEYS,
                    [agent_id, memory_scope, MemoryType.SEMANTIC.value],
                ).fetchall()
        except sqlite3.Error:
            raise MemoryDomainError(MemoryErrorCode.PERSISTENCE_FAILED) from None
        keys = [str(row["logical_key"]) for row in rows]
        if len(keys) > max_keys:
            return []
        return keys

    def _select_partition(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        memory_scope: str,
        logical_key: str,
    ) -> Tuple[sqlite3.Row, ...]:
        return tuple(
            conn.execute(
                _SQL_SELECT_LTM_PARTITION,
                [agent_id, memory_scope, MemoryType.SEMANTIC.value, logical_key],
            ).fetchall()
        )

    def _apply_plan(
        self, conn: sqlite3.Connection, plan: _LifecyclePlan
    ) -> None:
        ts = plan.mutation_timestamp
        if plan.insert is not None:
            self._insert_row(conn, plan.insert)
        for mid in plan.supersede_rows:
            conn.execute(
                _SQL_UPDATE_LTM_SUPERSEDE,
                (
                    MemoryStatus.SUPERSEDED.value,
                    plan.winner_memory_id,
                    ts.isoformat(),
                    mid,
                ),
            )
        for mid in plan.repoint_rows:
            conn.execute(
                _SQL_UPDATE_LTM_RELATION,
                (plan.winner_memory_id, ts.isoformat(), mid),
            )
        for mid in plan.forget_rows:
            conn.execute(
                _SQL_UPDATE_LTM_REDACT,
                (
                    MemoryStatus.FORGOTTEN.value,
                    FORGET_TOMBSTONE_TEXT,
                    "{}",
                    None,
                    ts.isoformat(),
                    mid,
                ),
            )

    def _validate_plan(
        self,
        plan: _LifecyclePlan,
        candidate: Optional[SemanticMemoryRecord],
        rows: Sequence[sqlite3.Row],
    ) -> None:
        """Store 侧窄 invariant 校验（不重实现 lifecycle policy）。"""
        if plan.operation in {
            LifecycleOperation.INSERT,
            LifecycleOperation.SUPERSEDE,
            LifecycleOperation.NO_CHANGE,
        } and plan.candidate_outcome is not None:
            if plan.winner_memory_id is None:
                raise MemoryDomainError(
                    MemoryErrorCode.INVALID_ARGUMENT, "lifecycle winner 缺失"
                )
            by_id = {r["memory_id"]: r for r in rows}
            if plan.insert is None:
                winner_row = by_id.get(plan.winner_memory_id)
                if winner_row is None or winner_row["status"] != MemoryStatus.ACTIVE.value:
                    raise MemoryDomainError(
                        MemoryErrorCode.INVALID_ARGUMENT,
                        "supersede winner 必须存在且 ACTIVE",
                    )
            else:
                if plan.winner_memory_id != plan.insert.memory_id:
                    raise MemoryDomainError(
                        MemoryErrorCode.INVALID_ARGUMENT,
                        "新 winner 必须是被插入的 candidate",
                    )
            for mid in plan.supersede_rows:
                row = by_id.get(mid)
                if row is None or row["status"] != MemoryStatus.ACTIVE.value:
                    raise MemoryDomainError(
                        MemoryErrorCode.INVALID_ARGUMENT,
                        "supersede 目标必须存在且 ACTIVE",
                    )
                if mid == plan.winner_memory_id:
                    raise MemoryDomainError(
                        MemoryErrorCode.INVALID_ARGUMENT,
                        "supersede 不得自指 winner",
                    )
            for mid in plan.repoint_rows:
                row = by_id.get(mid)
                if row is None or row["status"] != MemoryStatus.SUPERSEDED.value:
                    raise MemoryDomainError(
                        MemoryErrorCode.INVALID_ARGUMENT,
                        "relation 修复目标必须存在且 SUPERSEDED",
                    )
                if mid == plan.winner_memory_id:
                    raise MemoryDomainError(
                        MemoryErrorCode.INVALID_ARGUMENT,
                        "relation 修复不得自指",
                    )
        if plan.operation is LifecycleOperation.FORGET:
            for mid in plan.forget_rows:
                if not any(r["memory_id"] == mid for r in rows):
                    raise MemoryDomainError(
                        MemoryErrorCode.INVALID_ARGUMENT,
                        "forget 目标 row 必须属于当前 partition",
                    )

    def _validate_post_state(
        self,
        conn: sqlite3.Connection,
        candidate: Optional[SemanticMemoryRecord],
        plan: _LifecyclePlan,
    ) -> None:
        """operation-local invariant：keyed canonical winner 与 FORGET tombstone。"""
        if candidate is not None and candidate.logical_key is not None and plan.operation in {
            LifecycleOperation.INSERT,
            LifecycleOperation.SUPERSEDE,
            LifecycleOperation.NO_CHANGE,
        }:
            after = conn.execute(
                _SQL_SELECT_LTM_PARTITION,
                [
                    candidate.agent_id,
                    candidate.memory_scope,
                    MemoryType.SEMANTIC.value,
                    candidate.logical_key,
                ],
            ).fetchall()
            active = [r for r in after if r["status"] == MemoryStatus.ACTIVE.value]
            if len(active) != 1 or active[0]["memory_id"] != plan.winner_memory_id:
                raise MemoryDomainError(
                    MemoryErrorCode.PERSISTENCE_FAILED,
                    "keyed ACTIVE invariant 违反：canonical winner 不唯一",
                )
            if active[0]["superseded_by_memory_id"] is not None:
                raise MemoryDomainError(
                    MemoryErrorCode.PERSISTENCE_FAILED,
                    "keyed ACTIVE invariant 违反：winner relation 非空",
                )
            for row in after:
                if (
                    row["status"] == MemoryStatus.SUPERSEDED.value
                    and row["superseded_by_memory_id"] != plan.winner_memory_id
                ):
                    raise MemoryDomainError(
                        MemoryErrorCode.PERSISTENCE_FAILED,
                        "keyed relation invariant 违反：未 direct-to-latest",
                    )
        if plan.operation is LifecycleOperation.FORGET and plan.forget_rows:
            # all-version redaction 后该 partition 不应再暴露原正文/非 tombstone。
            first = self._fetch_row(conn, plan.forget_rows[0])
            if first is not None:
                after = self._select_partition(
                    conn,
                    first["agent_id"],
                    first["memory_scope"],
                    first["logical_key"],
                )
                for r in after:
                    if not _is_safe_forget_tombstone(r):
                        raise MemoryDomainError(
                            MemoryErrorCode.PERSISTENCE_FAILED,
                            "forget 后仍存在非安全 tombstone row",
                        )

    def _build_lifecycle_result(
        self,
        operation: LifecycleOperation,
        outcome: str,
        candidate_outcome: Optional[str],
        *,
        winner_memory_id: Optional[str],
        new_memory_id: Optional[str],
        transitions: Tuple[MemoryTransition, ...],
        affected_count: int,
        safe_reason: str,
        safe_error_code: Optional[str],
        resolution_started: float,
        mutation_duration_ms: int,
    ) -> LifecycleResolutionResult:
        ids_truncated = False
        omitted = 0
        ordered = sorted(transitions, key=lambda t: t.memory_id)
        bounded = tuple(ordered)
        if len(transitions) > 8:
            bounded = tuple(ordered[:8])
            ids_truncated = True
            omitted = len(ordered) - 8
        return LifecycleResolutionResult(
            operation=operation,
            outcome=outcome,
            candidate_outcome=candidate_outcome,
            winner_memory_id=winner_memory_id,
            new_memory_id=new_memory_id,
            affected_transitions=bounded,
            affected_count=affected_count,
            ids_truncated=ids_truncated,
            omitted_count=omitted,
            safe_reason=safe_reason,
            safe_error_code=safe_error_code,
            resolution_duration_ms=max(
                0, int((time.monotonic() - resolution_started) * 1000)
            ),
            mutation_duration_ms=mutation_duration_ms,
        )

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
    "ActiveSemanticScopeRead",
    "AdvancedMemoryStore",
    "FORGET_TOMBSTONE_TEXT",
    "LONG_TERM_MEMORY_TABLE",
    "LifecycleOperation",
    "LifecycleResolutionResult",
    "MemoryDomainError",
    "MemoryErrorCode",
    "MemoryLifecycleResolver",
    "MemoryOrigin",
    "MemoryStatus",
    "MemoryTransition",
    "MemoryType",
    "SemanticMemoryRecord",
    "typed_values_equal",
]
