"""Durable Tool side-effect intent and provider-specific reconciliation.

This module owns the PostgreSQL invocation aggregate for Tool Runtime.  It does
not own Run leases, approvals, or provider business state; those authorities
remain in WP1, WP2, and the provider adapter respectively.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
import inspect
from collections.abc import Mapping
from typing import Protocol

from sqlalchemy import func, select

from core.persistence.database import Database
from core.persistence.models import DurableToolInvocationRow, RunControlRow
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

    @property
    def result_digest(self) -> str | None:
        """兼容调用方使用更具描述性的 result_digest 名称。"""
        return self.digest


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
        result = provider.reconcile_provider(
            invocation, provider_operation_id=row.provider_operation_id
        )
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ProviderReconciliationResult):
            raise TypeError("provider reconciliation must return ProviderReconciliationResult")
        if result in {ProviderReconciliationResult.STILL_PENDING, ProviderReconciliationResult.UNKNOWN}:
            return await self.get(invocation.invocation_id)  # type: ignore[return-value]
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
            current.version += 1
            return self._record(current)

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
    "ProviderReconciliationResult",
    "ProviderReconciler",
    "ToolInvocationState",
    "tool_invocation_binding_digest",
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
