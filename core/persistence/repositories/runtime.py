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
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.persistence.models import (
    EventConsumptionCheckpointRow,
    RuntimeEventJournalRow,
    RuntimeSnapshotRow,
    WorkflowRunRow,
    WorkflowStepRunRow,
)
from core.runtime.execution_aggregate import canonical_payload_digest

if TYPE_CHECKING:
    from core.agent_platform.runtime_adapter import RuntimeRequest

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


async def workflow_claim_allowed(
    session: AsyncSession, run_id: str, runtime_kind: str, request: RuntimeRequest | None, *,
    runtime_target_digest: str | None = None, recovering: bool = False,
    already_admitted: bool = False, workflow_artifact_check=None,
) -> bool:
    """在调用方持有的 Run 锁下核对实际执行请求与持久预留。"""
    step = await session.scalar(select(WorkflowStepRunRow).where(WorkflowStepRunRow.platform_run_id == run_id))
    if step is None:
        return request is None or request.workflow_dispatch_token is None
    if step.runtime_kind != runtime_kind or step.status != "RESERVED":
        return False
    if recovering:
        return True
    if (request is None or request.workflow_dispatch_token is None
            or request.workflow_dispatch_token != step.dispatch_token):
        return False
    access = request.memory_access_context
    run = await session.get(WorkflowRunRow, step.workflow_run_id)
    matches = (
        request.run_id == run_id
        and request.agent_id == step.agent_id
        and request.agent_version == step.agent_version
        and canonical_payload_digest(request.input) == step.input_digest
        and request.runtime_instance_id == step.runtime_instance_id
        and runtime_target_digest == step.runtime_target_digest
        and (runtime_kind != "OPENCODE" or runtime_target_digest is not None)
        and access is not None and access.principal_kind == "HUMAN"
        and access.tenant_id == run.tenant_id and access.user_id == str(run.owner_user_id)
        and access.agent_id == request.agent_id
        and access.session_id == request.session_id == f"{run.workflow_run_id}:{step.step_id}"
    )
    if not matches:
        return False
    if runtime_kind == "NATIVE" and not already_admitted and (run.handoff_input is not None or step.bound_input is not None):
        # 只读 Workflow 状态，避免逆序获取已由 prepare/cancel 持有的 Workflow 行锁。
        if (run.handoff_input is None or run.cancel_requested or run.status != "RUNNING" or run.current_step != step.position
                or step.position != 1 or step.bound_input != request.input or workflow_artifact_check is None):
            return False
        await workflow_artifact_check(session, run, step, request.input)
    return True


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
