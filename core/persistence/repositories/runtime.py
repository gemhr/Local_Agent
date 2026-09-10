#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Runtime 持久化的 narrow repositories。

契约（Stage6-WP0 冻结）：

* 每个函数只接收调用方提供的 ``AsyncSession``；
* **禁止** ``commit()`` / ``rollback()`` / ``session.begin()`` / 新建 Session；
* 只做 INSERT / SELECT / UPDATE / DELETE 与必要的 ``flush()``；
* 事务边界与重试预算由 Transaction Owner（Application Service / Store）持有。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.persistence.models import (
    EventConsumptionCheckpointRow,
    RuntimeEventJournalRow,
    RuntimeSnapshotRow,
)

__all__ = [
    "delete_journal_rows_for_run",
    "insert_checkpoint",
    "insert_journal_row",
    "insert_snapshot_row",
    "lock_run_scope",
    "select_checkpoint",
    "select_journal_by_event_id",
    "select_journal_by_sequence",
    "select_journal_page",
    "select_last_checkpoint_sequence",
    "select_last_journal_sequence",
    "select_latest_snapshot",
    "select_snapshot_by_id",
    "select_snapshots_for_run",
    "select_terminal_sequence",
    "select_terminal_sequences",
]


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


async def lock_run_scope(session: AsyncSession, run_id: str) -> None:
    """取得 run 级 transaction-scoped advisory lock。

    ``pg_advisory_xact_lock`` 在事务结束时自动释放，锁序恒为单键，因此不存在
    交叉加锁导致的死锁。它把同一 Run 的 read-decide-insert 串行化，使并发
    append 得到确定性的 typed 结果，而不是随机撞唯一约束。

    这是**明确声明的锁范围**：只锁定调用方传入的 run_id，不锁其它 Run，也不
    锁全表。
    """
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(run_id, 0)))
    )


async def select_journal_by_event_id(
    session: AsyncSession, event_id: str
) -> RuntimeEventJournalRow | None:
    result = await session.execute(
        select(RuntimeEventJournalRow).where(
            RuntimeEventJournalRow.event_id == event_id
        )
    )
    return result.scalar_one_or_none()


async def select_journal_by_sequence(
    session: AsyncSession, run_id: str, sequence: int
) -> RuntimeEventJournalRow | None:
    result = await session.execute(
        select(RuntimeEventJournalRow).where(
            RuntimeEventJournalRow.run_id == run_id,
            RuntimeEventJournalRow.sequence == sequence,
        )
    )
    return result.scalar_one_or_none()


async def select_last_journal_sequence(
    session: AsyncSession, run_id: str
) -> int | None:
    result = await session.execute(
        select(func.max(RuntimeEventJournalRow.sequence)).where(
            RuntimeEventJournalRow.run_id == run_id
        )
    )
    value = result.scalar_one_or_none()
    return None if value is None else int(value)


async def select_terminal_sequence(
    session: AsyncSession, run_id: str, terminal_event_type: str
) -> int | None:
    result = await session.execute(
        select(RuntimeEventJournalRow.sequence)
        .where(
            RuntimeEventJournalRow.run_id == run_id,
            RuntimeEventJournalRow.event_type == terminal_event_type,
        )
        .order_by(RuntimeEventJournalRow.sequence.asc())
        .limit(1)
    )
    value = result.scalar_one_or_none()
    return None if value is None else int(value)


async def select_terminal_sequences(
    session: AsyncSession, run_id: str, terminal_event_type: str
) -> tuple[int, ...]:
    """读取全部终态 sequence；用于只读校验终态不变量。"""
    result = await session.execute(
        select(RuntimeEventJournalRow.sequence)
        .where(
            RuntimeEventJournalRow.run_id == run_id,
            RuntimeEventJournalRow.event_type == terminal_event_type,
        )
        .order_by(RuntimeEventJournalRow.sequence.asc())
    )
    return tuple(int(value) for value in result.scalars().all())


async def select_journal_page(
    session: AsyncSession, run_id: str, after_sequence: int, limit: int
) -> tuple[RuntimeEventJournalRow, ...]:
    result = await session.execute(
        select(RuntimeEventJournalRow)
        .where(
            RuntimeEventJournalRow.run_id == run_id,
            RuntimeEventJournalRow.sequence > after_sequence,
        )
        .order_by(RuntimeEventJournalRow.sequence.asc())
        .limit(limit)
    )
    return tuple(result.scalars().all())


async def insert_journal_row(
    session: AsyncSession, values: dict[str, object]
) -> None:
    session.add(RuntimeEventJournalRow(**values))
    await session.flush()


async def delete_journal_rows_for_run(
    session: AsyncSession, run_id: str
) -> int:
    result = await session.execute(
        delete(RuntimeEventJournalRow).where(
            RuntimeEventJournalRow.run_id == run_id
        )
    )
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


async def select_snapshot_by_id(
    session: AsyncSession, snapshot_id: str
) -> RuntimeSnapshotRow | None:
    result = await session.execute(
        select(RuntimeSnapshotRow).where(
            RuntimeSnapshotRow.snapshot_id == snapshot_id
        )
    )
    return result.scalar_one_or_none()


async def select_latest_snapshot(
    session: AsyncSession, run_id: str
) -> RuntimeSnapshotRow | None:
    result = await session.execute(
        select(RuntimeSnapshotRow)
        .where(RuntimeSnapshotRow.run_id == run_id)
        .order_by(
            RuntimeSnapshotRow.created_at.desc(),
            RuntimeSnapshotRow.snapshot_id.asc(),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def select_snapshots_for_run(
    session: AsyncSession, run_id: str, limit: int
) -> tuple[RuntimeSnapshotRow, ...]:
    result = await session.execute(
        select(RuntimeSnapshotRow)
        .where(RuntimeSnapshotRow.run_id == run_id)
        .order_by(
            RuntimeSnapshotRow.created_at.desc(),
            RuntimeSnapshotRow.snapshot_id.asc(),
        )
        .limit(limit)
    )
    return tuple(result.scalars().all())


async def insert_snapshot_row(
    session: AsyncSession, values: dict[str, object]
) -> None:
    session.add(RuntimeSnapshotRow(**values))
    await session.flush()


# ---------------------------------------------------------------------------
# Event consumption checkpoint
# ---------------------------------------------------------------------------


async def select_checkpoint(
    session: AsyncSession, consumer_id: str, event_id: str
) -> EventConsumptionCheckpointRow | None:
    result = await session.execute(
        select(EventConsumptionCheckpointRow).where(
            EventConsumptionCheckpointRow.consumer_id == consumer_id,
            EventConsumptionCheckpointRow.event_id == event_id,
        )
    )
    return result.scalar_one_or_none()


async def select_last_checkpoint_sequence(
    session: AsyncSession, consumer_id: str, run_id: str
) -> int | None:
    result = await session.execute(
        select(func.max(EventConsumptionCheckpointRow.sequence)).where(
            EventConsumptionCheckpointRow.consumer_id == consumer_id,
            EventConsumptionCheckpointRow.run_id == run_id,
        )
    )
    value = result.scalar_one_or_none()
    return None if value is None else int(value)


async def insert_checkpoint(
    session: AsyncSession,
    *,
    consumer_id: str,
    event_id: str,
    run_id: str,
    sequence: int,
    processed_at: datetime,
) -> None:
    session.add(
        EventConsumptionCheckpointRow(
            consumer_id=consumer_id,
            event_id=event_id,
            run_id=run_id,
            sequence=sequence,
            processed_at=processed_at,
        )
    )
    await session.flush()
