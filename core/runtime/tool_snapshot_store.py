#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PostgreSQL ToolResolutionSnapshot durable store。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.persistence.database import Database
from core.persistence.models import ToolResolutionSnapshotRow
from core.runtime.tool_discovery import ToolResolutionItem, ToolResolutionSnapshot


class ToolSnapshotStoreError(RuntimeError):
    pass


class PostgresToolResolutionSnapshotStore:
    """以 run_id 为主键 create-once；不提供 update/delete runtime API。"""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("database 必须是 Database")
        self._database = database

    async def save(self, snapshot: ToolResolutionSnapshot) -> ToolResolutionSnapshot:
        if not isinstance(snapshot, ToolResolutionSnapshot):
            raise TypeError("snapshot 必须是 ToolResolutionSnapshot")
        try:
            async with self._database.transaction() as session:
                existing = await session.get(ToolResolutionSnapshotRow, snapshot.run_id)
                if existing is not None:
                    loaded = self._from_row(existing)
                    if loaded.snapshot_digest != snapshot.snapshot_digest:
                        raise ToolSnapshotStoreError("Run 已绑定不同 ToolResolutionSnapshot")
                    return loaded
                session.add(ToolResolutionSnapshotRow(
                    run_id=snapshot.run_id,
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_digest=snapshot.snapshot_digest,
                    registry_digest=snapshot.registry_digest,
                    selection_algorithm_version=snapshot.selection_algorithm_version,
                    snapshot_schema_version=snapshot.snapshot_schema_version,
                    selection_query_digest=snapshot.selection_query_digest,
                    tool_items=[item.identity_dict() for item in snapshot.tools],
                    created_at=snapshot.created_at,
                ))
                await session.flush()
        except IntegrityError:
            # 并发 create 只允许同一 durable contract；不同内容 fail closed。
            loaded = await self.load(snapshot.run_id)
            if loaded is None or loaded.snapshot_digest != snapshot.snapshot_digest:
                raise ToolSnapshotStoreError("ToolResolutionSnapshot create conflict") from None
            return loaded
        return snapshot

    async def load(self, run_id: str) -> ToolResolutionSnapshot | None:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id 必须非空")
        async with self._database.session() as session:
            result = await session.execute(
                select(ToolResolutionSnapshotRow).where(
                    ToolResolutionSnapshotRow.run_id == run_id
                )
            )
            row = result.scalar_one_or_none()
        return None if row is None else self._from_row(row)

    @staticmethod
    def _from_row(row: ToolResolutionSnapshotRow) -> ToolResolutionSnapshot:
        tools = tuple(ToolResolutionItem(**dict(item)) for item in row.tool_items)
        return ToolResolutionSnapshot(
            snapshot_id=row.snapshot_id,
            run_id=row.run_id,
            created_at=row.created_at,
            tools=tools,
            registry_digest=row.registry_digest,
            selection_algorithm_version=row.selection_algorithm_version,
            snapshot_schema_version=row.snapshot_schema_version,
            selection_query_digest=row.selection_query_digest,
            snapshot_digest=row.snapshot_digest,
        )


__all__ = ["PostgresToolResolutionSnapshotStore", "ToolSnapshotStoreError"]
