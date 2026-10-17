"""Narrow repository for the Stage10 canonical execution aggregate."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from dataclasses import asdict, is_dataclass
from uuid import NAMESPACE_URL, uuid5
import hashlib
from typing import Any, Awaitable, Callable, Mapping

from sqlalchemy import and_, exists, func, or_, select
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
    RuntimeLoopFrameRow,
    RuntimeLoopIterationRow,
    RuntimeContextSnapshotRow,
    RuntimeContextOperationRow,
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
    loop_frames: tuple[RuntimeLoopFrameRow, ...] = ()
    loop_iterations: tuple[RuntimeLoopIterationRow, ...] = ()
    terminal_sequence: int | None = None
    context_snapshots: tuple = ()
    context_operations: tuple = ()

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

            if values.get("execution_policy") in {"durable-agent-loop.v1", "durable-agent-loop.v2"}:
                loop = root.resume_input.get("agent_loop")
                if not isinstance(loop, Mapping) or len(root.plan.steps) != 1:
                    raise ValueError("LOOP_ADMISSION_INVALID")
                seed = to_primitive(dict(loop["context_seed"]))
                config = to_primitive(dict(loop["pinned_config"]))
                policy = to_primitive(dict(loop["loop_policy"]))
                maximum = policy.get("max_iterations", 16)
                if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 64:
                    raise ValueError("LOOP_ADMISSION_INVALID")
                step_id = root.plan.steps[0].step_id
                session.add(RuntimeLoopFrameRow(
                    frame_id=str(uuid5(NAMESPACE_URL, f"{root.run_id}:{step_id}:agent-loop")),
                    run_id=root.run_id, step_id=step_id, context_seed=seed,
                    schema_version=2 if values["execution_policy"].endswith(".v2") else 1,
                    decision_schema_version=2 if values["execution_policy"].endswith(".v2") else 1,
                    context_seed_version=2 if values["execution_policy"].endswith(".v2") else 1,
                    execution_policy=values["execution_policy"], execution_agent_id=root.plan.steps[0].preferred_agent,
                    seed_digest=canonical_payload_digest(seed), pinned_config=config,
                    config_digest=canonical_payload_digest(config), loop_policy=policy,
                    context_policy=config.get("context_policy"),
                    last_committed_fencing_token=token,
                ))
                await session.flush()
                if lease is not None:
                    await self._memory_admission(session, lease, seed_digest=canonical_payload_digest(seed))


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
            frames = tuple((await db.execute(select(RuntimeLoopFrameRow).where(RuntimeLoopFrameRow.run_id == run_id))).scalars().all())
            iterations = tuple((await db.execute(select(RuntimeLoopIterationRow).join(RuntimeLoopFrameRow, RuntimeLoopIterationRow.frame_id == RuntimeLoopFrameRow.frame_id).where(RuntimeLoopFrameRow.run_id == run_id).order_by(RuntimeLoopIterationRow.iteration_no))).scalars().all())
            control = await db.get(RunControlRow, run_id)
            snapshots = tuple((await db.execute(select(RuntimeContextSnapshotRow).where(RuntimeContextSnapshotRow.run_id == run_id).order_by(RuntimeContextSnapshotRow.version))).scalars().all())
            operations = tuple((await db.execute(select(RuntimeContextOperationRow).join(RuntimeLoopFrameRow, RuntimeContextOperationRow.frame_id == RuntimeLoopFrameRow.frame_id).where(RuntimeLoopFrameRow.run_id == run_id).order_by(RuntimeContextOperationRow.target_iteration))).scalars().all())
            return RecoveryImage(root, steps, models, tools, approvals, claims, frames, iterations, control.terminal_sequence if control else None, snapshots, operations)
        if session is not None:
            return await read(session)
        # A plain session gives each SELECT a separate READ COMMITTED snapshot.
        async with self.database.transaction() as db:
            return await read(db)

    async def stale_run_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be positive")
        async with self.database.session() as session:
            waiting_loop = exists(select(1).where(RuntimeLoopFrameRow.run_id == RuntimeRunExecutionRow.run_id, RuntimeLoopFrameRow.phase.in_(("WAIT_TOOL", "WAIT_ACTIONS", "WAIT_CHILD"))))
            pending_approval = exists(select(1).where(DurableApprovalRow.run_id == RuntimeRunExecutionRow.run_id, DurableApprovalRow.state == "PENDING"))
            unresolved_tool = exists(select(1).where(DurableToolInvocationRow.run_id == RuntimeRunExecutionRow.run_id, DurableToolInvocationRow.state.in_(("STARTED", "UNKNOWN"))))
            ready_tool = exists(select(1).where(DurableToolInvocationRow.run_id == RuntimeRunExecutionRow.run_id, DurableToolInvocationRow.state.in_(("PREPARED", "COMMITTED", "NOT_COMMITTED"))))
            decided_approval = exists(select(1).where(DurableApprovalRow.run_id == RuntimeRunExecutionRow.run_id, DurableApprovalRow.state != "PENDING"))
            # WAIT_TOOL 本身不构成执行许可；仅已确认结果/审批裁决或控制命令唤醒。
            loop_ready = or_(~waiting_loop, and_(~pending_approval, ~unresolved_tool, or_(ready_tool, decided_approval)))
            result = await session.execute(
                select(RuntimeRunExecutionRow.run_id)
                .join(RunControlRow, RunControlRow.run_id == RuntimeRunExecutionRow.run_id)
                .where(
                    RunControlRow.state == "ACTIVE",
                    or_(RunControlRow.lease_until.is_(None), RunControlRow.lease_until <= func.now()),
                    RuntimeRunExecutionRow.status.notin_(("SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED")),
                    RuntimeRunExecutionRow.recovery_supported.is_(True),
                    or_(
                        and_(RuntimeRunExecutionRow.manual_required.is_(False), or_(RuntimeRunExecutionRow.next_recovery_at.is_(None), RuntimeRunExecutionRow.next_recovery_at <= func.now()), or_(RuntimeRunExecutionRow.execution_policy.notin_(("durable-agent-loop.v1", "durable-agent-loop.v2")),
                            and_(RuntimeRunExecutionRow.execution_policy == "durable-agent-loop.v1", loop_ready),
                            and_(RuntimeRunExecutionRow.execution_policy == "durable-agent-loop.v2", ~pending_approval, ~unresolved_tool))),
                        and_(RuntimeRunExecutionRow.execution_policy.in_(("durable-agent-loop.v1", "durable-agent-loop.v2")), or_(RunControlRow.cancel_requested_at.is_not(None), RuntimeRunExecutionRow.absolute_deadline <= func.now())),
                    ),
                    ~exists(select(1).where(
                        RuntimeEventJournalRow.run_id == RuntimeRunExecutionRow.run_id,
                        RuntimeEventJournalRow.event_type == "RUN_COMPLETED",
                    )),
                ).order_by(RuntimeRunExecutionRow.updated_at).limit(limit)
            )
            return tuple(result.scalars().all())

    async def reconciliation_candidates(self, *, limit: int = 20) -> tuple[DurableToolInvocationRow, ...]:
        """返回到期且未人工接管的 STARTED/UNKNOWN Tool invocation。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be positive")
        async with self.database.session() as session:
            result = await session.execute(
                select(DurableToolInvocationRow)
                .join(RunControlRow, RunControlRow.run_id == DurableToolInvocationRow.run_id)
                .join(RuntimeRunExecutionRow, RuntimeRunExecutionRow.run_id == DurableToolInvocationRow.run_id)
                .where(
                    DurableToolInvocationRow.state.in_(("STARTED", "UNKNOWN")),
                    DurableToolInvocationRow.manual_required.is_(False),
                    or_(DurableToolInvocationRow.next_reconcile_at.is_(None), DurableToolInvocationRow.next_reconcile_at <= func.now()),
                    RunControlRow.state == "ACTIVE",
                    RuntimeRunExecutionRow.status.notin_(("SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED")),
                    or_(RunControlRow.lease_until.is_(None), RunControlRow.lease_until <= func.now()),
                )
                .order_by(DurableToolInvocationRow.created_at)
                .limit(limit)
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
            if root.execution_policy == "durable-agent-loop.v2":
                from .agent_loop import recover_v2
                return await recover_v2(self, session, lease, root)
            if root.execution_policy == "durable-agent-loop.v1":
                frames = tuple((await session.execute(select(RuntimeLoopFrameRow).where(RuntimeLoopFrameRow.run_id == lease.run_id).with_for_update())).scalars().all())
                if len(frames) != 1:
                    raise RuntimeError("LOOP_FRAME_MISSING")
                frame = frames[0]
                self._validate_loop_frame(frame)
                if frame.last_committed_fencing_token == lease.fencing_token:
                    return await self._load_in_session(session, lease.run_id)
                frame.resume_generation += 1
                self._touch_loop(frame, lease)
                self._settle_recovery_budget(root, lease)
                iterations = tuple((await session.execute(select(RuntimeLoopIterationRow).where(RuntimeLoopIterationRow.frame_id == frame.frame_id).order_by(RuntimeLoopIterationRow.iteration_no).with_for_update())).scalars().all())
                models = tuple((await session.execute(select(RuntimeModelInvocationRow).where(RuntimeModelInvocationRow.run_id == lease.run_id).with_for_update())).scalars().all())
                abandoned_iterations = set()
                for model in models:
                    if model.state == ModelAttemptState.STARTED.value:
                        if model.purpose == "DECISION":
                            abandoned_iterations.add(model.loop_iteration_no)
                        model.state = ModelAttemptState.UNKNOWN.value
                        model.completed_at = func.now()
                        model.version += 1
                        model.last_committed_fencing_token = lease.fencing_token
                for iteration in iterations:
                    if iteration.iteration_no in abandoned_iterations:
                        iteration.model_recovery_count += 1
                current = next((row for row in iterations if row.iteration_no == frame.current_iteration), None)
                if current is not None and current.state == "OPEN" and current.model_recovery_count >= 2:
                    frame.phase = "STOPPED"
                    frame.stop_code = "LOOP_MODEL_RECOVERY_EXHAUSTED"
                await session.flush()
                return await self._load_in_session(session, lease.run_id)
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
        image = await self.load(run_id, session=session)
        if image is None:
            raise RuntimeError("unknown execution root")
        return image

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

    async def _memory_admission(self, session, lease, admission=None, seed_digest=None):
        from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
        from core.runtime.memory_scope import MemoryContextError
        code = await MemoryLifecycleRepository(self.database).guard_in_transaction(
            session, lease, admission=admission, seed_digest=seed_digest)
        if code:
            raise MemoryContextError(code)

    async def start_model_attempt(self, lease: RunLease, *, step_id: str, attempt: int, model_attempt: int, request_digest: str, provider_kind: str, profile_identity: str, journal_append: JournalAppend | None = None, loop_frame_id: str | None = None, loop_iteration_no: int | None = None, resume_generation: int | None = None, invocation_group: str | None = None, operation_id: str | None = None, reserved_usage: Any = None, purpose: str = "DECISION", context_snapshot_id: str | None = None, request_assessment: dict | None = None, compaction_operation_id: str | None = None, memory_operation_id: str | None = None) -> None:
        """Fenced NOT_STARTED -> STARTED transition before provider I/O."""
        async with self.database.transaction() as session:
            if self.run_control is not None:
                await self.run_control.assert_current_in_transaction(session, lease)
            if loop_frame_id is not None:
                _, frame = await self._lock_loop(session, lease, loop_frame_id, resume_generation=resume_generation)
                if frame.frame_role == "CHILD" and purpose == "FORMATION":
                    raise RuntimeError("DELEGATION_FORMATION_DENIED")
                iteration = await session.get(RuntimeLoopIterationRow, (loop_frame_id, loop_iteration_no), with_for_update=True) if loop_iteration_no is not None else None
                if frame.step_id != step_id or frame.phase != ("RESULT_READY" if purpose == "FORMATION" else "NEED_DECISION"):
                    raise RuntimeError("LOOP_MODEL_BINDING_INVALID")
                if purpose == "COMPACTION":
                    operation = await session.get(RuntimeContextOperationRow, compaction_operation_id, with_for_update=True)
                    if (operation is None or operation.frame_id != frame.frame_id or operation.state != "PREPARED"
                            or operation.attempt_count >= frame.context_policy["max_attempts"]
                            or invocation_group != f"context:{compaction_operation_id}"):
                        raise RuntimeError("CONTEXT_OPERATION_EXHAUSTED")
                    completed = (await session.execute(select(RuntimeModelInvocationRow).where(
                        RuntimeModelInvocationRow.compaction_operation_id == compaction_operation_id,
                        RuntimeModelInvocationRow.state.in_(["STARTED", "COMPLETED"])))).scalars().first()
                    if completed is not None:
                        raise RuntimeError("CONTEXT_OPERATION_ALREADY_STARTED")
                    operation.attempt_count += 1
                elif purpose == "FORMATION":
                    pass
                elif iteration is None or iteration.state != "OPEN":
                    raise RuntimeError("LOOP_MODEL_BINDING_INVALID")
                elif invocation_group != f"{loop_frame_id}:{loop_iteration_no}":
                    raise RuntimeError("LOOP_MODEL_BINDING_INVALID")
                if frame.context_policy and purpose != "FORMATION":
                    from core.runtime.agent_loop.context import ContextSnapshotV1
                    if not request_assessment or not request_assessment.get("admissible") or request_assessment.get("request_digest") != request_digest:
                        raise RuntimeError("CONTEXT_BUDGET_UNCERTAIN")
                    if purpose == "DECISION":
                        snapshot = await session.get(RuntimeContextSnapshotRow, context_snapshot_id)
                        if (snapshot is None or snapshot.snapshot_id != iteration.context_snapshot_id
                                or snapshot.snapshot_digest != iteration.context_snapshot_digest
                                or snapshot.snapshot_digest != ContextSnapshotV1.create(snapshot.payload).digest
                                or snapshot.payload["messages_digest"] != request_assessment.get("messages_digest")):
                            raise RuntimeError("CONTEXT_SNAPSHOT_UNAVAILABLE")
                        if (snapshot.payload["kind"] == "COMPACTED" and request_assessment["required_window"] * 100 > request_assessment["window"] * frame.context_policy["target_percent"]):
                            raise RuntimeError("CONTEXT_VALIDATION_FAILED")
                if operation_id is None or reserved_usage is None:
                    raise ValueError("Loop model requires budget reservation")
                await self.reserve_budget_operation_in_transaction(session, lease, operation_id=operation_id, usage=reserved_usage, kind="MODEL", resume_generation=resume_generation, frame_id=loop_frame_id)
            if loop_frame_id is None:
                await session.get(RuntimeRunExecutionRow, lease.run_id, with_for_update=True)
            await self._memory_admission(session, lease, f"model:{step_id}:{attempt}:{model_attempt}")
            if purpose == "FORMATION":
                from core.persistence.models import MemoryOperationRow, RuntimeMemoryBindingRow
                from core.persistence.repositories.memory_lifecycle import MemoryLifecycleRepository
                from core.runtime.memory_scope import MemoryAccessContext, MemoryContextError
                operation = await session.get(MemoryOperationRow, memory_operation_id)
                binding = await session.get(RuntimeMemoryBindingRow, lease.run_id)
                if operation is None or operation.state != "PREPARED" or binding is None or binding.mode != "SCOPED":
                    raise MemoryContextError("MEMORY_SOURCE_INVALID")
                memory = MemoryLifecycleRepository(self.database)
                await memory._locks(session, [operation.scope_id, *(d["scope_id"] for d in operation.dependencies)])
                await memory._scope(session, MemoryAccessContext.from_dict(binding.access), operation.scope_id, "WRITE")
                await memory._dependencies(session, operation.dependencies)
                from core.persistence.models import MessageExchangeRow
                source = await session.scalar(select(MessageExchangeRow.exchange_id).where(
                    MessageExchangeRow.source_identity == operation.source_identity,
                    MessageExchangeRow.access_scope_id == MemoryAccessContext.from_dict(binding.access).scope_id("SESSION"),
                    MessageExchangeRow.run_id == lease.run_id))
                if source is None:
                    raise MemoryContextError("MEMORY_SOURCE_INVALID")
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
                    loop_frame_id=loop_frame_id, loop_iteration_no=loop_iteration_no,
                    resume_generation=resume_generation, invocation_group=invocation_group,
                    purpose=purpose, compaction_operation_id=compaction_operation_id, memory_operation_id=memory_operation_id,
                    context_snapshot_id=context_snapshot_id,
                    context_snapshot_version=snapshot.version if context_snapshot_id else None,
                    context_snapshot_digest=snapshot.snapshot_digest if context_snapshot_id else None,
                    request_assessment=request_assessment,
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

    async def finish_model_attempt(self, lease: RunLease, *, step_id: str, attempt: int, model_attempt: int, state: ModelAttemptState, result: Mapping[str, Any] | None = None, usage: Mapping[str, Any] | None = None, cost: Mapping[str, Any] | None = None, safe_error: str | None = None, journal_append: JournalAppend | None = None, resume_generation: int | None = None, operation_id: str | None = None, actual_usage: Any = None, usage_source: str = "ACTUAL", loop_frame_id: str | None = None) -> None:
        """Fenced STARTED -> COMPLETED/UNKNOWN; no provider lookup."""
        if state not in {ModelAttemptState.COMPLETED, ModelAttemptState.UNKNOWN, ModelAttemptState.REJECTED}:
            raise ValueError("model finish state must be COMPLETED or UNKNOWN")
        if state in {ModelAttemptState.UNKNOWN, ModelAttemptState.REJECTED} and result is not None:
            raise ValueError("UNKNOWN model attempt cannot carry a result")
        normalized_result = to_primitive(dict(result)) if result is not None else None
        digest = canonical_payload_digest(normalized_result) if normalized_result is not None else None
        async with self.database.transaction() as session:
            if self.run_control is not None:
                await self.run_control.assert_current_in_transaction(session, lease)
            if loop_frame_id is None and resume_generation is not None:
                loop_frame_id = (await session.execute(select(RuntimeModelInvocationRow.loop_frame_id).where(RuntimeModelInvocationRow.run_id == lease.run_id, RuntimeModelInvocationRow.step_id == step_id, RuntimeModelInvocationRow.attempt_number == attempt, RuntimeModelInvocationRow.model_attempt_number == model_attempt))).scalar_one()
            if loop_frame_id is not None:
                root, frame = await self._lock_loop(session, lease, loop_frame_id, resume_generation=resume_generation)
                if operation_id is None:
                    raise ValueError("Loop model requires budget operation")
                try:
                    await self.settle_budget_operation_in_transaction(session, lease, operation_id=operation_id, usage=actual_usage, source=usage_source, resume_generation=resume_generation, frame_id=loop_frame_id)
                except RuntimeError as exc:
                    if not frame.context_policy or not str(exc).startswith("LOOP_BUDGET_EXHAUSTED:"):
                        raise
                    # 原额度仍保守结算；reported actual 保留在原 Attempt，禁止恢复成 winner。
                    dimension = str(exc).rsplit(":", 1)[1]
                    await self.settle_budget_operation_in_transaction(session, lease, operation_id=operation_id, usage=None, source="ESTIMATED", resume_generation=resume_generation, frame_id=loop_frame_id)
                    state, normalized_result, digest = ModelAttemptState.UNKNOWN, None, None
                    safe_error = "BUDGET_EXHAUSTED"
                    cost = {"actual_exceeded_budget": True}
                    frame.phase, frame.stop_code = "STOPPED", "LOOP_BUDGET_EXHAUSTED"
                    frame.stop_details = {"budget_dimension": dimension}
                    self._touch_loop(frame, lease)
            row = (await session.execute(select(RuntimeModelInvocationRow).where(
                RuntimeModelInvocationRow.run_id == lease.run_id,
                RuntimeModelInvocationRow.step_id == step_id,
                RuntimeModelInvocationRow.attempt_number == attempt,
                RuntimeModelInvocationRow.model_attempt_number == model_attempt,
            ).with_for_update())).scalar_one_or_none()
            if row is None or row.state != ModelAttemptState.STARTED.value:
                raise RuntimeError("model attempt transition rejected")
            if state is ModelAttemptState.REJECTED and (row.request_assessment is None or safe_error != "PROVIDER_CONTEXT_REJECTED"):
                raise RuntimeError("CONTEXT_REJECTION_UNCONFIRMED")
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
            if root.execution_policy == "durable-agent-loop.v2":
                operations = dict(root.budget_operations)
                for key, allocation in operations.items():
                    if allocation["kind"] == "DELEGATION_ALLOCATION" and allocation["state"] == "RESERVED":
                        root.budget_reserved = self._budget_add(root.budget_reserved, allocation["unspent"], -1)
                        operations[key] = {**allocation, "state": "RELEASED", "released": allocation["unspent"],
                            "unspent": {}, "release_source": "ROOT_TERMINAL"}
                root.budget_operations = operations
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
            committed = getattr(client_event_feed, "projection_committed", None)
            if callable(committed):
                committed(event)
            record_write_succeeded = getattr(client_event_feed, "record_write_succeeded", None)
            if callable(record_write_succeeded):
                record_write_succeeded()
        return append_status

    async def _lock_loop(self, session: AsyncSession, lease: RunLease, frame_id: str, *, resume_generation: int | None = None, row_version: int | None = None, allow_control_stop: bool = False) -> tuple[RuntimeRunExecutionRow, RuntimeLoopFrameRow]:
        if self.run_control is None:
            raise RuntimeError("Loop requires durable Run control")
        await self.run_control.assert_current_in_transaction(session, lease)
        root = (await session.execute(select(RuntimeRunExecutionRow).where(RuntimeRunExecutionRow.run_id == lease.run_id).with_for_update())).scalar_one()
        frame = (await session.execute(select(RuntimeLoopFrameRow).where(RuntimeLoopFrameRow.run_id == lease.run_id, RuntimeLoopFrameRow.frame_id == frame_id).with_for_update())).scalar_one()
        self._validate_loop_frame(frame)
        if root.execution_policy != frame.execution_policy or root.status != "ACTIVE":
            raise RuntimeError("LOOP_STATE_INVALID")
        if resume_generation is not None and frame.resume_generation != resume_generation:
            raise RuntimeError("LOOP_STALE_GENERATION")
        if row_version is not None and frame.row_version != row_version:
            raise RuntimeError("LOOP_CAS_CONFLICT")
        if not allow_control_stop:
            control = await session.get(RunControlRow, lease.run_id)
            if control.cancel_requested_at is not None:
                from core.runtime.cancellation import RunCancelledError
                raise RunCancelledError(control.cancel_reason or "REQUEST_CANCELLED")
            if root.absolute_deadline is not None and root.absolute_deadline <= datetime.now(UTC):
                from core.runtime.context import RunDeadlineExceededError
                raise RunDeadlineExceededError()
        return root, frame

    @staticmethod
    def _validate_loop_frame(frame: RuntimeLoopFrameRow) -> None:
        if (frame.schema_version, frame.decision_schema_version, frame.context_seed_version, frame.execution_policy) not in {(1, 1, 1, "durable-agent-loop.v1"), (2, 2, 2, "durable-agent-loop.v2")}:
            raise RuntimeError("LOOP_VERSION_UNSUPPORTED")
        if canonical_payload_digest(frame.context_seed) != frame.seed_digest or canonical_payload_digest(frame.pinned_config) != frame.config_digest:
            raise RuntimeError("LOOP_PINNED_CONFIG_UNAVAILABLE")

    @staticmethod
    def _touch_loop(frame: RuntimeLoopFrameRow, lease: RunLease) -> None:
        frame.row_version += 1
        frame.last_committed_fencing_token = lease.fencing_token
        frame.updated_at = func.now()

    @staticmethod
    def _check_loop_version(frame: RuntimeLoopFrameRow, row_version: int | None) -> None:
        if row_version is not None and frame.row_version != row_version:
            raise RuntimeError("LOOP_CAS_CONFLICT")

    async def load_loop(self, lease: RunLease, step_id: str, frame_id: str | None = None) -> tuple[RuntimeLoopFrameRow, tuple[RuntimeLoopIterationRow, ...]]:
        async with self.database.transaction() as session:
            if self.run_control is not None:
                await self.run_control.assert_current_in_transaction(session, lease)
            frame = (await session.execute(select(RuntimeLoopFrameRow).where(RuntimeLoopFrameRow.run_id == lease.run_id, RuntimeLoopFrameRow.step_id == step_id, RuntimeLoopFrameRow.frame_id == frame_id if frame_id else RuntimeLoopFrameRow.frame_role == "ROOT"))).scalar_one()
            self._validate_loop_frame(frame)
            rows = tuple((await session.execute(select(RuntimeLoopIterationRow).where(RuntimeLoopIterationRow.frame_id == frame.frame_id).order_by(RuntimeLoopIterationRow.iteration_no))).scalars().all())
            return frame, rows

    async def accept_loop_batch(self, lease, frame_id, iteration_no, **kwargs):
        from .agent_loop import accept_batch
        return await accept_batch(self, lease, frame_id, iteration_no, **kwargs)

    async def load_loop_actions(self, lease, frame_id, iteration_no):
        from .agent_loop import load_actions
        return await load_actions(self, lease, frame_id, iteration_no)

    async def record_loop_action(self, lease, frame_id, iteration_no, **kwargs):
        from .agent_loop import record_action
        return await record_action(self, lease, frame_id, iteration_no, **kwargs)

    async def commit_loop_batch(self, lease, frame_id, iteration_no, **kwargs):
        from .agent_loop import commit_batch
        return await commit_batch(self, lease, frame_id, iteration_no, **kwargs)

    async def load_delegation(self, lease, parent_frame_id, iteration_no):
        from .agent_delegation import load_delegation
        return await load_delegation(self, lease, parent_frame_id, iteration_no)

    async def commit_child_result(self, lease, frame_id, **kwargs):
        from .agent_delegation import commit_child_result
        return await commit_child_result(self, lease, frame_id, **kwargs)

    async def accept_child_observation(self, lease, parent_frame_id, iteration_no, **kwargs):
        from .agent_delegation import accept_child
        return await accept_child(self, lease, parent_frame_id, iteration_no, **kwargs)

    async def prepare_context_operation(self, lease, frame_id, *, resume_generation, row_version, prepared):
        async with self.database.transaction() as session:
            _, frame = await self._lock_loop(session, lease, frame_id, resume_generation=resume_generation)
            self._check_loop_version(frame, row_version)
            target = prepared["target_iteration"]
            current = await session.get(RuntimeLoopIterationRow, (frame_id, frame.current_iteration), with_for_update=True) if frame.current_iteration else None
            if current and current.state == "OPEN":
                attempts = tuple((await session.execute(select(RuntimeModelInvocationRow).where(
                    RuntimeModelInvocationRow.loop_frame_id == frame_id, RuntimeModelInvocationRow.loop_iteration_no == current.iteration_no,
                    RuntimeModelInvocationRow.context_snapshot_id == current.context_snapshot_id).with_for_update())).scalars().all())
                if target != current.iteration_no or current.context_rebind_count or not attempts or any(m.state != "REJECTED" for m in attempts):
                    raise RuntimeError("CONTEXT_REBIND_FORBIDDEN")
            elif target != frame.current_iteration + 1:
                raise RuntimeError("CONTEXT_OPERATION_CONFLICT")
            existing = (await session.execute(select(RuntimeContextOperationRow).where(
                RuntimeContextOperationRow.frame_id == frame_id, RuntimeContextOperationRow.target_iteration == target).with_for_update())).scalar_one_or_none()
            if existing is not None:
                if existing.prepared != prepared:
                    raise RuntimeError("CONTEXT_OPERATION_CONFLICT")
                return existing
            count = (await session.execute(select(func.count()).select_from(RuntimeContextOperationRow).join(
                RuntimeLoopFrameRow, RuntimeLoopFrameRow.frame_id == RuntimeContextOperationRow.frame_id).where(
                RuntimeLoopFrameRow.run_id == lease.run_id))).scalar_one()
            if count >= frame.context_policy["max_operations"]:
                raise RuntimeError("CONTEXT_OPERATION_EXHAUSTED")
            if (frame.phase != "NEED_DECISION" or prepared["parent_id"] != frame.current_context_snapshot_id
                    or prepared["source_cursor"] != frame.committed_observation_cursor):
                raise RuntimeError("CONTEXT_REJECTED_STALE")
            row = RuntimeContextOperationRow(operation_id=str(uuid5(NAMESPACE_URL, f"context:{frame_id}:{target}")),
                frame_id=frame_id, target_iteration=target, source_cursor=frame.committed_observation_cursor,
                parent_id=frame.current_context_snapshot_id, prepared=prepared, created_generation=resume_generation)
            session.add(row)
            self._touch_loop(frame, lease)
            await session.flush()
            return row

    async def record_context_validation(self, lease, frame_id, *, resume_generation, row_version, operation_id, candidate, validation, reason):
        async with self.database.transaction() as session:
            _, frame = await self._lock_loop(session, lease, frame_id, resume_generation=resume_generation)
            self._check_loop_version(frame, row_version)
            row = await session.get(RuntimeContextOperationRow, operation_id, with_for_update=True)
            if row is None or row.frame_id != frame_id:
                raise RuntimeError("CONTEXT_OPERATION_CONFLICT")
            if row.state != "PREPARED":
                if row.candidate != candidate or row.validation != validation or row.reason != reason:
                    raise RuntimeError("CONTEXT_VALIDATION_CONFLICT")
                return row
            stale = row.parent_id != frame.current_context_snapshot_id or row.source_cursor != frame.committed_observation_cursor
            row.state = "REJECTED" if stale or reason != "VALID" else "RESULT_READY"
            row.reason = "CONTEXT_REJECTED_STALE" if stale else reason
            row.candidate, row.validation = candidate, validation
            row.candidate_digest = canonical_payload_digest(candidate) if candidate is not None else None
            row.validation_digest = canonical_payload_digest(validation) if validation is not None else None
            response = (await session.execute(select(RuntimeModelInvocationRow).where(
                RuntimeModelInvocationRow.compaction_operation_id == operation_id,
                RuntimeModelInvocationRow.purpose == "COMPACTION", RuntimeModelInvocationRow.state == "COMPLETED").with_for_update())).scalar_one_or_none()
            row.result_digest = response.result_digest if response else None
            self._touch_loop(frame, lease)
            await session.flush()
            return row

    async def accept_context_and_open_iteration(self, lease, frame_id, *, resume_generation, row_version, candidate, validation, operation_id=None, rebind=False):
        from core.runtime.agent_loop.context import ContextSnapshotV1, context_sources, snapshot_candidate
        async with self.database.transaction() as session:
            _, frame = await self._lock_loop(session, lease, frame_id, resume_generation=resume_generation)
            rows = tuple((await session.execute(select(RuntimeLoopIterationRow).where(RuntimeLoopIterationRow.frame_id == frame_id).order_by(RuntimeLoopIterationRow.iteration_no).with_for_update())).scalars().all())
            await self._memory_admission(session, lease)
            current = rows[-1] if rows and rows[-1].state == "OPEN" else None
            artifact = ContextSnapshotV1.create(candidate)
            if current and current.context_snapshot_digest == artifact.digest:
                return current
            self._check_loop_version(frame, row_version)
            if not frame.context_policy or frame.phase != "NEED_DECISION":
                raise RuntimeError("CONTEXT_SNAPSHOT_UNAVAILABLE")
            groups = context_sources(frame, rows)
            for row in rows:
                if row.state == "OBSERVED" and frame.schema_version == 1:
                    tool = await session.get(DurableToolInvocationRow, row.tool_invocation_id)
                    if tool is None or tool.state != "COMMITTED" or tool.committed_result_digest != row.observation_source_digest:
                        raise RuntimeError("CONTEXT_VALIDATION_FAILED")
            expected_options = {"tools": [t["schema"] for t in frame.pinned_config["tools"]], "tool_choice": "auto"} if frame.context_seed["protocol_mode"] == "native" else {}
            if frame.schema_version == 2:
                from core.runtime.agent_loop.handoff import loop_options
                expected_options = loop_options(frame)
            if candidate["options"] != expected_options or candidate["output_reserve"] != frame.pinned_config["max_tokens"]:
                raise RuntimeError("CONTEXT_VALIDATION_FAILED")
            expected = snapshot_candidate(frame, groups, candidate["options"], candidate["output_reserve"], summary=candidate["summary"], kind=candidate["kind"])
            if expected.digest != artifact.digest or validation.get("reason") != "VALID" or not all(validation.get(k) is True for k in ("source", "mandatory", "roles", "pairs", "budget")):
                raise RuntimeError("CONTEXT_REJECTED_STALE")
            operation = await session.get(RuntimeContextOperationRow, operation_id, with_for_update=True) if operation_id else None
            if candidate["kind"] == "COMPACTED" and (operation is None or operation.state != "RESULT_READY" or operation.candidate != candidate):
                raise RuntimeError("CONTEXT_VALIDATION_FAILED")
            if operation and (operation.candidate_digest != artifact.digest
                    or operation.validation_digest != canonical_payload_digest(operation.validation) or not operation.result_digest):
                raise RuntimeError("CONTEXT_VALIDATION_FAILED")
            if current:
                attempts = tuple((await session.execute(select(RuntimeModelInvocationRow).where(
                    RuntimeModelInvocationRow.loop_frame_id == frame_id, RuntimeModelInvocationRow.loop_iteration_no == current.iteration_no,
                    RuntimeModelInvocationRow.context_snapshot_id == current.context_snapshot_id).with_for_update())).scalars().all())
                if (not rebind or operation is None or current.context_rebind_count or not attempts
                        or any(m.state != "REJECTED" or m.purpose != "DECISION" for m in attempts)):
                    raise RuntimeError("CONTEXT_REBIND_FORBIDDEN")
                current.context_rebind_count += 1
                v = dict(operation.validation)
                v.update(before_snapshot_id=current.context_snapshot_id, before_context_digest=current.context_input_digest,
                    after_context_digest=candidate["messages_digest"])
                operation.validation = v
                operation.validation_digest = canonical_payload_digest(v)
            else:
                if rebind or frame.current_iteration >= frame.loop_policy["max_iterations"]:
                    raise RuntimeError("LOOP_ITERATION_LIMIT")
                frame.current_iteration += 1
                current = RuntimeLoopIterationRow(frame_id=frame_id, run_id=lease.run_id, step_id=frame.step_id,
                    iteration_no=frame.current_iteration, schema_version=frame.schema_version, context_cursor=frame.committed_observation_cursor,
                    context_input_digest=candidate["messages_digest"])
                session.add(current)
            version = (frame.current_context_snapshot_version or 0) + 1
            snapshot_id = str(uuid5(NAMESPACE_URL, f"snapshot:{frame_id}:{version}:{artifact.digest}"))
            snapshot = RuntimeContextSnapshotRow(snapshot_id=snapshot_id, frame_id=frame_id, run_id=lease.run_id,
                step_id=frame.step_id, version=version, snapshot_digest=artifact.digest, payload=candidate, validation=validation,
                created_generation=operation.created_generation if operation else resume_generation,
                accepted_generation=resume_generation, accepted_fence=lease.fencing_token)
            session.add(snapshot)
            await session.flush()
            current.context_snapshot_id, current.context_snapshot_version, current.context_snapshot_digest = snapshot_id, version, artifact.digest
            current.context_input_digest = candidate["messages_digest"]
            frame.current_context_snapshot_id, frame.current_context_snapshot_version, frame.current_context_snapshot_digest = snapshot_id, version, artifact.digest
            if operation:
                operation.state, operation.accepted_snapshot_id = "ACCEPTED", snapshot_id
            self._touch_loop(frame, lease)
            await session.flush()
            return current

    async def open_loop_iteration(self, lease: RunLease, frame_id: str, *, resume_generation: int, row_version: int | None = None, context_input_digest: str, context_cursor: int) -> RuntimeLoopIterationRow:
        async with self.database.transaction() as session:
            _, frame = await self._lock_loop(session, lease, frame_id, resume_generation=resume_generation)
            current = (await session.execute(select(RuntimeLoopIterationRow).where(RuntimeLoopIterationRow.frame_id == frame_id, RuntimeLoopIterationRow.iteration_no == frame.current_iteration).with_for_update())).scalar_one_or_none()
            if current is not None and current.state == "OPEN":
                if (current.context_input_digest, current.context_cursor) != (context_input_digest, context_cursor):
                    raise RuntimeError("LOOP_CONTEXT_CONFLICT")
                return current
            self._check_loop_version(frame, row_version)
            if frame.phase != "NEED_DECISION" or context_cursor != frame.committed_observation_cursor:
                raise RuntimeError("LOOP_STATE_INVALID")
            if frame.context_policy:
                raise RuntimeError("CONTEXT_SNAPSHOT_UNAVAILABLE")
            if frame.current_iteration >= frame.loop_policy.get("max_iterations", 16):
                raise RuntimeError("LOOP_ITERATION_LIMIT")
            frame.current_iteration += 1
            row = RuntimeLoopIterationRow(frame_id=frame_id, run_id=lease.run_id, step_id=frame.step_id, iteration_no=frame.current_iteration, schema_version=frame.schema_version, context_input_digest=context_input_digest, context_cursor=context_cursor)
            session.add(row)
            self._touch_loop(frame, lease)
            await session.flush()
            return row

    async def get_loop_model_response(self, lease: RunLease, frame_id: str, iteration_no: int) -> RuntimeModelInvocationRow | None:
        async with self.database.transaction() as session:
            await self._lock_loop(session, lease, frame_id, allow_control_stop=True)
            iteration = await session.get(RuntimeLoopIterationRow, (frame_id, iteration_no))
            if iteration is None:
                raise RuntimeError("LOOP_ITERATION_MISSING")
            statement = select(RuntimeModelInvocationRow).where(RuntimeModelInvocationRow.run_id == lease.run_id, RuntimeModelInvocationRow.loop_frame_id == frame_id, RuntimeModelInvocationRow.loop_iteration_no == iteration_no, RuntimeModelInvocationRow.state == "COMPLETED", RuntimeModelInvocationRow.purpose == "DECISION", RuntimeModelInvocationRow.context_snapshot_id == iteration.context_snapshot_id)
            if iteration.accepted_model_attempt is not None:
                statement = statement.where(RuntimeModelInvocationRow.attempt_number == iteration.accepted_model_attempt, RuntimeModelInvocationRow.model_attempt_number == iteration.accepted_model_attempt_number)
            return (await session.execute(statement.order_by(RuntimeModelInvocationRow.attempt_number, RuntimeModelInvocationRow.model_attempt_number).limit(1))).scalar_one_or_none()

    async def accept_loop_decision(self, lease: RunLease, frame_id: str, iteration_no: int, *, resume_generation: int, row_version: int | None = None, decision_id: str, decision_kind: str, decision_digest: str, model_attempt: tuple[int, int], action_id: str | None = None, action_recipe: Mapping[str, Any] | None = None, assistant_record: Mapping[str, Any] | None = None) -> RuntimeLoopIterationRow:
        if decision_kind not in {"FINAL", "REFUSAL", "TOOL_CALL"} or ((decision_kind == "TOOL_CALL") != (action_id is not None and action_recipe is not None)):
            raise ValueError("LOOP_DECISION_INVALID")
        async with self.database.transaction() as session:
            _, frame = await self._lock_loop(session, lease, frame_id, resume_generation=resume_generation)
            row = (await session.execute(select(RuntimeLoopIterationRow).where(RuntimeLoopIterationRow.frame_id == frame_id, RuntimeLoopIterationRow.iteration_no == iteration_no).with_for_update())).scalar_one()
            await self._memory_admission(session, lease)
            if row.decision_id is not None:
                if (row.decision_id, row.decision_digest) != (decision_id, decision_digest):
                    raise RuntimeError("LOOP_DECISION_CONFLICT")
                return row
            self._check_loop_version(frame, row_version)
            if frame.phase != "NEED_DECISION" or frame.current_iteration != iteration_no or row.state != "OPEN":
                raise RuntimeError("LOOP_STATE_INVALID")
            model = await session.get(RuntimeModelInvocationRow, (lease.run_id, frame.step_id, *model_attempt), with_for_update=True)
            if model is None or model.state != "COMPLETED" or model.loop_frame_id != frame_id or model.loop_iteration_no != iteration_no or model.purpose != "DECISION" or model.context_snapshot_id != row.context_snapshot_id:
                raise RuntimeError("LOOP_MODEL_BINDING_INVALID")
            row.decision_id, row.decision_kind, row.decision_digest = decision_id, decision_kind, decision_digest
            row.accepted_model_attempt, row.accepted_model_attempt_number = model_attempt
            row.accepted_generation = resume_generation
            row.action_id = action_id
            row.action_recipe = to_primitive(dict(action_recipe)) if action_recipe is not None else None
            row.assistant_record = to_primitive(dict(assistant_record)) if assistant_record is not None else None
            row.state = "ACCEPTED" if decision_kind == "TOOL_CALL" else "CLOSED"
            frame.phase = "DECISION_ACCEPTED" if decision_kind == "TOOL_CALL" else "RESULT_READY"
            if decision_kind != "TOOL_CALL":
                frame.result_decision_id = decision_id
                frame.stop_code = decision_kind
            self._touch_loop(frame, lease)
            await session.flush()
            return row

    async def bind_loop_action(self, lease: RunLease, frame_id: str, iteration_no: int, *, resume_generation: int, row_version: int | None = None, invocation_id: str) -> RuntimeLoopIterationRow:
        async with self.database.transaction() as session:
            return await self.bind_loop_action_in_transaction(session, lease, frame_id, iteration_no, resume_generation=resume_generation, row_version=row_version, invocation_id=invocation_id)

    async def bind_loop_action_in_transaction(self, session: AsyncSession, lease: RunLease, frame_id: str, iteration_no: int, *, resume_generation: int, row_version: int | None = None, invocation_id: str) -> RuntimeLoopIterationRow:
        _, frame = await self._lock_loop(session, lease, frame_id, resume_generation=resume_generation)
        row = await session.get(RuntimeLoopIterationRow, (frame_id, iteration_no), with_for_update=True)
        tool = await session.get(DurableToolInvocationRow, invocation_id, with_for_update=True)
        if frame.schema_version == 2:
            from core.persistence.models import RuntimeLoopActionRow
            action = await session.get(RuntimeLoopActionRow, invocation_id, with_for_update=True)
            if (row is None or action is None or (action.frame_id, action.iteration_no) != (frame_id, iteration_no)
                    or tool is None or (tool.run_id, tool.step_id) != (lease.run_id, frame.step_id)
                    or action.not_executed or frame.phase != "WAIT_ACTIONS"):
                raise RuntimeError("LOOP_ACTION_BINDING_INVALID")
            action.invocation_id = invocation_id
            await session.flush()
            return row
        if row is None or row.action_id != invocation_id or tool is None or (tool.run_id, tool.step_id) != (lease.run_id, frame.step_id):
            raise RuntimeError("LOOP_ACTION_BINDING_INVALID")
        if row.tool_invocation_id is not None:
            if row.tool_invocation_id != invocation_id:
                raise RuntimeError("LOOP_ACTION_CONFLICT")
            return row
        self._check_loop_version(frame, row_version)
        if frame.phase not in {"DECISION_ACCEPTED", "WAIT_TOOL"}:
            raise RuntimeError("LOOP_STATE_INVALID")
        row.tool_invocation_id = invocation_id
        frame.phase = "WAIT_TOOL"
        self._touch_loop(frame, lease)
        await session.flush()
        return row

    async def commit_loop_observation(self, lease: RunLease, frame_id: str, iteration_no: int, *, resume_generation: int, row_version: int | None = None, invocation_id: str, source_digest: str, observation: Mapping[str, Any], observation_digest: str) -> RuntimeLoopIterationRow:
        payload = to_primitive(dict(observation))
        if canonical_payload_digest(payload) != observation_digest:
            raise ValueError("LOOP_OBSERVATION_DIGEST_INVALID")
        async with self.database.transaction() as session:
            _, frame = await self._lock_loop(session, lease, frame_id, resume_generation=resume_generation)
            row = await session.get(RuntimeLoopIterationRow, (frame_id, iteration_no), with_for_update=True)
            tool = await session.get(DurableToolInvocationRow, invocation_id, with_for_update=True)
            if row is None or row.tool_invocation_id != invocation_id or tool is None or tool.state != "COMMITTED" or tool.committed_result is None or tool.committed_result_digest != source_digest:
                raise RuntimeError("LOOP_RESULT_UNAVAILABLE")
            if row.observation is not None:
                if (row.observation_digest, row.observation_source_digest) != (observation_digest, source_digest):
                    raise RuntimeError("LOOP_OBSERVATION_CONFLICT")
                return row
            self._check_loop_version(frame, row_version)
            if frame.phase != "WAIT_TOOL" or frame.current_iteration != iteration_no or row.state != "ACCEPTED":
                raise RuntimeError("LOOP_STATE_INVALID")
            frame.committed_observation_cursor += 1
            row.observation = payload
            row.observation_digest = observation_digest
            row.observation_source_digest = source_digest
            row.observation_cursor = frame.committed_observation_cursor
            row.observed_generation = resume_generation
            row.state = "OBSERVED"
            frame.phase = "NEED_DECISION"
            self._touch_loop(frame, lease)
            await session.flush()
            return row

    async def stop_loop(self, lease: RunLease, frame_id: str, *, resume_generation: int, row_version: int | None = None, stop_code: str, stop_details: Mapping[str, Any] | None = None) -> RuntimeLoopFrameRow:
        details = to_primitive(dict(stop_details or {}))
        async with self.database.transaction() as session:
            _, frame = await self._lock_loop(session, lease, frame_id, resume_generation=resume_generation, allow_control_stop=True)
            if frame.phase == "STOPPED":
                if frame.stop_code != stop_code or frame.stop_details != details:
                    raise RuntimeError("LOOP_STOP_CONFLICT")
                return frame
            self._check_loop_version(frame, row_version)
            if frame.final_delivery_receipt is not None:
                raise RuntimeError("LOOP_FINAL_ALREADY_DELIVERED")
            frame.phase, frame.stop_code = "STOPPED", stop_code
            frame.stop_details = details
            self._touch_loop(frame, lease)
            await session.flush()
            return frame

    async def suspend_loop(self, lease: RunLease, frame_id: str, *, resume_generation: int, row_version: int | None = None, reason: str, next_recovery_at: datetime | None = None, manual_required: bool = False) -> RuntimeLoopFrameRow:
        """只保存等待与重扫边界；不改变 Step 或 Run 的非终态事实。"""
        async with self.database.transaction() as session:
            root, frame = await self._lock_loop(session, lease, frame_id, resume_generation=resume_generation, row_version=row_version, allow_control_stop=True)
            if frame.phase == "DECISION_ACCEPTED":
                frame.phase = "WAIT_TOOL"
            if frame.phase not in {"WAIT_TOOL", "NEED_DECISION"}:
                raise RuntimeError("LOOP_NOT_SUSPENDABLE")
            root.last_recovery_error = reason[:128]
            root.next_recovery_at = next_recovery_at or datetime.now(UTC) + timedelta(seconds=5)
            root.manual_required = manual_required
            root.execution_version += 1
            root.last_committed_fencing_token = lease.fencing_token
            self._touch_loop(frame, lease)
            await session.flush()
            return frame

    @staticmethod
    def _budget_usage(usage: Any) -> dict[str, int]:
        from core.runtime.budget import BudgetUsage, _DIMENSIONS
        if usage is None:
            return {}
        values = asdict(usage) if is_dataclass(usage) else dict(usage)
        values.pop("_allow_independent_total", None)
        if set(values) - set(_DIMENSIONS):
            raise ValueError("unknown budget dimension")
        BudgetUsage(**values, _allow_independent_total=True)
        return {key: value for key, value in values.items() if value}

    @staticmethod
    def _budget_add(left: Mapping[str, int], right: Mapping[str, int], sign: int = 1) -> dict[str, int]:
        result = dict(left)
        for key, value in right.items():
            result[key] = result.get(key, 0) + sign * value
            if result[key] < 0:
                raise RuntimeError("LOOP_BUDGET_INCONSISTENT")
        return result

    async def reserve_budget_operation(self, lease: RunLease, *, operation_id: str, usage: Any, kind: str, resume_generation: int, frame_id: str) -> dict:
        async with self.database.transaction() as session:
            return await self.reserve_budget_operation_in_transaction(session, lease, operation_id=operation_id, usage=usage, kind=kind, resume_generation=resume_generation, frame_id=frame_id)

    async def reserve_budget_operation_in_transaction(self, session: AsyncSession, lease: RunLease, *, operation_id: str, usage: Any, kind: str, resume_generation: int, frame_id: str) -> dict:
        root, frame = await self._lock_loop(session, lease, frame_id, resume_generation=resume_generation)
        values = self._budget_usage(usage)
        operations = dict(root.budget_operations)
        if operation_id in operations:
            stored = operations[operation_id]
            if stored["reserved"] != values or stored["kind"] != kind:
                raise RuntimeError("LOOP_BUDGET_OPERATION_CONFLICT")
            return stored
        if len(operations) >= 512:
            raise RuntimeError("LOOP_BUDGET_OPERATION_LIMIT")
        allocation = operations.get(f"delegation:{frame.delegation_id}") if frame.delegation_id else None
        if allocation is not None:
            if allocation["state"] != "RESERVED" or allocation.get("overrun") or any(n > allocation["unspent"].get(d, 0) for d, n in values.items()):
                raise RuntimeError("DELEGATION_BUDGET_EXHAUSTED")
            allocation = {**allocation, "unspent": self._budget_add(allocation["unspent"], values, -1)}
            operations[f"delegation:{frame.delegation_id}"] = allocation
            reserved = dict(root.budget_reserved)
        else:
            reserved = self._budget_add(root.budget_reserved, values)
        for dimension, count in values.items():
            limit = root.budget_totals.get(f"max_{dimension}")
            if limit is not None and root.budget_consumed.get(dimension, 0) + reserved.get(dimension, 0) > limit:
                raise RuntimeError(f"LOOP_BUDGET_EXHAUSTED:{dimension}")
        operation = {"kind": kind, "state": "RESERVED", "reserved": values, "actual": None, "source": None, "generation": resume_generation, **({"delegation_id": frame.delegation_id} if frame.delegation_id else {})}
        operations[operation_id] = operation
        root.budget_operations, root.budget_reserved = operations, reserved
        root.execution_version += 1
        root.last_committed_fencing_token = lease.fencing_token
        root.updated_at = func.now()
        return operation

    async def settle_budget_operation(self, lease: RunLease, *, operation_id: str, usage: Any = None, source: str = "ACTUAL", resume_generation: int, frame_id: str) -> dict:
        async with self.database.transaction() as session:
            return await self.settle_budget_operation_in_transaction(session, lease, operation_id=operation_id, usage=usage, source=source, resume_generation=resume_generation, frame_id=frame_id)

    async def settle_budget_operation_in_transaction(self, session: AsyncSession, lease: RunLease, *, operation_id: str, usage: Any = None, source: str = "ACTUAL", resume_generation: int, frame_id: str) -> dict:
        root, _ = await self._lock_loop(session, lease, frame_id, resume_generation=resume_generation, allow_control_stop=True)
        operations = dict(root.budget_operations)
        stored = operations.get(operation_id)
        if stored is None:
            raise RuntimeError("LOOP_BUDGET_OPERATION_MISSING")
        values = stored["reserved"] if usage is None else self._budget_usage(usage)
        if stored["state"] == "SETTLED":
            if stored["actual"] != values:
                raise RuntimeError("LOOP_BUDGET_SETTLEMENT_CONFLICT")
            return stored
        reserved = self._budget_add(root.budget_reserved, stored["reserved"], -1)
        consumed = self._budget_add(root.budget_consumed, values)
        delegation = stored.get("delegation_id")
        if delegation:
            key = f"delegation:{delegation}"
            allocation = operations[key]
            balance = self._budget_add(allocation["unspent"], stored["reserved"])
            unspent = {d: max(0, n-values.get(d, 0)) for d, n in balance.items()}
            reserved = self._budget_add(reserved, {d: unspent.get(d,0)-allocation["unspent"].get(d,0) for d in set(unspent)|set(allocation["unspent"])})
            operations[key] = {**allocation, "unspent": unspent, "consumed": self._budget_add(allocation["consumed"], values),
                "overrun": allocation.get("overrun", False) or any(n>balance.get(d,0) for d,n in values.items())}
        for dimension in values:
            limit = root.budget_totals.get(f"max_{dimension}")
            if root.execution_policy != "durable-agent-loop.v2" and limit is not None and consumed.get(dimension, 0) + reserved.get(dimension, 0) > limit:
                raise RuntimeError(f"LOOP_BUDGET_EXHAUSTED:{dimension}")
        root.budget_reserved, root.budget_consumed = reserved, consumed
        operation = {**stored, "state": "SETTLED", "actual": values, "source": source}
        operations[operation_id] = operation
        root.budget_operations = operations
        root.execution_version += 1
        root.last_committed_fencing_token = lease.fencing_token
        root.updated_at = func.now()
        return operation

    def _settle_recovery_budget(self, root: RuntimeRunExecutionRow, lease: RunLease) -> None:
        operations = dict(root.budget_operations)
        changed = False
        for key, operation in operations.items():
            if operation["state"] == "RESERVED" and operation["kind"] != "DELEGATION_ALLOCATION":
                changed = True
                root.budget_reserved = self._budget_add(root.budget_reserved, operation["reserved"], -1)
                root.budget_consumed = self._budget_add(root.budget_consumed, operation["reserved"])
                operations[key] = {**operation, "state": "SETTLED", "actual": operation["reserved"], "source": "RECOVERY_CONSERVATIVE"}
                if operation.get("delegation_id"):
                    eid = f"delegation:{operation['delegation_id']}"
                    envelope = operations[eid]
                    operations[eid] = {**envelope, "consumed": self._budget_add(envelope["consumed"], operation["reserved"])}
        root.budget_operations = operations
        if changed:
            root.execution_version += 1
            root.last_committed_fencing_token = lease.fencing_token
            root.updated_at = func.now()

    async def publish_loop_final(self, lease: RunLease, frame_id: str, decision_id: str, result_digest: str, event: Any, journal: Any, client_event_feed: Any, *, resume_generation: int | None = None, row_version: int | None = None) -> dict:
        from core.runtime.events import RuntimeEventType
        if client_event_feed is None or not callable(getattr(client_event_feed, "append_event_in_transaction", None)):
            raise ValueError("LOOP_ATOMIC_FEED_REQUIRED")
        if event.run_id != lease.run_id or event.event_type != RuntimeEventType.OUTPUT_DELTA:
            raise ValueError("LOOP_FINAL_EVENT_INVALID")
        projected = False
        async with self.database.transaction() as session:
            _, frame = await self._lock_loop(session, lease, frame_id, resume_generation=resume_generation, allow_control_stop=True)
            if frame.final_delivery_receipt is not None:
                receipt = frame.final_delivery_receipt
                if receipt["decision_id"] != decision_id or receipt["result_digest"] != result_digest:
                    raise RuntimeError("LOOP_FINAL_CONFLICT")
                return receipt
            await self._lock_loop(session, lease, frame_id, resume_generation=resume_generation, row_version=row_version)
            if frame.frame_role != "ROOT" or frame.phase != "RESULT_READY" or frame.result_decision_id != decision_id:
                raise RuntimeError("LOOP_FINAL_NOT_READY")
            iteration = (await session.execute(select(RuntimeLoopIterationRow).where(RuntimeLoopIterationRow.frame_id == frame_id, RuntimeLoopIterationRow.decision_id == decision_id).with_for_update())).scalar_one()
            await self._memory_admission(session, lease)
            model = await session.get(RuntimeModelInvocationRow, (lease.run_id, frame.step_id, iteration.accepted_model_attempt, iteration.accepted_model_attempt_number), with_for_update=True)
            if model is None or model.state != "COMPLETED":
                raise RuntimeError("LOOP_MODEL_BINDING_INVALID")
            from core.runtime.agent_loop.contracts import LoopDecisionV1, LoopDecisionV2
            from core.runtime.model_invocation import restore_loop_model_response
            decision = (LoopDecisionV2 if frame.schema_version == 2 else LoopDecisionV1).parse(restore_loop_model_response(model), protocol_mode=frame.context_seed["protocol_mode"], synthetic_id="unused")
            if decision.kind not in {"FINAL", "REFUSAL"} or decision.digest != iteration.decision_digest or hashlib.sha256(decision.content.encode("utf-8")).hexdigest() != result_digest:
                raise RuntimeError("LOOP_FINAL_CONTENT_DIGEST_INVALID")
            content_digest = hashlib.sha256(event.payload.text.encode("utf-8")).hexdigest()
            if content_digest != result_digest:
                raise RuntimeError("LOOP_FINAL_CONTENT_DIGEST_INVALID")
            receipt = {"decision_id": decision_id, "result_digest": result_digest, "content_digest": content_digest, "event_id": str(event.event_id), "sequence": event.sequence, "cursor": event.sequence, "status": "DELIVERED"}
            await journal.append_in_transaction(session, event)
            projected = await client_event_feed.append_event_in_transaction(session, event)
            frame.final_delivery_receipt = receipt
            self._touch_loop(frame, lease)
        if projected:
            callback = getattr(client_event_feed, "projection_committed", None)
            if callable(callback):
                callback(event)
            callback = getattr(client_event_feed, "record_write_succeeded", None)
            if callable(callback):
                callback()
        return receipt

    async def seal_context_evidence(self, run_id):
        from core.runtime.agent_loop.context_evidence import build_context_evidence
        async with self.database.transaction() as session:
            root = (await session.execute(select(RuntimeRunExecutionRow).where(RuntimeRunExecutionRow.run_id == run_id).with_for_update())).scalar_one()
            if root.execution_policy == "durable-agent-loop.v2":
                raise ValueError("UNSUPPORTED_EXECUTION_POLICY")
            frame = (await session.execute(select(RuntimeLoopFrameRow).where(RuntimeLoopFrameRow.run_id == run_id).with_for_update())).scalar_one()
            if frame.sealed_context_evidence is not None:
                return frame.sealed_context_evidence
            if root.status == "ACTIVE":
                raise RuntimeError("CONTEXT_EVIDENCE_NOT_READY")
            image = await self.load(run_id, session=session)
            # 冻结 Context 协议只覆盖上下文决策/压缩；post-delivery Formation 留在独立 Memory Evidence。
            context_image = replace(image, models=tuple(r for r in image.models if r.purpose in {"DECISION","COMPACTION"}))
            body = build_context_evidence(context_image)
            frame.sealed_context_evidence = {"body": body, "digest": canonical_payload_digest(body)}
            await session.flush()
            return frame.sealed_context_evidence

    async def read_context_evidence(self, run_id):
        image = await self.load(run_id)
        if image and image.root.execution_policy == "durable-agent-loop.v2":
            raise ValueError("UNSUPPORTED_EXECUTION_POLICY")
        return image.loop_frames[0].sealed_context_evidence if image and image.loop_frames else None

    async def read_loop_evidence_source(self, run_id: str) -> RecoveryImage | None:
        image = await self.load(run_id)
        if image is not None and image.root.execution_policy == "durable-agent-loop.v2":
            raise ValueError("UNSUPPORTED_EXECUTION_POLICY")
        return image

    async def read_agent_execution_evidence(self, run_id):
        from core.persistence.models import RuntimeLoopActionRow, RuntimeAgentDelegationRow, RuntimeMemoryBindingRow
        from core.runtime.agent_loop.execution_evidence import build_execution_evidence
        async with self.database.transaction() as session:
            root = await session.get(RuntimeRunExecutionRow, run_id, with_for_update=True)
            if root is None:
                raise ValueError("AGENT_EXECUTION_EVIDENCE_UNAVAILABLE")
            image = await self._load_in_session(session, run_id)
            actions = tuple((await session.execute(select(RuntimeLoopActionRow).where(RuntimeLoopActionRow.run_id == run_id)
                .order_by(RuntimeLoopActionRow.frame_id, RuntimeLoopActionRow.iteration_no, RuntimeLoopActionRow.ordinal))).scalars())
            delegations = tuple((await session.execute(select(RuntimeAgentDelegationRow).where(RuntimeAgentDelegationRow.run_id == run_id)
                .order_by(RuntimeAgentDelegationRow.delegation_id))).scalars())
            binding = await session.get(RuntimeMemoryBindingRow, run_id)
            body = build_execution_evidence(image, actions, delegations, binding)
            if binding is not None and binding.mode == "SCOPED":
                from core.runtime.memory_scope import MemoryAccessContext, MemoryContextError
                from .memory_lifecycle import MemoryLifecycleRepository
                memory = MemoryLifecycleRepository(self.database)
                try:
                    access = MemoryAccessContext.from_dict(binding.access)
                    await memory._locks(session, [s["scope_id"] for s in binding.scopes] + [d["scope_id"] for d in binding.manifest])
                    for scope in binding.scopes:
                        row = await memory._scope(session, access, scope["scope_id"])
                        if row.authz_epoch != scope["epoch"]:
                            raise MemoryContextError("MEMORY_CONTEXT_REVOKED")
                    await memory._dependencies(session, binding.manifest)
                    body["memory_binding"]["current_eligibility"] = "VALID"
                except MemoryContextError:
                    body["memory_binding"]["current_eligibility"] = "INVALID"
                    body["gaps"] = sorted(set(body["gaps"]) | {"MEMORY_ELIGIBILITY_INVALID"})
                    body["coverage"] = "PARTIAL"
            return {"body": body, "digest": canonical_payload_digest(body)}

    async def seal_loop_evidence(self, run_id: str, body: Mapping[str, Any], digest: str) -> dict:
        async with self.database.transaction() as session:
            root = (await session.execute(select(RuntimeRunExecutionRow).where(RuntimeRunExecutionRow.run_id == run_id).with_for_update())).scalar_one()
            if root.execution_policy == "durable-agent-loop.v2":
                raise ValueError("UNSUPPORTED_EXECUTION_POLICY")
            frame = (await session.execute(select(RuntimeLoopFrameRow).where(RuntimeLoopFrameRow.run_id == run_id).with_for_update())).scalar_one()
            if frame.sealed_evidence is not None:
                return frame.sealed_evidence
            if root.status == "ACTIVE":
                raise RuntimeError("LOOP_EVIDENCE_NOT_READY")
            rows = tuple((await session.execute(select(RuntimeLoopIterationRow).where(RuntimeLoopIterationRow.frame_id == frame.frame_id).order_by(RuntimeLoopIterationRow.iteration_no))).scalars().all())
            if tuple(row.iteration_no for row in rows) != tuple(range(1, frame.current_iteration + 1)):
                raise RuntimeError("LOOP_EVIDENCE_PREFIX_INVALID")
            if root.status == "SUCCEEDED" and (frame.final_delivery_receipt is None or frame.result_decision_id != frame.final_delivery_receipt["decision_id"]):
                raise RuntimeError("LOOP_FINAL_RECEIPT_MISSING")
            frame.sealed_evidence = {"body": to_primitive(dict(body)), "digest": digest}
            frame.row_version += 1
            await session.flush()
            return frame.sealed_evidence

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
