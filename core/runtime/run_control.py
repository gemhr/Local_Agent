"""PostgreSQL durable Run ownership, fencing and CANCEL control plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import func, or_, select, text, update

from core.persistence.database import Database
from core.persistence.errors import PersistenceError
from core.persistence.models import RunControlCommandRow, RunControlRow
from core.persistence.repositories import runtime as runtime_repository
from core.runtime.events import RuntimeEvent


class RunControlError(Exception):
    """Typed durable control-plane failure."""


class OwnershipLost(RunControlError):
    """The database can no longer prove that this executor owns the Run."""


class RunControlConflict(RunControlError):
    """The requested control mutation conflicts with durable state."""


@dataclass(frozen=True, slots=True)
class RunLease:
    run_id: str
    owner_id: str
    lease_until: datetime
    fencing_token: int
    version: int


@dataclass(frozen=True, slots=True)
class CancelIntent:
    run_id: str
    command_id: str
    reason: str
    created_at: datetime


class DurableRunControlService:
    """唯一负责 durable Run lease/fence/command 状态转换的 Service。"""

    def __init__(self, database: Database, *, lease_seconds: int = 30) -> None:
        if not isinstance(database, Database):
            raise TypeError("database 必须是 Database")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError("lease_seconds 必须是正整数")
        self.database = database
        self.lease_seconds = lease_seconds

    async def claim(self, run_id: str, owner_id: str) -> RunLease:
        self._validate_identity(run_id, "run_id")
        self._validate_identity(owner_id, "owner_id")
        try:
            async with self.database.transaction() as session:
                await runtime_repository.lock_run_scope(session, run_id)
                row = (await session.execute(
                    select(RunControlRow).where(RunControlRow.run_id == run_id).with_for_update()
                )).scalar_one_or_none()
                if row is None:
                    row = RunControlRow(run_id=run_id, owner_id=owner_id, fencing_token=1)
                    session.add(row)
                    await session.flush()
                    await session.execute(
                        update(RunControlRow)
                        .where(RunControlRow.run_id == run_id)
                        .values(lease_until=self._lease_until_expr())
                    )
                    await session.refresh(row)
                elif row.state == "CLOSED":
                    raise RunControlConflict("Run 已关闭，不能 claim")
                elif row.owner_id == owner_id and row.lease_until is not None:
                    renewed = await self._renew_locked(session, row, owner_id, int(row.fencing_token))
                    if renewed is not None:
                        return renewed
                    result = await self._takeover_locked(session, run_id, owner_id)
                    if result is None:
                        raise OwnershipLost("当前 owner 的 lease 已失效")
                    return result
                else:
                    takeover = await self._takeover_locked(session, run_id, owner_id)
                    if takeover is None:
                        raise RunControlConflict("Run 当前仍由其他 executor 持有")
                    return takeover
                return self._lease(row)
        except (RunControlError, PersistenceError):
            raise
        except Exception as exc:
            raise RunControlError("Run control claim 失败") from exc

    async def renew(self, lease: RunLease) -> RunLease:
        self._validate_lease(lease)
        try:
            async with self.database.transaction() as session:
                renewed = await session.execute(
                    update(RunControlRow)
                    .where(RunControlRow.run_id == lease.run_id, RunControlRow.owner_id == lease.owner_id,
                           RunControlRow.fencing_token == lease.fencing_token,
                           RunControlRow.state == "ACTIVE", RunControlRow.lease_until > func.now())
                    .values(lease_until=self._lease_until_expr(),
                            version=RunControlRow.version + 1, updated_at=func.now())
                    .returning(RunControlRow)
                )
                row = renewed.scalar_one_or_none()
                if row is None:
                    raise OwnershipLost("Run lease renew 被拒绝")
                return self._lease(row)
        except (RunControlError, PersistenceError):
            raise
        except Exception as exc:
            raise OwnershipLost("无法证明 Run lease 仍有效") from exc

    async def release(self, lease: RunLease) -> bool:
        self._validate_lease(lease)
        async with self.database.transaction() as session:
            result = await session.execute(
                update(RunControlRow).where(
                    RunControlRow.run_id == lease.run_id, RunControlRow.owner_id == lease.owner_id,
                    RunControlRow.fencing_token == lease.fencing_token, RunControlRow.state == "ACTIVE",
                ).values(
                    owner_id=None,
                    lease_until=func.now(),
                    version=RunControlRow.version + 1,
                    updated_at=func.now(),
                )
            )
            return result.rowcount == 1

    async def request_cancel(self, run_id: str, reason: str, *, command_id: str | None = None) -> CancelIntent:
        self._validate_identity(run_id, "run_id")
        self._validate_identity(reason, "reason")
        command_id = command_id or uuid4().hex
        self._validate_identity(command_id, "command_id")
        async with self.database.transaction() as session:
            await runtime_repository.lock_run_scope(session, run_id)
            control = (await session.execute(select(RunControlRow).where(RunControlRow.run_id == run_id).with_for_update())).scalar_one_or_none()
            if control is None:
                control = RunControlRow(run_id=run_id)
                session.add(control)
                await session.flush()
            elif control.state == "CLOSED":
                raise RunControlConflict("Run 已关闭，不能请求 CANCEL")
            existing = (await session.execute(select(RunControlCommandRow).where(RunControlCommandRow.run_id == run_id))).scalar_one_or_none()
            if existing is not None:
                if existing.reason != reason:
                    raise RunControlConflict("重复 CANCEL 的 payload 与既有 intent 冲突")
                return CancelIntent(run_id, existing.command_id, existing.reason, existing.created_at)
            command = RunControlCommandRow(command_id=command_id, run_id=run_id, command_type="CANCEL", reason=reason)
            session.add(command)
            control.cancel_command_id = command_id
            control.cancel_reason = reason
            control.cancel_requested_at = func.now()
            control.version += 1
            control.updated_at = func.now()
            await session.flush()
            await session.refresh(command)
            return CancelIntent(run_id, command.command_id, command.reason, command.created_at)

    async def cancel_intent(self, run_id: str) -> CancelIntent | None:
        async with self.database.session() as session:
            command = (await session.execute(select(RunControlCommandRow).where(
                RunControlCommandRow.run_id == run_id, RunControlCommandRow.command_type == "CANCEL"
            ))).scalar_one_or_none()
            if command is None:
                return None
            return CancelIntent(run_id, command.command_id, command.reason, command.created_at)

    async def assert_current(self, lease: RunLease) -> None:
        self._validate_lease(lease)
        async with self.database.session() as session:
            exists = (await session.execute(select(RunControlRow.run_id).where(
                RunControlRow.run_id == lease.run_id, RunControlRow.owner_id == lease.owner_id,
                RunControlRow.fencing_token == lease.fencing_token, RunControlRow.state == "ACTIVE",
                RunControlRow.lease_until > func.now()
            ))).scalar_one_or_none()
            if exists is None:
                raise OwnershipLost("Run fencing token 已过期或不是 current owner")

    async def finalize_terminal(self, lease: RunLease, event: RuntimeEvent, journal):
        """原子完成 fence 校验、Journal terminal append 与 control close。"""
        self._validate_lease(lease)
        if not isinstance(event, RuntimeEvent):
            raise TypeError("event 必须是 RuntimeEvent")
        append_in_transaction = getattr(journal, "append_in_transaction", None)
        if not callable(append_in_transaction):
            raise TypeError("journal 必须支持 append_in_transaction")
        async with self.database.transaction() as session:
            await runtime_repository.lock_run_scope(session, lease.run_id)
            row = (await session.execute(select(RunControlRow).where(
                RunControlRow.run_id == lease.run_id
            ).with_for_update())).scalar_one_or_none()
            if row is None or row.state != "ACTIVE" or row.owner_id != lease.owner_id \
                    or row.fencing_token != lease.fencing_token or row.lease_until is None:
                raise OwnershipLost("terminal write 的 fencing token 已失效")
            current = (await session.execute(select(RunControlRow.run_id).where(
                RunControlRow.run_id == lease.run_id,
                RunControlRow.lease_until > func.now(),
            ))).scalar_one_or_none()
            if current is None:
                raise OwnershipLost("terminal write 时 lease 已过期")
            append_status = await append_in_transaction(session, event)
            await self._close_terminal_locked(session, row, event.sequence)

        return append_status

    async def _close_terminal_locked(self, session, row: RunControlRow, sequence: int) -> None:
        """在 terminal Journal append 所在事务内关闭 coordination aggregate。"""
        row.state = "CLOSED"
        row.owner_id = None
        row.lease_until = None
        row.terminal_sequence = sequence
        row.version += 1
        row.updated_at = func.now()
        await session.flush()

    @staticmethod
    def _validate_identity(value: str, name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 必须是非空字符串")

    @classmethod
    def _validate_lease(cls, lease: RunLease) -> None:
        if not isinstance(lease, RunLease):
            raise TypeError("lease 必须是 RunLease")
        cls._validate_identity(lease.run_id, "run_id")
        cls._validate_identity(lease.owner_id, "owner_id")

    @staticmethod
    def _lease(row: RunControlRow) -> RunLease:
        assert row.lease_until is not None
        return RunLease(row.run_id, row.owner_id, row.lease_until, int(row.fencing_token), int(row.version))

    async def _renew_locked(self, session, row: RunControlRow, owner_id: str, token: int) -> RunLease | None:
        if row.owner_id != owner_id or row.fencing_token != token:
            return None
        result = await session.execute(update(RunControlRow).where(
            RunControlRow.run_id == row.run_id, RunControlRow.owner_id == owner_id,
            RunControlRow.fencing_token == token, RunControlRow.lease_until > func.now(),
        ).values(lease_until=self._lease_until_expr(), version=RunControlRow.version + 1, updated_at=func.now()).returning(RunControlRow))
        refreshed = result.scalar_one_or_none()
        return None if refreshed is None else self._lease(refreshed)

    async def _takeover_locked(self, session, run_id: str, owner_id: str) -> RunLease | None:
        result = await session.execute(
            update(RunControlRow)
            .where(
                RunControlRow.run_id == run_id,
                RunControlRow.state == "ACTIVE",
                or_(
                    RunControlRow.owner_id.is_(None),
                    RunControlRow.lease_until.is_(None),
                    RunControlRow.lease_until <= func.now(),
                ),
            )
            .values(
                owner_id=owner_id,
                fencing_token=RunControlRow.fencing_token + 1,
                lease_until=self._lease_until_expr(),
                version=RunControlRow.version + 1,
                updated_at=func.now(),
            )
            .returning(RunControlRow)
        )
        row = result.scalar_one_or_none()
        return None if row is None else self._lease(row)

    def _lease_until_expr(self):
        return func.now() + text(f"interval '{self.lease_seconds} seconds'")


__all__ = ["CancelIntent", "DurableRunControlService", "OwnershipLost", "RunControlConflict", "RunControlError", "RunLease"]
