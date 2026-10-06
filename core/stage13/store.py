"""Provider 独立 PostgreSQL truth；复用 Database，不使用 Runtime canonical 表。"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.persistence.database import Database
from core.stage13.contracts import (
    LookupResult,
    ProviderError,
    SealReceipt,
    SubmitReceipt,
    SubmitRequest,
    TERMINAL_STATES,
)
from core.stage13.workload import ExecutionPlan, Stage13Workload

PROVIDER_TABLES = ("stage13_provider_namespaces", "stage13_provider_keys")


class ProviderBase(DeclarativeBase):
    """独立 schema metadata owner；同一个现有 Alembic ledger。"""


class NamespaceRow(ProviderBase):
    __tablename__ = "stage13_provider_namespaces"
    provider_namespace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_time: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    evidence_retention_until: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    __table_args__ = (
        CheckConstraint("logical_time >= 0", name="ck_stage13_provider_clock"),
    )


class ProviderKeyRow(ProviderBase):
    __tablename__ = "stage13_provider_keys"
    provider_namespace_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("stage13_provider_namespaces.provider_namespace_id"),
        primary_key=True,
    )
    business_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    remote_execution_id: Mapped[str | None] = mapped_column(String(36))
    receipt: Mapped[dict | None] = mapped_column(JSONB)
    seal_receipt: Mapped[dict | None] = mapped_column(JSONB)
    request: Mapped[dict | None] = mapped_column(JSONB)
    plan: Mapped[dict | None] = mapped_column(JSONB)
    state: Mapped[str | None] = mapped_column(String(32))
    status_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    accepted_logical_time: Mapped[int | None] = mapped_column(BigInteger)
    terminal_logical_time: Mapped[int | None] = mapped_column(BigInteger)
    result_delay_seconds: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    response_loss_remaining: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    __table_args__ = (
        UniqueConstraint("remote_execution_id", name="uq_stage13_provider_remote_id"),
        CheckConstraint(
            "(remote_execution_id IS NULL AND seal_receipt IS NOT NULL AND receipt IS NULL AND state IS NULL AND request IS NULL AND plan IS NULL AND status_revision = 0) OR (remote_execution_id IS NOT NULL AND seal_receipt IS NULL AND receipt IS NOT NULL AND request IS NOT NULL AND plan IS NOT NULL AND state IS NOT NULL AND state IN ('QUEUED','RUNNING','COMPLETED','INFRA_FAILED','CANCELLED') AND status_revision >= 1)",
            name="ck_stage13_provider_key_kind",
        ),
        CheckConstraint(
            "result_delay_seconds >= 0 AND response_loss_remaining >= 0",
            name="ck_stage13_provider_fault_bounds",
        ),
    )


class ProviderStore:
    def __init__(self, database: Database, workload: Stage13Workload):
        self.database = database
        self.workload = workload
        self.namespace = workload.config.provider_namespace_id

    async def initialize(self):
        """仅注册 frozen namespace，不隐式建表、不迁移、不覆盖已有配置。"""
        async with self.database.transaction() as session:
            await session.execute(
                insert(NamespaceRow)
                .values(
                    provider_namespace_id=self.namespace,
                    config=self.workload.config.model_dump(mode="json"),
                    manifest=self.workload.manifest,
                    manifest_digest=self.workload.manifest["manifest_digest"],
                    logical_time=0,
                    evidence_retention_until=0,
                )
                .on_conflict_do_nothing()
            )
            row = await session.get(NamespaceRow, self.namespace)
            if row.manifest_digest != self.workload.manifest["manifest_digest"]:
                raise ProviderError("NAMESPACE_CONFLICT")

    def _key_query(self, key):
        return select(ProviderKeyRow).where(
            ProviderKeyRow.provider_namespace_id == self.namespace,
            ProviderKeyRow.business_key == key,
        )

    @staticmethod
    def _lookup_result(row, digest):
        if row is None:
            return LookupResult(result="NOT_CREATED")
        if row.request_digest != digest:
            raise ProviderError("CONFLICT")
        if row.remote_execution_id is None:
            return LookupResult(
                result="NOT_CREATED_FINAL",
                seal_receipt=SealReceipt.model_validate(row.seal_receipt),
            )
        return LookupResult(
            result="FOUND", receipt=SubmitReceipt.model_validate(row.receipt)
        )

    async def submit(
        self,
        request: SubmitRequest,
        plan: ExecutionPlan,
        *,
        lose_response=False,
        result_delay_seconds=0,
    ):
        replayed = False
        # 唯一键的 INSERT ON CONFLICT 与随后锁行形成 submit/seal 共同线性化边界。
        async with self.database.transaction() as session:
            namespace_row = await session.get(NamespaceRow, self.namespace)
            if namespace_row is None:
                raise ProviderError("NAMESPACE_NOT_INITIALIZED")
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            receipt = SubmitReceipt(
                provider_namespace_id=self.namespace,
                remote_execution_business_key=request.remote_execution_business_key,
                request_digest=request.request_digest,
                remote_execution_id=uuid4(),
                accepted_at=now,
                status_revision=1,
            )
            inserted = (
                await session.execute(
                    insert(ProviderKeyRow)
                    .values(
                        provider_namespace_id=self.namespace,
                        business_key=request.remote_execution_business_key,
                        request_digest=request.request_digest,
                        remote_execution_id=str(receipt.remote_execution_id),
                        receipt=receipt.model_dump(mode="json"),
                        request=request.model_dump(mode="json"),
                        plan=plan.compact(),
                        state="QUEUED",
                        status_revision=1,
                        accepted_logical_time=namespace_row.logical_time,
                        response_loss_remaining=int(lose_response),
                        result_delay_seconds=result_delay_seconds,
                    )
                    .on_conflict_do_nothing()
                    .returning(ProviderKeyRow.business_key)
                )
            ).scalar_one_or_none()
            row = (
                await session.execute(
                    self._key_query(
                        request.remote_execution_business_key
                    ).with_for_update()
                )
            ).scalar_one()
            # seal 后对任何 digest 的迟到 submit 都是 KEY_CLOSED。
            if row.remote_execution_id is None:
                raise ProviderError("KEY_CLOSED")
            result = self._lookup_result(row, request.request_digest)
            replayed = inserted is None
            loss = row.response_loss_remaining > 0
            if loss:
                row.response_loss_remaining -= 1
            receipt = result.receipt
        # 明确在 durable commit 之后丢响应。计数也已持久消耗，重启不会再注入同一次。
        if loss:
            raise ProviderError("RESPONSE_LOST_AFTER_COMMIT")
        return receipt, replayed

    async def lookup(
        self, key: str, digest: str, remote_id: str | None = None
    ) -> LookupResult:
        async with self.database.session() as session:
            row = (await session.execute(self._key_query(key))).scalar_one_or_none()
            result = self._lookup_result(row, digest)
            if remote_id is not None and (
                result.receipt is None
                or str(result.receipt.remote_execution_id) != remote_id
            ):
                raise ProviderError("REMOTE_ID_BINDING_MISMATCH")
            return result

    async def seal(self, key: str, digest: str) -> LookupResult:
        async with self.database.transaction() as session:
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            receipt = SealReceipt(
                provider_namespace_id=self.namespace,
                remote_execution_business_key=key,
                request_digest=digest,
                sealed_at=now,
            )
            await session.execute(
                insert(ProviderKeyRow)
                .values(
                    provider_namespace_id=self.namespace,
                    business_key=key,
                    request_digest=digest,
                    seal_receipt=receipt.model_dump(mode="json"),
                    status_revision=0,
                    result_delay_seconds=0,
                    response_loss_remaining=0,
                )
                .on_conflict_do_nothing()
            )
            row = (
                await session.execute(self._key_query(key).with_for_update())
            ).scalar_one()
            return self._lookup_result(row, digest)

    async def read_execution(self, key: str, digest: str, remote_id: str):
        """短事务按 durable logical clock 推进；不持锁模拟等待。"""
        async with self.database.transaction() as session:
            row = (
                await session.execute(self._key_query(key).with_for_update())
            ).scalar_one_or_none()
            result = self._lookup_result(row, digest)
            if (
                result.receipt is None
                or str(result.receipt.remote_execution_id) != remote_id
            ):
                raise ProviderError("REMOTE_ID_BINDING_MISMATCH")
            clock = (await session.get(NamespaceRow, self.namespace)).logical_time
            if row.state == "QUEUED" and clock >= row.accepted_logical_time + 60:
                row.state = "RUNNING"
                row.status_revision += 1
                await session.flush()
            terminal_due = row.accepted_logical_time + 60 + row.plan["duration_seconds"]
            if row.state == "RUNNING" and clock >= terminal_due:
                row.state = "COMPLETED"
                row.status_revision += 1
                row.terminal_logical_time = terminal_due
                await self._retain_until(session, terminal_due)
            retention_until = (
                await session.execute(
                    select(NamespaceRow.evidence_retention_until).where(
                        NamespaceRow.provider_namespace_id == self.namespace
                    )
                )
            ).scalar_one()
            return {
                "request": row.request,
                "receipt": row.receipt,
                "plan": row.plan,
                "state": row.state,
                "status_revision": row.status_revision,
                "clock": clock,
                "terminal_logical_time": row.terminal_logical_time,
                "result_delay_seconds": row.result_delay_seconds,
                "evidence_retention_until": retention_until,
            }

    async def _retain_until(self, session, terminal_time):
        await session.execute(
            update(NamespaceRow)
            .where(NamespaceRow.provider_namespace_id == self.namespace)
            .values(
                evidence_retention_until=func.greatest(
                    NamespaceRow.evidence_retention_until, terminal_time + 7 * 86400
                )
            )
        )

    async def advance_clock(self, logical_time: int):
        if type(logical_time) is not int or logical_time < 0:
            raise ValueError("logical clock 必须是非负整数")
        async with self.database.transaction() as session:
            row = (
                await session.execute(
                    select(NamespaceRow)
                    .where(NamespaceRow.provider_namespace_id == self.namespace)
                    .with_for_update()
                )
            ).scalar_one()
            if logical_time < row.logical_time:
                raise ProviderError("CLOCK_CANNOT_REWIND")
            row.logical_time = logical_time

    async def advance_execution(self, key: str, target: str):
        if target not in {"RUNNING", *TERMINAL_STATES}:
            raise ProviderError("INVALID_REMOTE_TRANSITION")
        async with self.database.transaction() as session:
            row = (
                await session.execute(self._key_query(key).with_for_update())
            ).scalar_one_or_none()
            if row is None or row.remote_execution_id is None:
                raise ProviderError("EXECUTION_NOT_FOUND")
            if row.state == target:
                return
            allowed = {
                "QUEUED": {"RUNNING", "INFRA_FAILED", "CANCELLED"},
                "RUNNING": TERMINAL_STATES,
            }
            if target not in allowed.get(row.state, set()):
                raise ProviderError("INVALID_REMOTE_TRANSITION")
            row.state = target
            row.status_revision += 1
            if target in TERMINAL_STATES:
                row.terminal_logical_time = (
                    await session.get(NamespaceRow, self.namespace)
                ).logical_time
                await self._retain_until(session, row.terminal_logical_time)

    async def counts(self) -> dict:
        async with self.database.session() as session:
            rows = (
                await session.execute(
                    select(
                        func.count(),
                        func.count(ProviderKeyRow.remote_execution_id),
                    ).where(ProviderKeyRow.provider_namespace_id == self.namespace)
                )
            ).one()
            return {
                "key_rows": rows[0],
                "unique_executions": rows[1],
                "sealed_keys": rows[0] - rows[1],
            }
