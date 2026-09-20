"""Narrow repository for the Stage10 canonical execution aggregate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable, Mapping

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.persistence.database import Database
from core.persistence.models import (
    DurableApprovalRow,
    DurableToolExecutionClaimRow,
    DurableToolInvocationRow,
    RunControlRow,
    RuntimeEventJournalRow,
    RuntimeModelInvocationRow,
    RuntimeRunExecutionRow,
    RuntimeStepExecutionRow,
)
from core.runtime.execution_aggregate import (
    ExecutionRootInput,
    ExecutionStatus,
    ModelAttemptState,
    StepExecutionStatus,
    canonical_payload_digest,
)
from core.runtime.run_control import DurableRunControlService, RunLease
from core.runtime.snapshot_serialization import to_primitive

JournalAppend = Callable[[AsyncSession], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class RecoveryImage:
    root: RuntimeRunExecutionRow
    steps: tuple[RuntimeStepExecutionRow, ...]
    models: tuple[RuntimeModelInvocationRow, ...]
    tool_invocations: tuple[DurableToolInvocationRow, ...] = ()
    approvals: tuple[DurableApprovalRow, ...] = ()
    execution_claims: tuple[DurableToolExecutionClaimRow, ...] = ()

    @property
    def tools(self) -> tuple[DurableToolInvocationRow, ...]:
        return self.tool_invocations

    @property
    def tool_rows(self) -> tuple[DurableToolInvocationRow, ...]:
        return self.tool_invocations

    @property
    def approval_requests(self) -> tuple[DurableApprovalRow, ...]:
        return self.approvals

    @property
    def approval_rows(self) -> tuple[DurableApprovalRow, ...]:
        return self.approvals


class DurableExecutionRepository:
    """Persistence owner for canonical Run/Step/Model execution rows."""

    def __init__(self, database: Database, run_control: DurableRunControlService | None = None) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be Database")
        self.database = database
        self.run_control = run_control

    async def initialize(self, root: ExecutionRootInput, *, lease: RunLease | None = None, fencing_token: int | None = None) -> None:
        """Persist root, immutable Plan and initial Steps before execution."""
        if not isinstance(root, ExecutionRootInput):
            raise TypeError("root must be ExecutionRootInput")
        if self.run_control is not None and lease is None:
            raise ValueError("lease is required for coordinated initialization")
        if lease is not None and lease.run_id != root.run_id:
            raise ValueError("lease and root run_id must match")
        if fencing_token is not None and (isinstance(fencing_token, bool) or not isinstance(fencing_token, int) or fencing_token < 0):
            raise ValueError("fencing_token must be a non-negative integer")
        token = lease.fencing_token if lease is not None else (fencing_token or 0)
        values = root.as_row_values()
        values["last_committed_fencing_token"] = token
        async with self.database.transaction() as session:
            if self.run_control is not None:
                await self.run_control.assert_current_in_transaction(session, lease)
            session.add(RuntimeRunExecutionRow(**values))
            for step in root.plan.steps:
                session.add(RuntimeStepExecutionRow(
                    run_id=root.run_id, step_id=step.step_id, plan_version=root.plan.version,
                    status=StepExecutionStatus.PENDING.value, current_attempt=0,
                    execution_kind=step.execution_kind.value,
                    risk_classification=step.capability_requirements.risk_level.value,
                    last_committed_fencing_token=token,
                ))
            await session.flush()

    async def load(self, run_id: str, *, session: AsyncSession | None = None) -> RecoveryImage | None:
        """Load all recovery sub-aggregates from one DB transaction snapshot."""
        async def read(db: AsyncSession) -> RecoveryImage | None:
            root = (await db.execute(select(RuntimeRunExecutionRow).where(RuntimeRunExecutionRow.run_id == run_id))).scalar_one_or_none()
            if root is None:
                return None
            steps = tuple((await db.execute(select(RuntimeStepExecutionRow).where(RuntimeStepExecutionRow.run_id == run_id).order_by(RuntimeStepExecutionRow.step_id))).scalars().all())
            models = tuple((await db.execute(select(RuntimeModelInvocationRow).where(RuntimeModelInvocationRow.run_id == run_id).order_by(RuntimeModelInvocationRow.step_id, RuntimeModelInvocationRow.attempt_number, RuntimeModelInvocationRow.model_attempt_number))).scalars().all())
            tools = tuple((await db.execute(select(DurableToolInvocationRow).where(DurableToolInvocationRow.run_id == run_id).order_by(DurableToolInvocationRow.step_id, DurableToolInvocationRow.invocation_id))).scalars().all())
            approvals = tuple((await db.execute(select(DurableApprovalRow).where(DurableApprovalRow.run_id == run_id).order_by(DurableApprovalRow.step_id, DurableApprovalRow.approval_id))).scalars().all())
            claims = tuple((await db.execute(select(DurableToolExecutionClaimRow).where(DurableToolExecutionClaimRow.run_id == run_id).order_by(DurableToolExecutionClaimRow.approval_id))).scalars().all())
            return RecoveryImage(root, steps, models, tools, approvals, claims)
        if session is not None:
            return await read(session)
        # A plain session gives each SELECT a separate READ COMMITTED snapshot.
        async with self.database.transaction() as db:
            return await read(db)

    async def stale_run_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be positive")
        async with self.database.session() as session:
            result = await session.execute(
                select(RuntimeRunExecutionRow.run_id)
                .join(RunControlRow, RunControlRow.run_id == RuntimeRunExecutionRow.run_id)
                .where(
                    RunControlRow.state == "ACTIVE",
                    or_(RunControlRow.lease_until.is_(None), RunControlRow.lease_until <= func.now()),
                    RuntimeRunExecutionRow.status.notin_(("SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED")),
                    RuntimeRunExecutionRow.recovery_supported.is_(True),
                    RuntimeRunExecutionRow.manual_required.is_(False),
                    or_(RuntimeRunExecutionRow.next_recovery_at.is_(None), RuntimeRunExecutionRow.next_recovery_at <= func.now()),
                    ~exists(select(1).where(
                        RuntimeEventJournalRow.run_id == RuntimeRunExecutionRow.run_id,
                        RuntimeEventJournalRow.event_type == "RUN_COMPLETED",
                    )),
                ).order_by(RuntimeRunExecutionRow.updated_at).limit(limit)
            )
            return tuple(result.scalars().all())

    async def start_step(self, lease: RunLease, *, step_id: str, plan_version: int, attempt: int | None = None, journal_append: JournalAppend | None = None) -> int:
        """Fenced ``PENDING -> RUNNING`` transition with RUNNING replay."""
        if attempt is not None and (isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0):
            raise ValueError("attempt must be a positive integer or None")
        async with self.database.transaction() as session:
            if self.run_control is not None:
                await self.run_control.assert_current_in_transaction(session, lease)
            row = (await session.execute(select(RuntimeStepExecutionRow).where(
                RuntimeStepExecutionRow.run_id == lease.run_id,
                RuntimeStepExecutionRow.step_id == step_id,
                RuntimeStepExecutionRow.plan_version == plan_version,
            ).with_for_update())).scalar_one_or_none()
            if row is None:
                raise ValueError("unknown durable step")
            if row.status == StepExecutionStatus.RUNNING.value:
                if attempt is not None and row.current_attempt != attempt:
                    raise RuntimeError("stale step attempt")
                return int(row.current_attempt)
            if row.status != StepExecutionStatus.PENDING.value:
                raise RuntimeError("durable step is not startable")
            next_attempt = int(row.current_attempt) + 1
            if attempt is not None and attempt != next_attempt:
                raise RuntimeError("stale step attempt")
            row.status = StepExecutionStatus.RUNNING.value
            row.current_attempt = next_attempt
            row.version += 1
            row.last_committed_fencing_token = lease.fencing_token
            row.updated_at = func.now()
            if journal_append is not None:
                if not callable(journal_append):
                    raise TypeError("journal_append must be callable")
                await journal_append(session)
            await session.flush()
            return next_attempt

    async def prepare_recovery(self, lease: RunLease) -> RecoveryImage:
        """Classify in-flight work after takeover under one current fence.

        Model ``STARTED`` attempts become explicit ``UNKNOWN`` (their budget
        reservation is untouched). A RUNNING Step is replayed only when no
        pending approval or unresolved Tool outcome binds it. The method does
        not mutate Tool or Approval rows; those remain their existing owners.
        """
        async with self.database.transaction() as session:
            if self.run_control is not None:
                await self.run_control.assert_current_in_transaction(session, lease)
            root = (await session.execute(select(RuntimeRunExecutionRow).where(RuntimeRunExecutionRow.run_id == lease.run_id).with_for_update())).scalar_one_or_none()
            if root is None:
                raise ValueError("unknown execution root")
            models = tuple((await session.execute(select(RuntimeModelInvocationRow).where(RuntimeModelInvocationRow.run_id == lease.run_id).with_for_update())).scalars().all())
            for model in models:
                if model.state == ModelAttemptState.STARTED.value:
                    model.state = ModelAttemptState.UNKNOWN.value
                    model.completed_at = func.now()
                    model.version += 1
                    model.last_committed_fencing_token = lease.fencing_token
                    model.updated_at = func.now()
            steps = tuple((await session.execute(select(RuntimeStepExecutionRow).where(RuntimeStepExecutionRow.run_id == lease.run_id).with_for_update())).scalars().all())
            tools = tuple((await session.execute(select(DurableToolInvocationRow).where(DurableToolInvocationRow.run_id == lease.run_id))).scalars().all())
            approvals = tuple((await session.execute(select(DurableApprovalRow).where(DurableApprovalRow.run_id == lease.run_id))).scalars().all())
            for step in steps:
                if step.status != StepExecutionStatus.RUNNING.value:
                    continue
                # Count UNKNOWN attempts across the whole Step, not only the
                # latest attempt; the second crash must fail closed.
                step_models = tuple(model for model in models if model.step_id == step.step_id)
                step_tools = tuple(tool for tool in tools if tool.step_id == step.step_id)
                unresolved_tool = any(tool.state in {"STARTED", "UNKNOWN"} for tool in step_tools)
                pending_approval = any(approval.step_id == step.step_id and approval.state == "PENDING" for approval in approvals)
                unknown_count = sum(model.state == ModelAttemptState.UNKNOWN.value for model in step_models)
                if unresolved_tool or unknown_count >= 2:
                    step.status = StepExecutionStatus.BLOCKED.value
                    step.safe_error = "TOOL_OUTCOME_UNKNOWN" if unresolved_tool else "MODEL_RECOVERY_EXHAUSTED"
                elif pending_approval:
                    # Approval waiting is an already-started step and must not
                    # issue a second approval request.
                    continue
                else:
                    step.status = StepExecutionStatus.PENDING.value
                step.version += 1
                step.last_committed_fencing_token = lease.fencing_token
                step.updated_at = func.now()
            await session.flush()
            # Re-read through the same transaction snapshot after classification.
            return await self._load_in_session(session, lease.run_id)

    async def _load_in_session(self, session: AsyncSession, run_id: str) -> RecoveryImage:
        root = (await session.execute(select(RuntimeRunExecutionRow).where(RuntimeRunExecutionRow.run_id == run_id))).scalar_one()
        steps = tuple((await session.execute(select(RuntimeStepExecutionRow).where(RuntimeStepExecutionRow.run_id == run_id).order_by(RuntimeStepExecutionRow.step_id))).scalars().all())
        models = tuple((await session.execute(select(RuntimeModelInvocationRow).where(RuntimeModelInvocationRow.run_id == run_id).order_by(RuntimeModelInvocationRow.step_id, RuntimeModelInvocationRow.attempt_number, RuntimeModelInvocationRow.model_attempt_number))).scalars().all())
        tools = tuple((await session.execute(select(DurableToolInvocationRow).where(DurableToolInvocationRow.run_id == run_id).order_by(DurableToolInvocationRow.step_id, DurableToolInvocationRow.invocation_id))).scalars().all())
        approvals = tuple((await session.execute(select(DurableApprovalRow).where(DurableApprovalRow.run_id == run_id).order_by(DurableApprovalRow.step_id, DurableApprovalRow.approval_id))).scalars().all())
        claims = tuple((await session.execute(select(DurableToolExecutionClaimRow).where(DurableToolExecutionClaimRow.run_id == run_id).order_by(DurableToolExecutionClaimRow.approval_id))).scalars().all())
        return RecoveryImage(root, steps, models, tools, approvals, claims)

    async def complete_step(self, lease: RunLease, *, step_id: str, plan_version: int, attempt: int, result: Mapping[str, Any], status: StepExecutionStatus = StepExecutionStatus.SUCCEEDED, safe_error: str | None = None, journal_append: JournalAppend | None = None) -> bool:
        if status not in {StepExecutionStatus.SUCCEEDED, StepExecutionStatus.FAILED, StepExecutionStatus.CANCELLED, StepExecutionStatus.BLOCKED, StepExecutionStatus.SKIPPED}:
            raise ValueError("complete_step requires a terminal status")
        normalized_result = to_primitive(dict(result))
        digest = canonical_payload_digest(normalized_result)
        async with self.database.transaction() as session:
            if self.run_control is not None:
                await self.run_control.assert_current_in_transaction(session, lease)
            row = (await session.execute(select(RuntimeStepExecutionRow).where(
                RuntimeStepExecutionRow.run_id == lease.run_id,
                RuntimeStepExecutionRow.step_id == step_id,
                RuntimeStepExecutionRow.plan_version == plan_version,
            ).with_for_update())).scalar_one_or_none()
            if row is None:
                raise ValueError("unknown durable step")
            terminal = {item.value for item in (StepExecutionStatus.SUCCEEDED, StepExecutionStatus.FAILED, StepExecutionStatus.CANCELLED, StepExecutionStatus.BLOCKED, StepExecutionStatus.SKIPPED)}
            if row.status in terminal:
                if row.result_digest == digest and row.current_attempt == attempt:
                    return True
                raise RuntimeError("durable step completion conflict")
            if row.status != StepExecutionStatus.RUNNING.value:
                raise RuntimeError("durable step is not running")
            if row.current_attempt != attempt or row.version <= 0:
                raise RuntimeError("stale step attempt")
            row.status = status.value
            row.typed_result_payload = normalized_result
            row.result_digest = digest
            row.safe_error = safe_error
            row.version += 1
            row.last_committed_fencing_token = lease.fencing_token
            row.updated_at = func.now()
            if journal_append is not None:
                if not callable(journal_append):
                    raise TypeError("journal_append must be callable")
                await journal_append(session)
            await session.flush()
            return True

    async def start_model_attempt(self, lease: RunLease, *, step_id: str, attempt: int, model_attempt: int, request_digest: str, provider_kind: str, profile_identity: str, journal_append: JournalAppend | None = None) -> None:
        """Fenced NOT_STARTED -> STARTED transition before provider I/O."""
        async with self.database.transaction() as session:
            if self.run_control is not None:
                await self.run_control.assert_current_in_transaction(session, lease)
            row = (await session.execute(select(RuntimeModelInvocationRow).where(
                RuntimeModelInvocationRow.run_id == lease.run_id,
                RuntimeModelInvocationRow.step_id == step_id,
                RuntimeModelInvocationRow.attempt_number == attempt,
                RuntimeModelInvocationRow.model_attempt_number == model_attempt,
            ).with_for_update())).scalar_one_or_none()
            if row is None:
                row = RuntimeModelInvocationRow(
                    run_id=lease.run_id, step_id=step_id, attempt_number=attempt,
                    model_attempt_number=model_attempt, request_digest=request_digest,
                    provider_kind=provider_kind, profile_identity=profile_identity,
                    state=ModelAttemptState.STARTED.value, started_at=func.now(),
                    last_committed_fencing_token=lease.fencing_token,
                )
                session.add(row)
            elif row.state == ModelAttemptState.NOT_STARTED.value:
                row.state = ModelAttemptState.STARTED.value
                row.started_at = func.now()
                row.version += 1
                row.last_committed_fencing_token = lease.fencing_token
            else:
                raise RuntimeError("model attempt is not startable")
            if journal_append is not None:
                if not callable(journal_append):
                    raise TypeError("journal_append must be callable")
                await journal_append(session)
            await session.flush()

    async def finish_model_attempt(self, lease: RunLease, *, step_id: str, attempt: int, model_attempt: int, state: ModelAttemptState, result: Mapping[str, Any] | None = None, usage: Mapping[str, Any] | None = None, cost: Mapping[str, Any] | None = None, safe_error: str | None = None, journal_append: JournalAppend | None = None) -> None:
        """Fenced STARTED -> COMPLETED/UNKNOWN; no provider lookup."""
        if state not in {ModelAttemptState.COMPLETED, ModelAttemptState.UNKNOWN}:
            raise ValueError("model finish state must be COMPLETED or UNKNOWN")
        if state is ModelAttemptState.UNKNOWN and result is not None:
            raise ValueError("UNKNOWN model attempt cannot carry a result")
        normalized_result = to_primitive(dict(result)) if result is not None else None
        digest = canonical_payload_digest(normalized_result) if normalized_result is not None else None
        async with self.database.transaction() as session:
            if self.run_control is not None:
                await self.run_control.assert_current_in_transaction(session, lease)
            row = (await session.execute(select(RuntimeModelInvocationRow).where(
                RuntimeModelInvocationRow.run_id == lease.run_id,
                RuntimeModelInvocationRow.step_id == step_id,
                RuntimeModelInvocationRow.attempt_number == attempt,
                RuntimeModelInvocationRow.model_attempt_number == model_attempt,
            ).with_for_update())).scalar_one_or_none()
            if row is None or row.state != ModelAttemptState.STARTED.value:
                raise RuntimeError("model attempt transition rejected")
            row.state = state.value
            row.result_binding = normalized_result
            row.result_digest = digest
            row.usage_evidence = to_primitive(dict(usage or {}))
            row.cost_evidence = to_primitive(dict(cost or {}))
            row.safe_error = safe_error
            row.completed_at = func.now()
            row.version += 1
            row.last_committed_fencing_token = lease.fencing_token
            row.updated_at = func.now()
            if journal_append is not None:
                if not callable(journal_append):
                    raise TypeError("journal_append must be callable")
                await journal_append(session)
            await session.flush()

    async def finalize_terminal(self, lease: RunLease, *, event: Any, journal: Any, client_event_feed: Any = None, final_result_binding: Mapping[str, Any] | None = None) -> Any:
        """Atomically update root, append terminal evidence and close control."""
        if self.run_control is None:
            raise RuntimeError("run_control is required for atomic terminal")
        from core.runtime.events import RuntimeEvent, RuntimeEventType
        if not isinstance(event, RuntimeEvent) or event.run_id != lease.run_id:
            raise TypeError("event must be a RuntimeEvent for the leased run")
        if event.event_type is not RuntimeEventType.RUN_COMPLETED:
            raise ValueError("atomic terminal requires RUN_COMPLETED event")
        append_in_transaction = getattr(journal, "append_in_transaction", None)
        if not callable(append_in_transaction):
            raise TypeError("journal must support append_in_transaction")
        projected = False
        async with self.database.transaction() as session:
            await self.run_control.assert_current_in_transaction(session, lease)
            root = (await session.execute(select(RuntimeRunExecutionRow).where(RuntimeRunExecutionRow.run_id == lease.run_id).with_for_update())).scalar_one_or_none()
            if root is None:
                raise ValueError("unknown execution root")
            if root.status != ExecutionStatus.ACTIVE.value:
                raise RuntimeError("execution root is already terminal")
            try:
                status = ExecutionStatus(str(event.payload.status))
            except ValueError as exc:
                raise ValueError("terminal event has unsupported execution status") from exc
            append_status = await append_in_transaction(session, event)
            if client_event_feed is not None:
                append_projection = getattr(client_event_feed, "append_event_in_transaction", None)
                if not callable(append_projection):
                    raise TypeError("client_event_feed must support append_event_in_transaction")
                projected = await append_projection(session, event)
            root.status = status.value
            root.stop_reason = str(event.payload.stop_reason)
            root.final_result_binding = to_primitive(dict(final_result_binding)) if final_result_binding is not None else None
            root.execution_version += 1
            root.last_committed_fencing_token = lease.fencing_token
            root.updated_at = func.now()
            close = getattr(self.run_control, "_close_terminal_locked", None)
            if not callable(close):
                raise RuntimeError("run_control does not expose its terminal close owner")
            control = (await session.execute(select(RunControlRow).where(RunControlRow.run_id == lease.run_id).with_for_update())).scalar_one_or_none()
            if control is None:
                raise RuntimeError("unknown Run control row")
            await close(session, control, event.sequence)
        if projected and client_event_feed is not None:
            record_write_succeeded = getattr(client_event_feed, "record_write_succeeded", None)
            if callable(record_write_succeeded):
                record_write_succeeded()
        return append_status

    async def record_recovery_failure(self, run_id: str, error_code: str, *, max_attempts: int, initial_seconds: int = 5, max_seconds: int = 60) -> bool:
        if not error_code or max_attempts <= 0 or initial_seconds <= 0 or max_seconds < initial_seconds:
            raise ValueError("invalid recovery backoff configuration")
        async with self.database.transaction() as session:
            row = (await session.execute(select(RuntimeRunExecutionRow).where(RuntimeRunExecutionRow.run_id == run_id).with_for_update())).scalar_one_or_none()
            if row is None:
                return False
            row.recovery_attempt_count += 1
            row.last_recovery_error = error_code[:128]
            row.last_recovery_at = func.now()
            if row.recovery_attempt_count >= max_attempts:
                row.manual_required = True
                row.next_recovery_at = None
            else:
                seconds = min(max_seconds, initial_seconds * (2 ** max(0, row.recovery_attempt_count - 1)))
                row.next_recovery_at = func.now() + timedelta(seconds=seconds)
            row.updated_at = func.now()
            return True


__all__ = ["DurableExecutionRepository", "RecoveryImage"]
