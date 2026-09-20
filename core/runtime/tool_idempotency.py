"""Durable Tool side-effect intent and provider-specific reconciliation.

This module owns the PostgreSQL invocation aggregate for Tool Runtime.  It does
not own Run leases, approvals, or provider business state; those authorities
remain in WP1, WP2, and the provider adapter respectively.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
import inspect
from collections.abc import Mapping
from typing import Protocol
from uuid import uuid4

from sqlalchemy import func, select

from core.persistence.database import Database
from core.persistence.models import (
    DurableToolInvocationRow,
    ManualToolResolutionAuditRow,
    RunControlRow,
)
from core.persistence.repositories import runtime as runtime_repository
from core.runtime.run_control import OwnershipLost, RunLease
from core.runtime.tool_contract import (
    RetryDisposition,
    ToolExecutionResult,
    ToolExecutionStatus,
    ToolInvocation,
    ToolOutput,
    ToolSideEffectState,
    canonical_json_digest,
    safe_key_digest,
)


class ToolInvocationState(str, Enum):
    PREPARED = "PREPARED"
    STARTED = "STARTED"
    COMMITTED = "COMMITTED"
    UNKNOWN = "UNKNOWN"
    NOT_COMMITTED = "NOT_COMMITTED"


class ProviderReconciliationResult(str, Enum):
    COMMITTED = "COMMITTED"
    NOT_COMMITTED = "NOT_COMMITTED"
    STILL_PENDING = "STILL_PENDING"
    UNKNOWN = "UNKNOWN"


PROVIDER_RECONCILIATION_UNSUPPORTED = "PROVIDER_RECONCILIATION_UNSUPPORTED"
RECONCILIATION_LOOKUP_FAILED = "RECONCILIATION_LOOKUP_FAILED"


class ProviderReconciler(Protocol):
    def reconcile_provider(
        self,
        invocation: ToolInvocation,
        *,
        provider_operation_id: str | None,
    ) -> ProviderReconciliationResult: ...


@dataclass(frozen=True, slots=True)
class DurableToolInvocation:
    invocation_id: str
    run_id: str
    step_id: str
    tool_name: str
    arguments_digest: str
    invocation_binding_digest: str
    idempotency_key_digest: str
    resource_key_digest: str | None
    owner_id: str
    fencing_token: int
    approval_id: str | None
    execution_claim_id: str | None
    state: ToolInvocationState
    provider_operation_id: str | None
    uncertainty_reason: str | None
    version: int
    created_at: datetime
    started_at: datetime | None
    committed_at: datetime | None
    unknown_at: datetime | None
    reconciled_at: datetime | None
    committed_result: dict[str, object] | None
    digest: str | None
    reconcile_attempt_count: int = 0
    last_reconcile_at: datetime | None = None
    last_safe_error_code: str | None = None
    next_reconcile_at: datetime | None = None
    manual_required: bool = False

    @property
    def result_digest(self) -> str | None:
        """兼容调用方使用更具描述性的 result_digest 名称。"""
        return self.digest


@dataclass(frozen=True, slots=True)
class ManualResolutionAudit:
    """HTTP/operator-derived audit facts written with the state mutation."""

    actor_id: str
    tenant_id: str
    reason: str

    def __post_init__(self) -> None:
        for value, name, maximum in (
            (self.actor_id, "actor_id", 255),
            (self.tenant_id, "tenant_id", 255),
            (self.reason, "reason", 512),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"{name} 必须是 bounded non-empty text")


@dataclass(frozen=True, slots=True)
class ProviderReconciliationEvidence:
    """Provider truth plus an optional replayable Tool result.

    The existing enum-only provider contract remains valid for lookup callers.
    Durable-record reconciliation may additionally return the provider-owned
    result so the local aggregate can persist the exact result needed by the
    canonical ToolExecutionService replay path.
    """

    outcome: ProviderReconciliationResult
    result: ToolExecutionResult | Mapping[str, object] | None = None


def tool_invocation_binding_digest(invocation: ToolInvocation) -> str:
    """Build a stable binding from safe invocation identity fields only."""
    return canonical_json_digest(
        {
            "invocation_id": invocation.invocation_id,
            "tool_name": invocation.tool_name,
            "arguments_digest": invocation.arguments_digest,
            "idempotency_key_digest": safe_key_digest(invocation.idempotency_key),
            "resource_key_digest": safe_key_digest(invocation.resource_key),
        }
    )


class DurableToolInvocationService:
    """唯一负责 durable side-effect invocation aggregate 状态转换的 Service。"""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("database 必须是 Database")
        self.database = database

    async def prepare(
        self,
        *,
        lease: RunLease,
        step_id: str,
        invocation: ToolInvocation,
        tool_name: str,
        invocation_binding_digest: str | None = None,
        approval_id: str | None = None,
        execution_claim_id: str | None = None,
    ) -> DurableToolInvocation:
        binding = invocation_binding_digest or tool_invocation_binding_digest(invocation)
        idempotency_digest = safe_key_digest(invocation.idempotency_key) or safe_key_digest(invocation.invocation_id)
        assert idempotency_digest is not None
        row_values = {
            "invocation_id": invocation.invocation_id,
            "run_id": lease.run_id,
            "step_id": step_id,
            "tool_name": tool_name,
            "invocation_binding_digest": binding,
            "arguments_digest": invocation.arguments_digest,
            "idempotency_key_digest": idempotency_digest,
            "resource_key_digest": safe_key_digest(invocation.resource_key),
            "owner_id": lease.owner_id,
            "fencing_token": lease.fencing_token,
            "approval_id": approval_id,
            "execution_claim_id": execution_claim_id,
        }
        # 允许持久化 migration 与本模块分批落地；字段存在时必须写入，
        # 旧数据库则继续使用既有 invocation contract，后续 migration 补齐事实列。
        row_values = {
            key: value
            for key, value in row_values.items()
            if key in {
                "invocation_id", "run_id", "step_id", "tool_name",
                "invocation_binding_digest", "idempotency_key_digest",
                "resource_key_digest", "owner_id", "fencing_token",
                "approval_id", "execution_claim_id",
            }
            or hasattr(DurableToolInvocationRow, key)
        }
        async with self.database.transaction() as session:
            await self._lock_current_lease(session, lease)
            row = (await session.execute(
                select(DurableToolInvocationRow)
                .where(DurableToolInvocationRow.invocation_id == invocation.invocation_id)
                .with_for_update()
            )).scalar_one_or_none()
            if row is None:
                row = DurableToolInvocationRow(**row_values)
                session.add(row)
                await session.flush()
            else:
                binds_execution_claim = (
                    row.state == ToolInvocationState.PREPARED.value
                    and row.execution_claim_id is None
                    and execution_claim_id is not None
                )
                self._assert_identity(
                    row,
                    lease.run_id,
                    step_id,
                    tool_name,
                    binding,
                    invocation.arguments_digest,
                    idempotency_digest,
                    approval_id,
                    execution_claim_id,
                    allow_new_execution_claim=binds_execution_claim,
                )
                if row.state == ToolInvocationState.PREPARED.value:
                    if binds_execution_claim:
                        row.execution_claim_id = execution_claim_id
                        row.version += 1
                    if row.owner_id != lease.owner_id or row.fencing_token != lease.fencing_token:
                        row.owner_id = lease.owner_id
                        row.fencing_token = lease.fencing_token
                        row.version += 1
            return self._record(row)

    async def start(self, *, lease: RunLease, invocation_id: str) -> DurableToolInvocation:
        async with self.database.transaction() as session:
            await self._lock_current_lease(session, lease)
            row = await self._locked_row(session, invocation_id)
            self._assert_owner(row, lease)
            if row.state != ToolInvocationState.PREPARED.value:
                raise ValueError("STARTED 只能由 PREPARED 获取一次")
            row.state = ToolInvocationState.STARTED.value
            row.started_at = datetime.now(UTC)
            row.version += 1
            return self._record(row)

    async def committed(
        self,
        *,
        lease: RunLease,
        invocation_id: str,
        provider_operation_id: str | None = None,
        result: ToolExecutionResult | Mapping[str, object] | None = None,
    ) -> DurableToolInvocation:
        result_payload, result_digest = _committed_result_payload(result)
        async with self.database.transaction() as session:
            await self._lock_current_lease(session, lease)
            row = await self._locked_row(session, invocation_id)
            self._assert_owner(row, lease)
            if row.state == ToolInvocationState.COMMITTED.value:
                _assert_existing_result(row, result_payload, result_digest)
                return self._record(row)
            if row.state != ToolInvocationState.STARTED.value:
                raise ValueError("COMMITTED 只能从 STARTED 收口")
            row.state = ToolInvocationState.COMMITTED.value
            row.provider_operation_id = provider_operation_id or row.provider_operation_id
            row.committed_at = datetime.now(UTC)
            row.reconciled_at = None
            if result_payload is not None:
                _set_result_fields(row, result_payload, result_digest)
            row.version += 1
            return self._record(row)

    async def unknown(
        self,
        *,
        lease: RunLease,
        invocation_id: str,
        reason: str,
        provider_operation_id: str | None = None,
    ) -> DurableToolInvocation:
        if not reason or len(reason) > 128:
            raise ValueError("uncertainty reason 必须是 bounded safe text")
        async with self.database.transaction() as session:
            await self._lock_current_lease(session, lease)
            row = await self._locked_row(session, invocation_id)
            self._assert_owner(row, lease)
            if row.state == ToolInvocationState.COMMITTED.value:
                return self._record(row)
            if row.state == ToolInvocationState.UNKNOWN.value:
                return self._record(row)
            if row.state != ToolInvocationState.STARTED.value:
                raise ValueError("UNKNOWN 只能由 STARTED 产生")
            row.state = ToolInvocationState.UNKNOWN.value
            row.provider_operation_id = provider_operation_id or row.provider_operation_id
            row.uncertainty_reason = reason
            row.unknown_at = datetime.now(UTC)
            row.version += 1
            return self._record(row)

    async def get(self, invocation_id: str) -> DurableToolInvocation | None:
        async with self.database.session() as session:
            row = (await session.execute(
                select(DurableToolInvocationRow).where(
                    DurableToolInvocationRow.invocation_id == invocation_id
                )
            )).scalar_one_or_none()
            return None if row is None else self._record(row)

    async def reconcile(
        self,
        *,
        lease: RunLease,
        invocation: ToolInvocation,
        provider: ProviderReconciler,
        invocation_binding_digest: str | None = None,
    ) -> DurableToolInvocation:
        """Query/normalize through provider, then apply the local CAS decision."""
        binding = invocation_binding_digest or tool_invocation_binding_digest(invocation)
        row = await self._takeover_for_reconciliation(
            lease=lease,
            invocation=invocation,
            binding=binding,
        )
        if row.state in {ToolInvocationState.COMMITTED.value, ToolInvocationState.NOT_COMMITTED.value}:
            return row
        if row.state != ToolInvocationState.UNKNOWN.value:
            return row
        try:
            result = provider.reconcile_provider(
                invocation, provider_operation_id=row.provider_operation_id
            )
        except (TimeoutError, ConnectionError, OSError, ValueError, TypeError, RuntimeError):
            return await self.record_reconciliation_unknown(
                lease=lease, invocation_id=invocation.invocation_id,
                error_code=RECONCILIATION_LOOKUP_FAILED,
                max_attempts=5, initial_seconds=5, max_seconds=60,
            )
        if inspect.isawaitable(result):
            result = await result
        result, committed_result = _normalize_reconciliation_evidence(result)
        if result in {ProviderReconciliationResult.STILL_PENDING, ProviderReconciliationResult.UNKNOWN}:
            return await self.record_reconciliation_unknown(
                lease=lease, invocation_id=invocation.invocation_id,
                error_code="RECONCILIATION_STILL_UNKNOWN",
                max_attempts=5, initial_seconds=5, max_seconds=60,
            )
        async with self.database.transaction() as session:
            await self._lock_current_lease(session, lease)
            current = await self._locked_row(session, invocation.invocation_id)
            self._assert_owner(current, lease)
            self._assert_reconciliation_identity(current, lease.run_id, invocation, binding)
            if current.state in {ToolInvocationState.COMMITTED.value, ToolInvocationState.NOT_COMMITTED.value}:
                return self._record(current)
            if current.state != ToolInvocationState.UNKNOWN.value:
                raise ValueError("reconciliation requires UNKNOWN")
            current.state = (
                ToolInvocationState.COMMITTED.value
                if result is ProviderReconciliationResult.COMMITTED
                else ToolInvocationState.NOT_COMMITTED.value
            )
            current.reconciled_at = datetime.now(UTC)
            if result is ProviderReconciliationResult.COMMITTED:
                current.committed_at = current.committed_at or datetime.now(UTC)
                if committed_result is not None:
                    payload, digest = _committed_result_payload(committed_result)
                    assert payload is not None and digest is not None
                    _set_result_fields(current, payload, digest)
            current.version += 1
            return self._record(current)

    async def reconcile_durable_record(self, *, lease: RunLease, provider: ProviderReconciler, invocation_id: str) -> DurableToolInvocation:
        """对账 lane 的 durable identity 入口；不要求重新持久化原始 Tool 参数。"""
        async with self.database.session() as session:
            row = (await session.execute(select(DurableToolInvocationRow).where(DurableToolInvocationRow.invocation_id == invocation_id))).scalar_one_or_none()
            if row is None:
                raise ValueError("Tool invocation 不存在")
            if row.run_id != lease.run_id:
                raise ValueError("Tool invocation Run binding conflict")
        current = await self._takeover_for_reconciliation_record(lease=lease, invocation_id=invocation_id)
        if current.state in {ToolInvocationState.COMMITTED, ToolInvocationState.NOT_COMMITTED}:
            return current
        lookup = getattr(provider, "reconcile_durable", None)
        if not callable(lookup):
            return await self.record_reconciliation_unknown(
                lease=lease, invocation_id=invocation_id,
                error_code=PROVIDER_RECONCILIATION_UNSUPPORTED,
                max_attempts=1, initial_seconds=5, max_seconds=60,
            )
        try:
            result = lookup(current)
            if inspect.isawaitable(result):
                result = await result
            result, committed_result = _normalize_reconciliation_evidence(result)
        except Exception:
            return await self.record_reconciliation_unknown(
                lease=lease, invocation_id=invocation_id,
                error_code=RECONCILIATION_LOOKUP_FAILED,
                max_attempts=5, initial_seconds=5, max_seconds=60,
            )
        if result in {ProviderReconciliationResult.STILL_PENDING, ProviderReconciliationResult.UNKNOWN}:
            return await self.record_reconciliation_unknown(
                lease=lease, invocation_id=invocation_id,
                error_code="RECONCILIATION_STILL_UNKNOWN",
                max_attempts=5, initial_seconds=5, max_seconds=60,
            )
        if result is ProviderReconciliationResult.COMMITTED and current.committed_result is None and committed_result is None:
            return await self.record_reconciliation_unknown(
                lease=lease, invocation_id=invocation_id,
                error_code="RECONCILIATION_RESULT_MISSING",
                max_attempts=5, initial_seconds=5, max_seconds=60,
            )
        try:
            payload, digest = _committed_result_payload(committed_result)
        except (TypeError, ValueError):
            return await self.record_reconciliation_unknown(
                lease=lease, invocation_id=invocation_id,
                error_code="RECONCILIATION_RESULT_INVALID",
                max_attempts=5, initial_seconds=5, max_seconds=60,
            )
        async with self.database.transaction() as session:
            await self._lock_current_lease(session, lease)
            row = await self._locked_row(session, invocation_id)
            self._assert_owner(row, lease)
            if row.state in {ToolInvocationState.COMMITTED.value, ToolInvocationState.NOT_COMMITTED.value}:
                return self._record(row)
            if row.state != ToolInvocationState.UNKNOWN.value:
                raise ValueError("reconciliation requires UNKNOWN")
            row.state = result.value
            row.reconciled_at = datetime.now(UTC)
            row.manual_required = False
            row.next_reconcile_at = None
            row.last_safe_error_code = None
            if result is ProviderReconciliationResult.COMMITTED:
                row.committed_at = row.committed_at or datetime.now(UTC)
                if payload is not None and digest is not None:
                    _set_result_fields(row, payload, digest)
            row.version += 1
            return self._record(row)

    async def _takeover_for_reconciliation_record(self, *, lease: RunLease, invocation_id: str) -> DurableToolInvocation:
        async with self.database.transaction() as session:
            await self._lock_current_lease(session, lease)
            row = await self._locked_row(session, invocation_id)
            self._assert_owner_run(row, lease)
            if row.state == ToolInvocationState.STARTED.value:
                row.state = ToolInvocationState.UNKNOWN.value
                row.uncertainty_reason = "RECOVERY_AFTER_STARTED"
                row.unknown_at = datetime.now(UTC)
                row.version += 1
            if row.state == ToolInvocationState.UNKNOWN.value and (row.owner_id != lease.owner_id or row.fencing_token != lease.fencing_token):
                row.owner_id = lease.owner_id
                row.fencing_token = lease.fencing_token
                row.version += 1
            return self._record(row)

    async def record_reconciliation_unknown(
        self, *, lease: RunLease, invocation_id: str, error_code: str,
        max_attempts: int, initial_seconds: int, max_seconds: int,
    ) -> DurableToolInvocation:
        """保留 UNKNOWN，并以 durable bounded backoff 安排下一次查询。"""
        if not error_code or len(error_code) > 128 or max_attempts <= 0 or initial_seconds <= 0 or max_seconds < initial_seconds:
            raise ValueError("invalid reconciliation backoff configuration")
        async with self.database.transaction() as session:
            await self._lock_current_lease(session, lease)
            row = await self._locked_row(session, invocation_id)
            self._assert_owner(row, lease)
            if row.state not in {ToolInvocationState.STARTED.value, ToolInvocationState.UNKNOWN.value}:
                return self._record(row)
            if row.state == ToolInvocationState.STARTED.value:
                row.state = ToolInvocationState.UNKNOWN.value
                row.unknown_at = datetime.now(UTC)
            row.reconcile_attempt_count = int(getattr(row, "reconcile_attempt_count", 0)) + 1
            row.last_reconcile_at = datetime.now(UTC)
            row.last_safe_error_code = error_code
            row.manual_required = row.reconcile_attempt_count >= max_attempts
            row.next_reconcile_at = None if row.manual_required else datetime.now(UTC) + timedelta(seconds=min(max_seconds, initial_seconds * (2 ** (row.reconcile_attempt_count - 1))))
            row.version += 1
            return self._record(row)

    async def resolve_unknown_committed(
        self, *, lease: RunLease, invocation_id: str,
        provider_operation_id: str | None = None,
        result: ToolExecutionResult | Mapping[str, object] | None = None,
        manual_audit: ManualResolutionAudit | None = None,
    ) -> DurableToolInvocation:
        return await self._resolve_unknown(
            lease=lease, invocation_id=invocation_id,
            state=ToolInvocationState.COMMITTED,
            provider_operation_id=provider_operation_id,
            result=result,
            manual_audit=manual_audit,
        )

    async def resolve_unknown_not_committed(
        self, *, lease: RunLease, invocation_id: str,
        manual_audit: ManualResolutionAudit | None = None,
    ) -> DurableToolInvocation:
        return await self._resolve_unknown(
            lease=lease, invocation_id=invocation_id,
            state=ToolInvocationState.NOT_COMMITTED,
            manual_audit=manual_audit,
        )

    async def _resolve_unknown(
        self, *, lease: RunLease, invocation_id: str, state: ToolInvocationState,
        provider_operation_id: str | None = None,
        result: ToolExecutionResult | Mapping[str, object] | None = None,
        manual_audit: ManualResolutionAudit | None = None,
    ) -> DurableToolInvocation:
        if state not in {ToolInvocationState.COMMITTED, ToolInvocationState.NOT_COMMITTED}:
            raise ValueError("manual resolution must be terminal")
        payload, digest = _committed_result_payload(result)
        async with self.database.transaction() as session:
            await self._lock_current_lease(session, lease)
            row = await self._locked_row(session, invocation_id)
            if row.state in {ToolInvocationState.COMMITTED.value, ToolInvocationState.NOT_COMMITTED.value}:
                return self._record(row)
            if row.state != ToolInvocationState.UNKNOWN.value:
                raise ValueError("manual resolution requires UNKNOWN")
            # An expired worker may leave UNKNOWN owned by its old fence.  The
            # current fenced operator lease can take over only this UNKNOWN
            # aggregate; PREPARED/STARTED never receive this shortcut.
            if row.state == ToolInvocationState.UNKNOWN.value and (
                row.owner_id != lease.owner_id or row.fencing_token != lease.fencing_token
            ):
                row.owner_id = lease.owner_id
                row.fencing_token = lease.fencing_token
                row.version += 1
            self._assert_owner(row, lease)
            if manual_audit is not None:
                if manual_audit.reason == "" or row.run_id != lease.run_id:
                    raise ValueError("manual audit binding invalid")
                session.add(
                    ManualToolResolutionAuditRow(
                        audit_id=uuid4().hex,
                        actor_id=manual_audit.actor_id,
                        tenant_id=manual_audit.tenant_id,
                        run_id=row.run_id,
                        step_id=row.step_id,
                        invocation_id=row.invocation_id,
                        tool_name=row.tool_name,
                        resolution=state.value,
                        reason=manual_audit.reason,
                    )
                )
            row.state = state.value
            row.provider_operation_id = provider_operation_id or row.provider_operation_id
            row.reconciled_at = datetime.now(UTC)
            row.manual_required = False
            row.next_reconcile_at = None
            row.last_safe_error_code = "MANUAL_RESOLUTION"
            if state is ToolInvocationState.COMMITTED:
                if payload is not None:
                    _set_result_fields(row, payload, digest)
                row.committed_at = row.committed_at or datetime.now(UTC)
            row.version += 1
            return self._record(row)

    async def _takeover_for_reconciliation(
        self,
        *,
        lease: RunLease,
        invocation: ToolInvocation,
        binding: str,
    ) -> DurableToolInvocation:
        async with self.database.transaction() as session:
            await self._lock_current_lease(session, lease)
            row = await self._locked_row(session, invocation.invocation_id)
            self._assert_reconciliation_identity(row, lease.run_id, invocation, binding)
            ownership_changed = (
                row.owner_id != lease.owner_id or row.fencing_token != lease.fencing_token
            )
            if row.state in {
                ToolInvocationState.STARTED.value,
                ToolInvocationState.UNKNOWN.value,
            }:
                row.owner_id = lease.owner_id
                row.fencing_token = lease.fencing_token
            if row.state == ToolInvocationState.STARTED.value:
                row.state = ToolInvocationState.UNKNOWN.value
                row.uncertainty_reason = "RECOVERY_AFTER_STARTED"
                row.unknown_at = datetime.now(UTC)
                row.version += 1
            elif row.state == ToolInvocationState.UNKNOWN.value and ownership_changed:
                row.version += 1
            return self._record(row)

    @staticmethod
    async def _locked_row(session, invocation_id: str) -> DurableToolInvocationRow:
        row = (await session.execute(
            select(DurableToolInvocationRow)
            .where(DurableToolInvocationRow.invocation_id == invocation_id)
            .with_for_update()
        )).scalar_one_or_none()
        if row is None:
            raise ValueError("Tool invocation 不存在")
        return row

    @staticmethod
    async def _lock_current_lease(session, lease: RunLease) -> None:
        await runtime_repository.lock_run_scope(session, lease.run_id)
        current = (await session.execute(
            select(RunControlRow).where(
                RunControlRow.run_id == lease.run_id,
                RunControlRow.owner_id == lease.owner_id,
                RunControlRow.fencing_token == lease.fencing_token,
                RunControlRow.state == "ACTIVE",
                RunControlRow.lease_until > func.now(),
            ).with_for_update()
        )).scalar_one_or_none()
        if current is None:
            raise OwnershipLost("Tool side-effect mutation 的 Run fencing 已失效")

    @staticmethod
    def _assert_owner(row: DurableToolInvocationRow, lease: RunLease) -> None:
        if row.run_id != lease.run_id or row.owner_id != lease.owner_id or row.fencing_token != lease.fencing_token:
            raise OwnershipLost("Tool invocation 不属于 current fenced executor")

    @staticmethod
    def _assert_owner_run(row: DurableToolInvocationRow, lease: RunLease) -> None:
        if row.run_id != lease.run_id:
            raise OwnershipLost("Tool invocation 不属于 current Run")

    @staticmethod
    def _assert_identity(
        row,
        run_id: str,
        step_id: str,
        tool_name: str,
        binding: str,
        arguments_digest: str,
        idempotency_digest: str,
        approval_id: str | None,
        execution_claim_id: str | None,
        *,
        allow_new_execution_claim: bool = False,
    ) -> None:
        if (
            row.run_id != run_id
            or row.step_id != step_id
            or row.tool_name != tool_name
            or row.invocation_binding_digest != binding
            or row.idempotency_key_digest != idempotency_digest
            or row.approval_id != approval_id
            or (
                row.execution_claim_id != execution_claim_id
                and not (
                    allow_new_execution_claim
                    and row.execution_claim_id is None
                    and execution_claim_id is not None
                )
            )
        ):
            raise ValueError("Tool invocation immutable binding conflict")
        persisted_arguments_digest = getattr(row, "arguments_digest", None)
        if persisted_arguments_digest is not None:
            if persisted_arguments_digest != arguments_digest:
                raise ValueError("Tool invocation arguments digest is invalid")

    @staticmethod
    def _assert_reconciliation_identity(
        row: DurableToolInvocationRow,
        run_id: str,
        invocation: ToolInvocation,
        binding: str,
    ) -> None:
        idempotency_digest = safe_key_digest(invocation.idempotency_key) or safe_key_digest(
            invocation.invocation_id
        )
        if (
            row.run_id != run_id
            or row.tool_name != invocation.tool_name
            or row.invocation_binding_digest != binding
            or (
                getattr(row, "arguments_digest", invocation.arguments_digest)
                != invocation.arguments_digest
            )
            or row.idempotency_key_digest != idempotency_digest
            or row.resource_key_digest != safe_key_digest(invocation.resource_key)
        ):
            raise ValueError("Tool invocation immutable binding conflict")

    @staticmethod
    def _record(row: DurableToolInvocationRow) -> DurableToolInvocation:
        committed_result = getattr(row, "committed_result", None)
        digest = getattr(row, "digest", None)
        if digest is None:
            digest = getattr(row, "result_digest", None)
        if digest is None:
            digest = getattr(row, "committed_result_digest", None)
        return DurableToolInvocation(
            invocation_id=row.invocation_id,
            run_id=row.run_id,
            step_id=row.step_id,
            tool_name=row.tool_name,
            arguments_digest=getattr(row, "arguments_digest", ""),
            invocation_binding_digest=row.invocation_binding_digest,
            idempotency_key_digest=row.idempotency_key_digest,
            resource_key_digest=row.resource_key_digest,
            owner_id=row.owner_id,
            fencing_token=int(row.fencing_token),
            approval_id=row.approval_id,
            execution_claim_id=row.execution_claim_id,
            state=ToolInvocationState(row.state),
            provider_operation_id=row.provider_operation_id,
            uncertainty_reason=row.uncertainty_reason,
            version=int(row.version),
            created_at=row.created_at,
            started_at=row.started_at,
            committed_at=row.committed_at,
            unknown_at=row.unknown_at,
            reconciled_at=row.reconciled_at,
            committed_result=committed_result,
            digest=digest,
            reconcile_attempt_count=int(getattr(row, "reconcile_attempt_count", 0)),
            last_reconcile_at=getattr(row, "last_reconcile_at", None),
            last_safe_error_code=getattr(row, "last_safe_error_code", None),
            next_reconcile_at=getattr(row, "next_reconcile_at", None),
            manual_required=bool(getattr(row, "manual_required", False)),
        )

    @staticmethod
    def restore_committed_result(record: DurableToolInvocation) -> ToolExecutionResult:
        """从 durable COMMITTED payload 恢复完整 ToolExecutionResult；缺失即 fail closed。"""
        if record.state is not ToolInvocationState.COMMITTED:
            raise ValueError("只有 COMMITTED invocation 才能恢复结果")
        payload = record.committed_result
        if payload is None or record.digest is None:
            raise ValueError("COMMITTED invocation 缺少 durable result")
        if canonical_json_digest(payload) != record.digest:
            raise ValueError("COMMITTED invocation result digest mismatch")
        return _tool_execution_result_from_payload(payload)


__all__ = [
    "DurableToolInvocation",
    "DurableToolInvocationService",
    "ManualResolutionAudit",
    "ProviderReconciliationEvidence",
    "ProviderReconciliationResult",
    "ProviderReconciler",
    "ToolInvocationState",
    "tool_invocation_binding_digest",
    "PROVIDER_RECONCILIATION_UNSUPPORTED",
    "RECONCILIATION_LOOKUP_FAILED",
]


def _committed_result_payload(
    result: ToolExecutionResult | Mapping[str, object] | None,
) -> tuple[dict[str, object] | None, str | None]:
    if result is None:
        return None, None
    if isinstance(result, ToolExecutionResult):
        payload = result.to_safe_dict(include_output=True)
    elif isinstance(result, Mapping):
        payload = dict(result)
    else:
        raise TypeError("committed result 必须是 ToolExecutionResult 或 JSON object")
    digest = canonical_json_digest(payload)
    return payload, digest


def _normalize_reconciliation_evidence(
    value: ProviderReconciliationResult | ProviderReconciliationEvidence,
) -> tuple[ProviderReconciliationResult, ToolExecutionResult | Mapping[str, object] | None]:
    if isinstance(value, ProviderReconciliationResult):
        return value, None
    if isinstance(value, ProviderReconciliationEvidence):
        if not isinstance(value.outcome, ProviderReconciliationResult):
            raise TypeError("provider reconciliation outcome must be ProviderReconciliationResult")
        if value.outcome is not ProviderReconciliationResult.COMMITTED and value.result is not None:
            raise ValueError("only COMMITTED reconciliation may carry a result")
        return value.outcome, value.result
    raise TypeError("provider reconciliation must return ProviderReconciliationResult or ProviderReconciliationEvidence")


def _set_result_fields(
    row: DurableToolInvocationRow,
    payload: dict[str, object],
    digest: str,
) -> None:
    if hasattr(row, "committed_result"):
        row.committed_result = payload
    if hasattr(row, "digest"):
        row.digest = digest
    elif hasattr(row, "result_digest"):
        row.result_digest = digest
    elif hasattr(row, "committed_result_digest"):
        row.committed_result_digest = digest


def _assert_existing_result(
    row: DurableToolInvocationRow,
    payload: dict[str, object] | None,
    digest: str | None,
) -> None:
    if payload is None:
        return
    stored_payload = getattr(row, "committed_result", None)
    stored_digest = (
        getattr(row, "digest", None)
        or getattr(row, "result_digest", None)
        or getattr(row, "committed_result_digest", None)
    )
    if stored_payload is None or stored_digest != digest or stored_payload != payload:
        raise ValueError("COMMITTED invocation result binding conflict")


def _tool_execution_result_from_payload(
    payload: Mapping[str, object],
) -> ToolExecutionResult:
    output_payload = payload.get("output")
    if not isinstance(output_payload, Mapping) or "content" not in output_payload:
        raise ValueError("COMMITTED Tool result 缺少完整 output content")
    try:
        output = ToolOutput(
            content_type=str(output_payload["content_type"]),
            content=output_payload["content"],
            original_size_bytes=int(output_payload["original_size_bytes"]),
            returned_size_bytes=int(output_payload["returned_size_bytes"]),
            truncated=bool(output_payload["truncated"]),
            digest=str(output_payload["digest"]),
        )
        return ToolExecutionResult(
            invocation_id=str(payload["invocation_id"]),
            attempt_id=str(payload["attempt_id"]),
            tool_name=str(payload["tool_name"]),
            status=ToolExecutionStatus(str(payload["status"])),
            output=output,
            safe_summary=str(payload["safe_summary"]),
            side_effect_state=ToolSideEffectState(str(payload["side_effect_state"])),
            idempotency_replayed=bool(payload["idempotency_replayed"]),
            retry_disposition=RetryDisposition(str(payload["retry_disposition"])),
            resource_key_digest=payload.get("resource_key_digest"),
            started_at=datetime.fromisoformat(str(payload["started_at"])),
            completed_at=datetime.fromisoformat(str(payload["completed_at"])),
            duration_ms=int(payload["duration_ms"]),
            retry_index=int(payload.get("retry_index", 0)),
            worker_terminated=bool(payload.get("worker_terminated", True)),
            execution_detached=bool(payload.get("execution_detached", False)),
            resource_release_pending=bool(payload.get("resource_release_pending", False)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("COMMITTED Tool result payload invalid") from exc
