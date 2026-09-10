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
from core.persistence.errors import PersistenceError, to_persistence_error
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

    async def add_message(
        self,
        agent_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        memory_scope: str = "direct",
    ) -> int:
        try:
            async with self.database.transaction() as session:
                row = MessageRow(
                    agent_id=agent_id,
                    role=role,
                    content=content,
                    metadata_json=metadata,
                    memory_scope=memory_scope,
                )
                session.add(row)
                await session.flush()
                return int(row.id)
        except PersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise to_persistence_error(exc, operation="memory.add_message") from None

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
    ) -> dict[str, Any]:
        final_exchange_id = self._validate_exchange(
            agent_id, memory_scope, user_message, assistant_message, run_id, exchange_id
        )
        try:
            async with self.database.transaction(statement_timeout_ms=statement_timeout_ms) as session:
                exchange = MessageExchangeRow(
                    exchange_id=final_exchange_id,
                    run_id=run_id,
                    agent_id=agent_id,
                    memory_scope=memory_scope,
                    state="PENDING",
                )
                session.add(exchange)
                await session.flush()
                user = MessageRow(
                    agent_id=agent_id, role="user", content=user_message,
                    memory_scope=memory_scope, exchange_id=final_exchange_id,
                    run_id=run_id, sequence=0,
                )
                assistant = MessageRow(
                    agent_id=agent_id, role="assistant", content=assistant_message,
                    memory_scope=memory_scope, exchange_id=final_exchange_id,
                    run_id=run_id, sequence=1,
                )
                session.add_all((user, assistant))
                await session.flush()
                exchange.state = "COMMITTED"
                exchange.user_message_id = user.id
                exchange.assistant_message_id = assistant.id
                await session.flush()
                return {
                    "exchange_id": final_exchange_id,
                    "user_message_id": int(user.id),
                    "assistant_message_id": int(assistant.id),
                }
        except IntegrityError:
            raise MemoryExchangeError(
                MemoryExchangeErrorCode.DUPLICATE_EXCHANGE,
                "该 Run 的 exchange 已提交，拒绝重复写入",
            ) from None
        except MemoryExchangeError:
            raise
        except SQLAlchemyError as exc:
            raise MemoryExchangeError(
                MemoryExchangeErrorCode.EXCHANGE_FAILED,
                "Memory exchange 写入失败",
            ) from exc

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
        conditions = [
            MessageRow.agent_id == agent_id,
            or_(
                MessageRow.exchange_id.is_(None),
                MessageExchangeRow.state == "COMMITTED",
            ),
        ]
        if memory_scope is not None:
            conditions.append(MessageRow.memory_scope == memory_scope)
        async with self.database.session() as session:
            result = await session.execute(
                select(func.count())
                .select_from(MessageRow)
                .outerjoin(
                    MessageExchangeRow,
                    MessageExchangeRow.exchange_id == MessageRow.exchange_id,
                )
                .where(and_(*conditions))
            )
            return int(result.scalar_one())

    async def get_chat_history(self, agent_id: str, limit: int = 10, offset: int = 0,
                               ascending: bool = False, memory_scope: str | None = "direct") -> list[dict[str, Any]]:
        return await self._messages(agent_id=agent_id, memory_scope=memory_scope, limit=limit, offset=offset, ascending=ascending)

    async def get_messages_for_summary(self, agent_id: str, after_id: int, before_id: int,
                                       memory_scope: str | None = "direct") -> list[dict[str, Any]]:
        if before_id <= after_id:
            return []
        return await self._messages(agent_id=agent_id, memory_scope=memory_scope, limit=500000,
                                    offset=0, ascending=True, after_id=after_id, before_id=before_id)

    async def get_all_messages(self, limit: int = 500, memory_scope: str | None = None) -> list[dict[str, Any]]:
        return await self._messages(memory_scope=memory_scope, limit=limit, offset=0, ascending=False)

    async def get_summary_record(self, agent_id: str) -> dict[str, Any]:
        async with self.database.session() as session:
            row = (await session.execute(select(ConversationSummaryRow).where(ConversationSummaryRow.agent_id == agent_id))).scalar_one_or_none()
            if row is None:
                return {"agent_id": agent_id, "summary": "", "last_message_id": 0, "updated_at": ""}
            return {"agent_id": row.agent_id, "summary": row.summary, "last_message_id": int(row.last_message_id), "updated_at": _iso(row.updated_at)}

    async def get_all_summaries(self) -> list[dict[str, Any]]:
        async with self.database.session() as session:
            rows = (await session.execute(select(ConversationSummaryRow).order_by(ConversationSummaryRow.updated_at.desc(), ConversationSummaryRow.agent_id.asc()))).scalars().all()
            return [{"agent_id": r.agent_id, "summary": r.summary, "last_message_id": int(r.last_message_id), "updated_at": _iso(r.updated_at)} for r in rows]

    async def save_summary(self, agent_id: str, summary: str, last_message_id: int) -> None:
        async with self.database.transaction() as session:
            row = (await session.execute(select(ConversationSummaryRow).where(ConversationSummaryRow.agent_id == agent_id).with_for_update())).scalar_one_or_none()
            if row is None:
                session.add(ConversationSummaryRow(agent_id=agent_id, summary=summary, last_message_id=last_message_id))
            else:
                row.summary = summary
                row.last_message_id = last_message_id
            await session.flush()

    async def search_messages(self, keyword: str, limit: int = 50, memory_scope: str | None = None) -> list[dict[str, Any]]:
        query = " ".join(keyword.split()).strip()
        if not query:
            return []
        params: dict[str, object] = {"query": query, "limit": limit}
        if memory_scope is None:
            statement = text(
                "SELECT m.id, m.agent_id, m.memory_scope, m.role, m.content, m.timestamp, m.metadata "
                "FROM messages m LEFT JOIN message_exchanges e ON e.exchange_id = m.exchange_id "
                "WHERE m.search_vector @@ websearch_to_tsquery('simple', :query) "
                "AND (m.exchange_id IS NULL OR e.state = 'COMMITTED') "
                "ORDER BY ts_rank(m.search_vector, websearch_to_tsquery('simple', :query)) DESC, "
                "m.timestamp DESC, m.id DESC LIMIT :limit"
            )
        else:
            params["memory_scope"] = memory_scope
            statement = text(
                "SELECT m.id, m.agent_id, m.memory_scope, m.role, m.content, m.timestamp, m.metadata "
                "FROM messages m LEFT JOIN message_exchanges e ON e.exchange_id = m.exchange_id "
                "WHERE m.search_vector @@ websearch_to_tsquery('simple', :query) "
                "AND (m.exchange_id IS NULL OR e.state = 'COMMITTED') "
                "AND m.memory_scope = :memory_scope "
                "ORDER BY ts_rank(m.search_vector, websearch_to_tsquery('simple', :query)) DESC, "
                "m.timestamp DESC, m.id DESC LIMIT :limit"
            )
        async with self.database.session() as session:
            result = await session.execute(statement, params)
            return [{"id": int(r.id), "agent_id": r.agent_id, "memory_scope": r.memory_scope,
                     "role": r.role, "content": r.content, "timestamp": _iso(r.timestamp),
                     "metadata": r.metadata or {}} for r in result]

    async def delete_messages(self, message_ids: list[int]) -> dict[str, list[str]]:
        if not message_ids:
            return {"affected_agent_ids": [], "refresh_agent_ids": []}
        async with self.database.transaction() as session:
            rows = (await session.execute(select(MessageRow).where(MessageRow.id.in_(message_ids)))).scalars().all()
            affected = sorted({r.agent_id for r in rows})
            refresh = sorted({r.agent_id for r in rows if r.memory_scope == "direct"})
            await session.execute(delete(MessageRow).where(MessageRow.id.in_(message_ids)))
            if refresh:
                await session.execute(delete(ConversationSummaryRow).where(ConversationSummaryRow.agent_id.in_(refresh)))
            return {"affected_agent_ids": affected, "refresh_agent_ids": refresh}

    async def clear_all_memory(self) -> None:
        async with self.database.transaction() as session:
            await session.execute(delete(MessageRow))
            await session.execute(delete(ConversationSummaryRow))


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
        if not isinstance(record, SemanticMemoryRecord):
            raise TypeError("create 需要 SemanticMemoryRecord")
        if record.memory_type is not MemoryType.SEMANTIC or record.status is not MemoryStatus.ACTIVE:
            raise MemoryDomainError(MemoryErrorCode.PUBLIC_CREATE_ACTIVE_ONLY, "公共 create 只允许创建 ACTIVE SEMANTIC record")
        try:
            async with self.database.transaction() as session:
                existing = (await session.execute(select(LongTermMemoryRow).where(LongTermMemoryRow.memory_id == record.memory_id))).scalar_one_or_none()
                if existing is not None:
                    if _row_dict(existing) == _row_dict_from_record(record):
                        return self._record(existing)
                    raise MemoryDomainError(MemoryErrorCode.DUPLICATE_CONFLICT)
                session.add(_ltm_row(record))
                await session.flush()
                return record
        except MemoryDomainError:
            raise
        except IntegrityError:
            raise MemoryDomainError(MemoryErrorCode.DUPLICATE_CONFLICT) from None
        except SQLAlchemyError:
            raise MemoryDomainError(MemoryErrorCode.PERSISTENCE_FAILED) from None

    async def get_by_memory_id(self, memory_id: str) -> SemanticMemoryRecord:
        async with self.database.session() as session:
            row = (await session.execute(select(LongTermMemoryRow).where(LongTermMemoryRow.memory_id == memory_id))).scalar_one_or_none()
            if row is None:
                raise MemoryDomainError(MemoryErrorCode.NOT_FOUND)
            return self._record(row)

    async def list_by_agent(self, agent_id: str, *, memory_scope: str | None = None,
                            active_only: bool = True) -> list[SemanticMemoryRecord]:
        async with self.database.session() as session:
            query = select(LongTermMemoryRow).where(LongTermMemoryRow.agent_id == agent_id, LongTermMemoryRow.memory_type == MemoryType.SEMANTIC.value)
            if memory_scope is not None: query = query.where(LongTermMemoryRow.memory_scope == memory_scope)
            if active_only: query = query.where(LongTermMemoryRow.status == MemoryStatus.ACTIVE.value)
            rows = (await session.execute(query.order_by(LongTermMemoryRow.created_at.asc(), LongTermMemoryRow.memory_id.asc()))).scalars().all()
            return [self._record(r) for r in rows]

    async def list_active_semantic_for_scope(self, agent_id: str, memory_scope: str, *, candidate_limit: int):
        async with self.database.session() as session:
            query = select(LongTermMemoryRow).where(LongTermMemoryRow.agent_id == agent_id, LongTermMemoryRow.memory_scope == memory_scope, LongTermMemoryRow.memory_type == MemoryType.SEMANTIC.value, LongTermMemoryRow.status == MemoryStatus.ACTIVE.value).order_by(LongTermMemoryRow.created_at.desc(), LongTermMemoryRow.memory_id.asc()).limit(candidate_limit)
            rows = (await session.execute(query)).scalars().all()
            records, malformed = [], 0
            for row in rows:
                try: records.append(self._record(row))
                except (MemoryDomainError, TypeError, ValueError): malformed += 1
            return ActiveSemanticScopeRead(tuple(records), malformed)

    async def create_or_get_episode(self, record: EpisodicMemoryRecord) -> EpisodicMemoryRecord:
        try:
            async with self.database.transaction() as session:
                query = select(LongTermMemoryRow).where(LongTermMemoryRow.memory_type == MemoryType.EPISODIC.value, LongTermMemoryRow.origin_run_id == record.origin_run_id)
                if record.episode_kind.value == "STEP":
                    query = query.where(LongTermMemoryRow.agent_id == record.agent_id, LongTermMemoryRow.payload["episode_kind"].as_string() == "STEP", LongTermMemoryRow.payload["origin_step_id"].as_string() == record.origin_step_id)
                else:
                    query = query.where(text("COALESCE(payload ->> 'episode_kind', 'RUN') = 'RUN'"))
                existing = (await session.execute(query)).scalar_one_or_none()
                if existing is not None:
                    if existing.agent_id != record.agent_id or existing.memory_scope != record.memory_scope:
                        raise MemoryDomainError(MemoryErrorCode.EPISODE_ORIGIN_CONFLICT)
                    return self._episode(existing)
                if (await session.execute(select(LongTermMemoryRow).where(LongTermMemoryRow.memory_id == record.memory_id))).scalar_one_or_none() is not None:
                    raise MemoryDomainError(MemoryErrorCode.DUPLICATE_CONFLICT)
                session.add(_ltm_row(record))
                await session.flush()
                return record
        except MemoryDomainError: raise
        except IntegrityError: raise MemoryDomainError(MemoryErrorCode.EPISODE_ORIGIN_CONFLICT) from None
        except SQLAlchemyError: raise MemoryDomainError(MemoryErrorCode.PERSISTENCE_FAILED) from None

    async def get_episode(self, memory_id: str, agent_id: str, memory_scope: str) -> EpisodicMemoryRecord:
        async with self.database.session() as session:
            row = (await session.execute(select(LongTermMemoryRow).where(LongTermMemoryRow.memory_id == memory_id, LongTermMemoryRow.agent_id == agent_id, LongTermMemoryRow.memory_scope == memory_scope, LongTermMemoryRow.memory_type == MemoryType.EPISODIC.value))).scalar_one_or_none()
            if row is None: raise MemoryDomainError(MemoryErrorCode.NOT_FOUND)
            return self._episode(row)

    async def get_episode_by_origin_run_id(self, origin_run_id: str, agent_id: str, memory_scope: str) -> EpisodicMemoryRecord:
        async with self.database.session() as session:
            row = (await session.execute(select(LongTermMemoryRow).where(LongTermMemoryRow.origin_run_id == origin_run_id, LongTermMemoryRow.agent_id == agent_id, LongTermMemoryRow.memory_scope == memory_scope, LongTermMemoryRow.memory_type == MemoryType.EPISODIC.value))).scalar_one_or_none()
            if row is None: raise MemoryDomainError(MemoryErrorCode.NOT_FOUND)
            return self._episode(row)

    async def list_active_episodic_for_scope(self, agent_id: str, memory_scope: str, *, candidate_limit: int):
        async with self.database.session() as session:
            rows = (await session.execute(select(LongTermMemoryRow).where(LongTermMemoryRow.agent_id == agent_id, LongTermMemoryRow.memory_scope == memory_scope, LongTermMemoryRow.memory_type == MemoryType.EPISODIC.value, LongTermMemoryRow.status == MemoryStatus.ACTIVE.value).order_by(LongTermMemoryRow.created_at.desc(), LongTermMemoryRow.memory_id.asc()).limit(candidate_limit))).scalars().all()
            records, malformed = [], 0
            for row in rows:
                try: records.append(self._episode(row))
                except (MemoryDomainError, TypeError, ValueError): malformed += 1
            return ActiveEpisodicScopeRead(tuple(records), malformed)

    async def resolve_semantic(self, candidate: SemanticMemoryRecord):
        """在一个 PG transaction 内复用已冻结的 lifecycle resolver。"""
        try:
            async with self.database.transaction() as session:
                existing = (await session.execute(select(LongTermMemoryRow).where(LongTermMemoryRow.memory_id == candidate.memory_id))).scalar_one_or_none()
                if existing is not None:
                    if _row_dict(existing) == _row_dict_from_record(candidate):
                        from core.advanced_memory import LifecycleOperation, LifecycleResolutionResult
                        return LifecycleResolutionResult(LifecycleOperation.NO_CHANGE, "OK", "REUSED", candidate.memory_id, candidate.memory_id, (), 0, False, 0, "IDEMPOTENT_REUSE", None, 0, 0)
                    raise MemoryDomainError(MemoryErrorCode.DUPLICATE_CONFLICT)
                result = await session.execute(select(LongTermMemoryRow).where(LongTermMemoryRow.agent_id == candidate.agent_id, LongTermMemoryRow.memory_scope == candidate.memory_scope, LongTermMemoryRow.memory_type == MemoryType.SEMANTIC.value).order_by(LongTermMemoryRow.created_at.asc(), LongTermMemoryRow.memory_id.asc()))
                rows = [_row_dict(r) for r in result.scalars().all()]
                plan = MemoryLifecycleResolver.resolve_remember(candidate, rows, mutation_time=datetime.now(UTC))
                if plan.insert is not None:
                    session.add(_ltm_row(plan.insert))
                for mid in plan.supersede_rows:
                    await session.execute(update(LongTermMemoryRow).where(LongTermMemoryRow.memory_id == mid).values(status=MemoryStatus.SUPERSEDED.value, superseded_by_memory_id=plan.winner_memory_id, updated_at=plan.mutation_timestamp.isoformat() if plan.mutation_timestamp else candidate.updated_at.isoformat()))
                for mid in plan.repoint_rows:
                    await session.execute(update(LongTermMemoryRow).where(LongTermMemoryRow.memory_id == mid).values(superseded_by_memory_id=plan.winner_memory_id, updated_at=plan.mutation_timestamp.isoformat() if plan.mutation_timestamp else candidate.updated_at.isoformat()))
                await session.flush()
                return _lifecycle_result(plan)
        except MemoryDomainError: raise
        except IntegrityError: raise MemoryDomainError(MemoryErrorCode.DUPLICATE_CONFLICT) from None
        except SQLAlchemyError: raise MemoryDomainError(MemoryErrorCode.PERSISTENCE_FAILED) from None

    async def forget_semantic_partition(self, *, agent_id: str, memory_scope: str, logical_key: str, mutation_time: datetime | None = None):
        from core.advanced_memory import LifecycleOperation, LifecycleResolutionResult
        mutation_time = mutation_time or datetime.now(UTC)
        async with self.database.transaction() as session:
            result = await session.execute(select(LongTermMemoryRow).where(LongTermMemoryRow.agent_id == agent_id, LongTermMemoryRow.memory_scope == memory_scope, LongTermMemoryRow.memory_type == MemoryType.SEMANTIC.value, LongTermMemoryRow.logical_key == logical_key).order_by(LongTermMemoryRow.created_at.asc(), LongTermMemoryRow.memory_id.asc()))
            rows = [_row_dict(r) for r in result.scalars().all()]
            plan = MemoryLifecycleResolver.resolve_forget(rows, mutation_time=mutation_time)
            if plan.forget_rows:
                await session.execute(update(LongTermMemoryRow).where(LongTermMemoryRow.memory_id.in_(plan.forget_rows)).values(status=MemoryStatus.FORGOTTEN.value, canonical_text="[FORGOTTEN]", payload={}, superseded_by_memory_id=None, updated_at=mutation_time.isoformat()))
            await session.flush()
            return _lifecycle_result(plan)

    async def list_logical_keys(self, agent_id: str, memory_scope: str, *, max_keys: int = 64) -> list[str]:
        async with self.database.session() as session:
            rows = (await session.execute(select(LongTermMemoryRow.logical_key).where(LongTermMemoryRow.agent_id == agent_id, LongTermMemoryRow.memory_scope == memory_scope, LongTermMemoryRow.memory_type == MemoryType.SEMANTIC.value, LongTermMemoryRow.logical_key.is_not(None)).distinct().order_by(LongTermMemoryRow.logical_key.asc()))).scalars().all()
            return [] if len(rows) > max_keys else [str(v) for v in rows]


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
        async with self.database.session() as session:
            query = select(ProjectSemanticMemoryRow).where(
                ProjectSemanticMemoryRow.project_id == project_id,
                ProjectSemanticMemoryRow.visibility == "PROJECT",
                ProjectSemanticMemoryRow.status == "ACTIVE",
            )
            if logical_key is not None:
                query = query.where(ProjectSemanticMemoryRow.logical_key == logical_key)
            rows = (await session.execute(query.order_by(ProjectSemanticMemoryRow.created_at.desc(), ProjectSemanticMemoryRow.memory_id.asc()))).scalars().all()
            return tuple(self._record(row) for row in rows)

    async def mutate(self, record: ProjectSemanticRecord, *, supersede: bool) -> ProjectMemoryMutation:
        now = datetime.now(UTC).isoformat()
        try:
            async with self.database.transaction() as session:
                query = select(ProjectSemanticMemoryRow).where(
                    ProjectSemanticMemoryRow.project_id == record.project_id,
                    ProjectSemanticMemoryRow.logical_key == record.logical_key,
                    ProjectSemanticMemoryRow.visibility == "PROJECT",
                    ProjectSemanticMemoryRow.status == "ACTIVE",
                ).order_by(ProjectSemanticMemoryRow.created_at.asc(), ProjectSemanticMemoryRow.memory_id.asc()).with_for_update()
                rows = (await session.execute(query)).scalars().all()
                current = rows[-1] if rows else None
                if current is not None:
                    current_record = self._record(current)
                    if current_record.payload == record.payload and current_record.canonical_text == record.canonical_text:
                        return ProjectMemoryMutation("NO_CHANGE", current_record, _project_allow(record))
                    if not supersede:
                        from core.runtime.project_memory import ProjectMemoryReason, ProjectMemoryAuthorizationResult, ProjectMemoryPermission
                        return ProjectMemoryMutation("CONFLICT", current_record, ProjectMemoryAuthorizationResult("WRITE", record.created_by_agent_id, record.project_id, ProjectMemoryPermission.WRITE.value, False, ProjectMemoryReason.PROJECT_SEMANTIC_CONFLICT))
                    await session.execute(update(ProjectSemanticMemoryRow).where(
                        ProjectSemanticMemoryRow.project_id == record.project_id,
                        ProjectSemanticMemoryRow.logical_key == record.logical_key,
                        ProjectSemanticMemoryRow.status == "ACTIVE",
                    ).values(status="SUPERSEDED", superseded_by_memory_id=record.memory_id, updated_at=now, updated_by_agent_id=record.updated_by_agent_id))
                session.add(ProjectSemanticMemoryRow(
                    memory_id=record.memory_id, project_id=record.project_id,
                    owner_kind="PROJECT", owner_id=record.project_id,
                    visibility="PROJECT", scope_id=record.project_id,
                    logical_key=record.logical_key, canonical_text=record.canonical_text,
                    payload=record.payload, status="ACTIVE",
                    origin_agent_id=record.origin_agent_id, origin_run_id=record.origin_run_id,
                    created_by_agent_id=record.created_by_agent_id, updated_by_agent_id=record.updated_by_agent_id,
                    source_memory_id=record.source_memory_id, source_owner_agent_id=record.source_owner_agent_id,
                    promoted_by_agent_id=record.promoted_by_agent_id, promotion_run_id=record.promotion_run_id,
                    promotion_time=record.promotion_time, superseded_by_memory_id=None,
                    created_at=now, updated_at=now,
                ))
                await session.flush()
                from core.runtime.project_memory import ProjectMemoryReason, ProjectMemoryAuthorizationResult, ProjectMemoryPermission
                return ProjectMemoryMutation("SUPERSEDED" if current else "CREATED", record, ProjectMemoryAuthorizationResult("WRITE", record.created_by_agent_id, record.project_id, ProjectMemoryPermission.WRITE.value, True, ProjectMemoryReason.ALLOW))
        except MemoryDomainError:
            raise
        except IntegrityError:
            raise MemoryDomainError(MemoryErrorCode.DUPLICATE_CONFLICT) from None
        except SQLAlchemyError:
            raise MemoryDomainError(MemoryErrorCode.PERSISTENCE_FAILED) from None

    async def forget(self, project_id: str, logical_key: str, updater: str) -> int:
        async with self.database.transaction() as session:
            result = await session.execute(update(ProjectSemanticMemoryRow).where(
                ProjectSemanticMemoryRow.project_id == project_id,
                ProjectSemanticMemoryRow.logical_key == logical_key,
                ProjectSemanticMemoryRow.visibility == "PROJECT",
                ProjectSemanticMemoryRow.status == "ACTIVE",
            ).values(status="FORGOTTEN", updated_at=datetime.now(UTC).isoformat(), updated_by_agent_id=updater))
            return int(result.rowcount or 0)


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
