#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PostgreSQL canonical Memory stores。

本模块只拥有 PostgreSQL Memory 的持久化实现。所有 ``async_*`` 方法在应用
event loop 上 await ``AsyncSession``；同步 Agent 执行器通过下方 bridge view
调用它们，bridge 本身不执行数据库驱动。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Iterable

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core.advanced_memory import (
    AdvancedMemoryStore,
    ActiveEpisodicScopeRead,
    ActiveSemanticScopeRead,
    EpisodeGoal,
    EpisodeObservation,
    EpisodeResult,
    EpisodeSituation,
    EpisodicMemoryRecord,
    MemoryDomainError,
    MemoryErrorCode,
    MemoryLifecycleResolver,
    MemoryOrigin,
    MemoryStatus,
    MemoryType,
    SemanticMemoryRecord,
    _is_safe_forget_tombstone,
)
from core.memory_manager import MemoryExchangeError, MemoryExchangeErrorCode
from core.persistence.database import Database
from core.persistence.errors import DatabaseErrorCode, PersistenceError, to_persistence_error
from core.persistence.models import (
    ConversationSummaryRow,
    LongTermMemoryRow,
    MessageExchangeRow,
    MessageRow,
    ProjectSemanticMemoryRow,
)
from core.persistence.sync_bridge import SyncPersistenceBridge
from core.runtime.project_memory import ProjectMemoryMutation, ProjectSemanticRecord


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return str(value)


def _row_dict(row: LongTermMemoryRow) -> dict[str, object]:
    return {
        "memory_id": row.memory_id,
        "memory_type": row.memory_type,
        "status": row.status,
        "agent_id": row.agent_id,
        "memory_scope": row.memory_scope,
        "canonical_text": row.canonical_text,
        # Lifecycle resolver 复用历史 SQLite domain helper，其安全边界要求
        # canonical JSON 文本；PG jsonb 只在模型层保持 dict。
        "payload": json.dumps(row.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "logical_key": row.logical_key,
        "origin_type": row.origin_type,
        "origin_run_id": row.origin_run_id,
        "origin_exchange_id": row.origin_exchange_id,
        "origin_agent_id": row.origin_agent_id,
        "origin_memory_scope": row.origin_memory_scope,
        "formation_method": row.formation_method,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "superseded_by_memory_id": row.superseded_by_memory_id,
    }


def _memory_error(exc: BaseException) -> MemoryDomainError:
    if isinstance(exc, MemoryDomainError):
        return exc
    return MemoryDomainError(MemoryErrorCode.PERSISTENCE_FAILED)


def _project_allow(record: ProjectSemanticRecord):
    from core.runtime.project_memory import (
        ProjectMemoryAuthorizationResult,
        ProjectMemoryPermission,
        ProjectMemoryReason,
    )
    return ProjectMemoryAuthorizationResult(
        "WRITE", record.created_by_agent_id, record.project_id,
        ProjectMemoryPermission.WRITE.value, True, ProjectMemoryReason.ALLOW,
    )


class PostgresMemoryManager:
    """Conversation Memory 的 async PostgreSQL implementation。"""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("database 必须是 Database")
        self.database = database
        from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
        self.lifecycle = MemoryLifecycleRepository(database)

    async def add_message(
        self,
        agent_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        memory_scope: str = "direct",
    ) -> int:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    @staticmethod
    def _validate_exchange(
        agent_id: str, memory_scope: str, user_message: str, assistant_message: str,
        run_id: str | None, exchange_id: str | None,
    ) -> str:
        for value, name in ((agent_id, "agent_id"), (memory_scope, "memory_scope"),
                            (user_message, "user_message"), (assistant_message, "assistant_message")):
            if not isinstance(value, str) or not value.strip():
                raise MemoryExchangeError(MemoryExchangeErrorCode.INVALID_ARGUMENT, f"{name} 必须是非空字符串")
        if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
            raise MemoryExchangeError(MemoryExchangeErrorCode.INVALID_ARGUMENT, "run_id 必须是非空字符串")
        if exchange_id is not None and (not isinstance(exchange_id, str) or not exchange_id.strip()):
            raise MemoryExchangeError(MemoryExchangeErrorCode.INVALID_ARGUMENT, "exchange_id 必须是非空字符串")
        if run_id is None and exchange_id is None:
            raise MemoryExchangeError(MemoryExchangeErrorCode.INVALID_ARGUMENT, "append_exchange_atomic 必须提供 run_id 或 exchange_id")
        return exchange_id or run_id  # type: ignore[return-value]

    async def append_exchange_atomic(
        self,
        agent_id: str,
        memory_scope: str,
        user_message: str,
        assistant_message: str,
        run_id: str | None = None,
        exchange_id: str | None = None,
        statement_timeout_ms: int | None = None,
        idempotent_replay: bool = False,
    ) -> dict[str, Any]:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    @staticmethod
    def _message_dict(row: MessageRow) -> dict[str, Any]:
        return {
            "id": int(row.id), "agent_id": row.agent_id,
            "memory_scope": row.memory_scope, "role": row.role,
            "content": row.content, "timestamp": _iso(row.timestamp),
            "metadata": row.metadata_json or {},
        }

    async def _messages(
        self, *, agent_id: str | None = None, memory_scope: str | None = None,
        limit: int = 500, offset: int = 0, ascending: bool = False,
        after_id: int | None = None, before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        conditions = [or_(MessageRow.exchange_id.is_(None), MessageExchangeRow.state == "COMMITTED")]
        if agent_id is not None:
            conditions.append(MessageRow.agent_id == agent_id)
        if memory_scope is not None:
            conditions.append(MessageRow.memory_scope == memory_scope)
        if after_id is not None:
            conditions.append(MessageRow.id > after_id)
        if before_id is not None:
            conditions.append(MessageRow.id <= before_id)
        ordering = (MessageRow.timestamp.asc(), MessageRow.id.asc()) if ascending else (MessageRow.timestamp.desc(), MessageRow.id.desc())
        query = select(MessageRow).outerjoin(MessageExchangeRow, MessageExchangeRow.exchange_id == MessageRow.exchange_id).where(and_(*conditions)).order_by(*ordering).limit(limit).offset(offset)
        try:
            async with self.database.session() as session:
                result = await session.execute(query)
                return [self._message_dict(row) for row in result.scalars().all()]
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise to_persistence_error(exc, operation="memory.read_messages") from None

    async def count_messages(self, agent_id: str, memory_scope: str | None = "direct") -> int:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def get_chat_history(self, agent_id: str, limit: int = 10, offset: int = 0,
                               ascending: bool = False, memory_scope: str | None = "direct") -> list[dict[str, Any]]:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def get_messages_for_summary(self, agent_id: str, after_id: int, before_id: int,
                                       memory_scope: str | None = "direct") -> list[dict[str, Any]]:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def get_all_messages(self, limit: int = 500, memory_scope: str | None = None) -> list[dict[str, Any]]:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def get_summary_record(self, agent_id: str) -> dict[str, Any]:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def get_all_summaries(self) -> list[dict[str, Any]]:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def save_summary(self, agent_id: str, summary: str, last_message_id: int) -> None:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def search_messages(self, keyword: str, limit: int = 50, memory_scope: str | None = None) -> list[dict[str, Any]]:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def delete_messages(self, message_ids: list[int]) -> dict[str, list[str]]:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def clear_all_memory(self) -> None:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")


class PostgresMemoryManagerBridge:
    """同步 worker 视图；只通过 ``SyncPersistenceBridge`` 回到 async PG。"""

    def __init__(self, store: PostgresMemoryManager, bridge: SyncPersistenceBridge) -> None:
        self._store, self._bridge = store, bridge
        self.database = store.database

    @property
    def async_store(self) -> PostgresMemoryManager:
        return self._store

    def _run(self, operation: str, factory):
        return self._bridge.run(factory, operation=operation)

    def add_message(self, *args, **kwargs): return self._run("memory.add_message", lambda: self._store.add_message(*args, **kwargs))
    def append_exchange_atomic(self, *args, **kwargs): return self._run("memory.append_exchange_atomic", lambda: self._store.append_exchange_atomic(*args, **kwargs))
    def count_messages(self, *args, **kwargs): return self._run("memory.count_messages", lambda: self._store.count_messages(*args, **kwargs))
    def get_chat_history(self, *args, **kwargs): return self._run("memory.get_chat_history", lambda: self._store.get_chat_history(*args, **kwargs))
    def get_messages_for_summary(self, *args, **kwargs): return self._run("memory.get_messages_for_summary", lambda: self._store.get_messages_for_summary(*args, **kwargs))
    def get_all_messages(self, *args, **kwargs): return self._run("memory.get_all_messages", lambda: self._store.get_all_messages(*args, **kwargs))
    def get_summary_record(self, *args, **kwargs): return self._run("memory.get_summary_record", lambda: self._store.get_summary_record(*args, **kwargs))
    def get_all_summaries(self, *args, **kwargs): return self._run("memory.get_all_summaries", lambda: self._store.get_all_summaries(*args, **kwargs))
    def save_summary(self, *args, **kwargs): return self._run("memory.save_summary", lambda: self._store.save_summary(*args, **kwargs))
    def search_messages(self, *args, **kwargs): return self._run("memory.search_messages", lambda: self._store.search_messages(*args, **kwargs))
    def delete_messages(self, *args, **kwargs): return self._run("memory.delete_messages", lambda: self._store.delete_messages(*args, **kwargs))
    def clear_all_memory(self, *args, **kwargs): return self._run("memory.clear_all_memory", lambda: self._store.clear_all_memory(*args, **kwargs))


class PostgresAdvancedMemoryStore:
    """Long-term Memory 的 async PostgreSQL persistence boundary。"""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("database 必须是 Database")
        self.database = database
        from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
        self.lifecycle = MemoryLifecycleRepository(database)

    @staticmethod
    def _record(row: LongTermMemoryRow) -> SemanticMemoryRecord:
        return SemanticMemoryRecord(
            memory_id=row.memory_id, memory_type=MemoryType(row.memory_type), status=MemoryStatus(row.status),
            agent_id=row.agent_id, memory_scope=row.memory_scope, canonical_text=row.canonical_text,
            payload=dict(row.payload), logical_key=row.logical_key,
            origin=MemoryOrigin(row.origin_type, row.origin_run_id, row.origin_exchange_id,
                                row.origin_agent_id, row.origin_memory_scope, row.formation_method),
            created_at=datetime.fromisoformat(row.created_at), updated_at=datetime.fromisoformat(row.updated_at),
            superseded_by_memory_id=row.superseded_by_memory_id,
        )

    @staticmethod
    def _episode(row: LongTermMemoryRow) -> EpisodicMemoryRecord:
        payload = dict(row.payload)
        return EpisodicMemoryRecord(
            memory_id=row.memory_id, agent_id=row.agent_id, memory_scope=row.memory_scope,
            origin_run_id=row.origin_run_id, episode_kind=payload.get("episode_kind", "RUN"),
            origin_step_id=payload.get("origin_step_id"),
            situation=EpisodeSituation(**payload["situation"]),
            goal=EpisodeGoal(**payload["goal"]),
            observations=tuple(EpisodeObservation(**v) for v in payload["observations"]),
            result=EpisodeResult(**payload["result"]),
            lesson=payload.get("lesson"),
            origin=MemoryOrigin(row.origin_type, row.origin_run_id, row.origin_exchange_id, row.origin_agent_id, row.origin_memory_scope, row.formation_method),
            created_at=datetime.fromisoformat(row.created_at), updated_at=datetime.fromisoformat(row.updated_at),
        )

    async def create(self, record: SemanticMemoryRecord) -> SemanticMemoryRecord:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def get_by_memory_id(self, memory_id: str) -> SemanticMemoryRecord:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def list_by_agent(self, agent_id: str, *, memory_scope: str | None = None,
                            active_only: bool = True) -> list[SemanticMemoryRecord]:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def list_active_semantic_for_scope(self, agent_id: str, memory_scope: str, *, candidate_limit: int):
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def create_or_get_episode(self, record: EpisodicMemoryRecord) -> EpisodicMemoryRecord:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def get_episode(self, memory_id: str, agent_id: str, memory_scope: str) -> EpisodicMemoryRecord:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def get_episode_by_origin_run_id(self, origin_run_id: str, agent_id: str, memory_scope: str) -> EpisodicMemoryRecord:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def list_active_episodic_for_scope(self, agent_id: str, memory_scope: str, *, candidate_limit: int):
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def resolve_semantic(self, candidate: SemanticMemoryRecord):
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def forget_semantic_partition(self, *, agent_id: str, memory_scope: str, logical_key: str, mutation_time: datetime | None = None):
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def list_logical_keys(self, agent_id: str, memory_scope: str, *, max_keys: int = 64) -> list[str]:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")


class PostgresAdvancedMemoryStoreBridge(AdvancedMemoryStore):
    """现有同步 Runtime 组件使用的 worker-thread view。"""
    def __init__(self, store: PostgresAdvancedMemoryStore, bridge: SyncPersistenceBridge) -> None:
        self._store, self._bridge = store, bridge
        self.database = store.database
    def _run(self, name: str, factory): return self._bridge.run(factory, operation=f"memory.{name}")
    def create(self, *a, **k): return self._run("create", lambda: self._store.create(*a, **k))
    def create_or_get_episode(self, *a, **k): return self._run("create_or_get_episode", lambda: self._store.create_or_get_episode(*a, **k))
    def get_by_memory_id(self, *a, **k): return self._run("get_by_memory_id", lambda: self._store.get_by_memory_id(*a, **k))
    def list_by_agent(self, *a, **k): return self._run("list_by_agent", lambda: self._store.list_by_agent(*a, **k))
    def list_active_semantic_for_scope(self, *a, **k): return self._run("list_active_semantic_for_scope", lambda: self._store.list_active_semantic_for_scope(*a, **k))
    def get_episode(self, *a, **k): return self._run("get_episode", lambda: self._store.get_episode(*a, **k))
    def get_episode_by_origin_run_id(self, *a, **k): return self._run("get_episode_by_origin_run_id", lambda: self._store.get_episode_by_origin_run_id(*a, **k))
    def list_active_episodic_for_scope(self, *a, **k): return self._run("list_active_episodic_for_scope", lambda: self._store.list_active_episodic_for_scope(*a, **k))
    def resolve_semantic(self, *a, **k): return self._run("resolve_semantic", lambda: self._store.resolve_semantic(*a, **k))
    def forget_semantic_partition(self, *a, **k): return self._run("forget_semantic_partition", lambda: self._store.forget_semantic_partition(*a, **k))
    def list_logical_keys(self, *a, **k): return self._run("list_logical_keys", lambda: self._store.list_logical_keys(*a, **k))


class PostgresProjectSemanticMemoryStore:
    """Project-scoped Semantic Memory 的 async PostgreSQL primitive。"""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("database 必须是 Database")
        self.database = database
        from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
        self.lifecycle = MemoryLifecycleRepository(database)

    @staticmethod
    def _record(row: ProjectSemanticMemoryRow) -> ProjectSemanticRecord:
        values = {
            name: getattr(row, name)
            for name in ProjectSemanticRecord.__dataclass_fields__
        }
        values["payload"] = dict(row.payload)
        values["created_at"] = _iso(row.created_at)
        values["updated_at"] = _iso(row.updated_at)
        return ProjectSemanticRecord(**values)

    async def active(self, project_id: str, logical_key: str | None = None) -> tuple[ProjectSemanticRecord, ...]:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def mutate(self, record: ProjectSemanticRecord, *, supersede: bool) -> ProjectMemoryMutation:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")

    async def forget(self, project_id: str, logical_key: str, updater: str) -> int:
        from core.runtime.memory_scope import MemoryContextError
        raise MemoryContextError("MEMORY_SCOPED_ACCESS_REQUIRED")


class PostgresProjectSemanticMemoryStoreBridge:
    """Project service 在同步 worker 边界使用的 PG bridge。"""
    def __init__(self, store: PostgresProjectSemanticMemoryStore, bridge: SyncPersistenceBridge) -> None:
        self._store, self._bridge = store, bridge
        self.database = store.database
    def _run(self, name: str, factory): return self._bridge.run(factory, operation=f"project_memory.{name}")
    def active(self, *a, **k): return self._run("active", lambda: self._store.active(*a, **k))
    def mutate(self, *a, **k): return self._run("mutate", lambda: self._store.mutate(*a, **k))
    def forget(self, *a, **k): return self._run("forget", lambda: self._store.forget(*a, **k))


def _ltm_row(record: SemanticMemoryRecord | EpisodicMemoryRecord) -> LongTermMemoryRow:
    payload = record.payload if isinstance(record, SemanticMemoryRecord) else record.to_payload()
    return LongTermMemoryRow(
        memory_id=record.memory_id, memory_type=record.memory_type.value, status=record.status.value,
        agent_id=record.agent_id, memory_scope=record.memory_scope, canonical_text=record.canonical_text,
        payload=payload, logical_key=getattr(record, "logical_key", None),
        origin_type=record.origin.origin_type, origin_run_id=record.origin.origin_run_id,
        origin_exchange_id=record.origin.origin_exchange_id, origin_agent_id=record.origin.origin_agent_id,
        origin_memory_scope=record.origin.origin_memory_scope, formation_method=record.origin.formation_method,
        created_at=record.created_at.isoformat(), updated_at=record.updated_at.isoformat(),
        superseded_by_memory_id=getattr(record, "superseded_by_memory_id", None),
    )


def _row_dict_from_record(record: SemanticMemoryRecord) -> dict[str, object]:
    return {**_row_dict(_ltm_row(record))}


def _lifecycle_result(plan):
    from core.advanced_memory import LifecycleResolutionResult
    return LifecycleResolutionResult(
        operation=plan.operation, outcome=plan.outcome, candidate_outcome=plan.candidate_outcome,
        winner_memory_id=plan.winner_memory_id,
        new_memory_id=plan.insert.memory_id if plan.insert is not None else None,
        affected_transitions=plan.transitions,
        affected_count=(1 if plan.insert is not None else 0) + len(plan.supersede_rows) + len(plan.repoint_rows) + len(plan.forget_rows),
        ids_truncated=False, omitted_count=0,
        safe_reason=plan.operation.value, safe_error_code=None,
        resolution_duration_ms=0, mutation_duration_ms=0,
    )


__all__ = [
    "PostgresAdvancedMemoryStore", "PostgresAdvancedMemoryStoreBridge",
    "PostgresMemoryManager", "PostgresMemoryManagerBridge",
    "PostgresProjectSemanticMemoryStore", "PostgresProjectSemanticMemoryStoreBridge",
]
