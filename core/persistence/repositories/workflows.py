"""Workflow 的事务与步骤关联。原 Run 表仅用于读取接纳和终态证据。"""

from __future__ import annotations

from asyncio import to_thread
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert

from core.agent_platform import ci_repair_artifacts as repair
from core.agent_platform import ci_repair_replay as replay_service
from core.agent_platform.workflow_artifacts import WorkflowArtifactError
from core.auth import ObjectAuthorizationService, require_owned
from core.incidents.contracts import CIError
from core.persistence.models import (
    ClientDeliveryEventRow, ExternalRunRow, ObjectOwnershipRow, RunControlRow,
    RuntimeEventJournalRow, WorkflowApprovalRow, WorkflowRunRow, WorkflowStepRunRow, WorkflowVersionRow,
    WorkspaceWorkflowRow,
)
from core.persistence.repositories.runtime import lock_run_scope
from core.persistence.repositories.workflow_approvals import (
    WorkflowApprovalError, approval_binding_digest, approval_binding_payload, consume_approval, require_dispatch_approval,
    verify_approval_binding, workflow_approval,
)
from core.runtime.cancellation import CancellationReason
from core.runtime.event_journal_store import journal_record_from_row
from core.runtime.events import RuntimeEventType
from core.runtime.execution_aggregate import canonical_payload_digest
from core.runtime.run_control import DurableRunControlService
from core.workspace.service import WorkspaceService


TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"})


@dataclass(frozen=True)
class StepEvidence:
    observation: str
    runtime_status: str | None = None
    result_ref: dict | None = None
    reason: str | None = None
    artifact_ref: dict | None = None
    next_input: str | None = None
    validation_error: str | None = None
    validation_retryable: bool = False
    native_output: str | None = None


def _set_status(run, status, reason=None):
    run.status = status
    run.reason = reason
    run.updated_at = datetime.now(UTC)


class WorkflowRepository:
    def __init__(self, database):
        self.database = database

    @staticmethod
    async def _lock_workflow(session, run_id):
        return await session.get(WorkflowRunRow, run_id, with_for_update=True)

    @staticmethod
    async def _step(session, run_id, position):
        return await session.scalar(select(WorkflowStepRunRow).where(
            WorkflowStepRunRow.workflow_run_id == run_id, WorkflowStepRunRow.position == position,
        ))

    async def find_request(self, principal, request_id):
        async with self.database.session() as session:
            return await self._find_request(session, principal, request_id)

    @staticmethod
    async def _find_request(session, principal, request_id):
        return await session.scalar(select(WorkflowRunRow).where(
            WorkflowRunRow.tenant_id == principal.tenant_id, WorkflowRunRow.owner_user_id == principal.user_id,
            WorkflowRunRow.request_id == request_id,
        ))

    async def create(self, principal, request_id, definition, digest, *, handoff_input=None, space_id=None, repair_baseline=None):
        async with self.database.transaction() as session:
            await lock_run_scope(session, f"workflow-request:{principal.tenant_id}:{principal.user_id}:{request_id}")
            if space_id is not None:
                await WorkspaceService.require_write_access_in_session(session, principal, space_id)
            prior = await self._find_request(session, principal, request_id)
            if prior is not None:
                return prior, False
            if repair_baseline is not None:
                root_key = canonical_payload_digest(repair_baseline["root_id"])
                await lock_run_scope(session, "ci-repair-root:" + root_key)
                taken = await session.scalar(select(WorkflowRunRow.workflow_run_id).where(
                    WorkflowRunRow.repair_baseline["root_id"] == repair_baseline["root_id"],
                ).limit(1))
                if taken is not None:
                    raise WorkflowArtifactError("CI_REPAIR_REPOSITORY_RESERVED")
                await to_thread(repair.require_pristine, repair_baseline)
            key = (definition.workflow_id, definition.workflow_version)
            if space_id is None:
                await session.execute(insert(WorkflowVersionRow).values(
                    workflow_id=key[0], workflow_version=key[1], definition_digest=digest,
                ).on_conflict_do_nothing())
            version = await session.get(WorkflowVersionRow, key)
            if (version is None or version.definition_digest != digest
                    or (version.definition_snapshot is not None) != (space_id is not None)):
                return None, False
            run = WorkflowRunRow(
                workflow_run_id=str(uuid4()), workflow_id=key[0], workflow_version=key[1],
                definition_digest=digest, tenant_id=principal.tenant_id, owner_user_id=principal.user_id,
                space_id=space_id,
                request_id=request_id, handoff_input=handoff_input, approval_phase=definition.approval_phase,
                repair_baseline=repair_baseline,
                status="PENDING", current_step=0, cancel_requested=False,
            )
            await ObjectAuthorizationService.bind_new_in_transaction(
                session, principal, "WORKFLOW_RUN", run.workflow_run_id, space_id=space_id,
            )
            session.add(run)
            await session.flush()
            session.add_all([
                WorkflowStepRunRow(
                    workflow_run_id=run.workflow_run_id, step_id=task.task_id, attempt=1, position=position,
                    agent_id=task.agent_id, agent_version=task.agent_version, runtime_kind=task.runtime_kind,
                    runtime_instance_id=task.runtime_instance_id, input_digest=canonical_payload_digest(task.instruction),
                    status="PENDING",
                ) for position, task in enumerate(definition.tasks)
            ])
            return run, True

    async def published_version(self, principal, space_id, workflow_id, version):
        async with self.database.session() as session:
            await WorkspaceService.require_access_in_session(session, principal, space_id)
            return await session.scalar(select(WorkflowVersionRow).join(
                WorkspaceWorkflowRow, WorkspaceWorkflowRow.id == WorkflowVersionRow.workflow_id,
            ).where(
                WorkspaceWorkflowRow.space_id == space_id, WorkspaceWorkflowRow.tenant_id == principal.tenant_id,
                WorkflowVersionRow.workflow_id == workflow_id, WorkflowVersionRow.workflow_version == version,
                WorkflowVersionRow.definition_snapshot.is_not(None),
            ))

    async def read(self, run_id):
        run, steps, _ = await self.snapshot(run_id)
        return run, steps

    async def snapshot(self, run_id, *, lock=True):
        async with self.database.transaction() as session:
            run = await session.get(WorkflowRunRow, run_id, with_for_update=lock)
            if run is None:
                return None, (), None
            steps = tuple(await session.scalars(select(WorkflowStepRunRow).where(
                WorkflowStepRunRow.workflow_run_id == run_id,
            ).order_by(WorkflowStepRunRow.position)))
            approval = await workflow_approval(session, run_id) if run.approval_phase is not None else None
            return run, steps, approval

    async def decide_approval(self, run_id, approval_id, actor, decision, binding_digest, diff_digest=None):
        decisions = {"APPROVE": "APPROVED", "REJECT": "REJECTED"}
        if decision not in decisions:
            raise WorkflowApprovalError("WORKFLOW_APPROVAL_DECISION_INVALID", 422)
        async with self.database.transaction() as session:
            run = await self._lock_workflow(session, run_id)
            await ObjectAuthorizationService.require_current_human_in_session(session, actor)
            if run.space_id is not None:
                await WorkspaceService.require_write_access_in_session(session, actor, run.space_id, manage=True)
            approval = await workflow_approval(session, run_id)
            if approval is None or approval.approval_id != approval_id:
                raise WorkflowApprovalError("WORKFLOW_APPROVAL_NOT_FOUND", 404)
            if run.cancel_requested or approval.status == "INVALIDATED":
                raise WorkflowApprovalError("WORKFLOW_APPROVAL_INVALIDATED")
            if (binding_digest is not None or run.repair_baseline is not None) and approval.binding_digest != binding_digest:
                raise WorkflowApprovalError("WORKFLOW_APPROVAL_BINDING_MISMATCH")
            consumer = await self._step(session, run_id, 2 if run.repair_baseline is not None else 1)
            producer = await verify_approval_binding(session, run, consumer, approval)
            if run.repair_baseline is not None and producer.artifact_ref["diff_digest"] != diff_digest:
                raise WorkflowApprovalError("CI_REPAIR_DIFF_MISMATCH")
            new_status = decisions[decision]
            if approval.status != "PENDING":
                if approval.status == new_status and approval.decided_by == actor.user_id:
                    return approval
                raise WorkflowApprovalError("WORKFLOW_APPROVAL_DECISION_CONFLICT")
            if run.status != "WAITING_APPROVAL" or consumer.platform_run_id is not None:
                raise WorkflowApprovalError("WORKFLOW_APPROVAL_DECISION_CONFLICT")
            if run.repair_baseline is not None and decision == "APPROVE":
                try:
                    await to_thread(repair.verify_current, run, producer)
                except WorkflowArtifactError as exc:
                    approval.status = "INVALIDATED"
                    _set_status(run, "PAUSED", exc.code)
                    return approval
            approval.status = new_status
            approval.decided_by = actor.user_id
            approval.decided_at = datetime.now(UTC)
            _set_status(run, "REJECTED" if new_status == "REJECTED" else "WAITING_APPROVAL")
            return approval

    async def prepare(self, run_id, position, principal, conversation_id, *, definition_digest=None,
                      runtime_target_digest=None, effective_input=None, prepare_artifact=None, bind_investigation=None):
        """锁序为 Workflow 行 → 子 Run 锁。旧凭证在同一个 Run 锁下失效。"""
        async with self.database.transaction() as session:
            run = await self._lock_workflow(session, run_id)
            if run.status in TERMINAL or run.cancel_requested or run.current_step != position:
                return None
            if run.space_id is not None:
                await WorkspaceService.require_write_access_in_session(session, principal, run.space_id)
            step = await self._step(session, run_id, position)
            if step.runtime_kind == "REPLAY":
                return None
            platform_run_id = step.platform_run_id or str(uuid4())
            await lock_run_scope(session, platform_run_id)
            control = await session.get(RunControlRow, platform_run_id)
            external = await session.get(ExternalRunRow, platform_run_id)
            if external is not None or (control is not None and (control.fencing_token > 0 or control.state == "CLOSED")):
                return None
            if run.repair_baseline is None and run.approval_phase is not None and step.position == 1:
                await require_dispatch_approval(session, run, step)
            if step.runtime_kind == "OPENCODE":
                if (definition_digest != run.definition_digest or runtime_target_digest is None
                        or step.runtime_target_digest not in (None, runtime_target_digest)):
                    return None
                step.runtime_target_digest = runtime_target_digest
            if step.platform_run_id is None:
                if control is not None or await session.get(ObjectOwnershipRow, ("RUN", platform_run_id)) is not None:
                    _set_status(run, "PAUSED", "WORKFLOW_RUN_ID_CONFLICT")
                    return None
            if run.repair_baseline is not None:
                if step.position == 0:
                    if effective_input is None or bind_investigation is None:
                        return None
                    if step.platform_run_id is not None and step.input_digest != canonical_payload_digest(effective_input):
                        return None
                    step.input_digest = canonical_payload_digest(effective_input)
                    await bind_investigation(session, platform_run_id)
                elif step.bound_input is None or step.bound_input != effective_input:
                    return None
                else:
                    await to_thread(repair.require_pristine, run.repair_baseline)
            elif run.handoff_input is not None:
                if step.position == 0:
                    if effective_input is None or prepare_artifact is None:
                        return None
                    if step.platform_run_id is None:
                        step.input_digest = canonical_payload_digest(effective_input)
                    elif canonical_payload_digest(effective_input) != step.input_digest:
                        return None
                    step.artifact_directory = prepare_artifact(step.platform_run_id is None, step.artifact_directory)
                elif step.bound_input is None or step.bound_input != effective_input:
                    return None
            if step.platform_run_id is None:
                await ObjectAuthorizationService.bind_new_in_transaction(
                    session, principal, "RUN", platform_run_id, space_id=run.space_id,
                )
                await ObjectAuthorizationService.bind_new_in_transaction(
                    session, principal, "CONVERSATION", conversation_id, space_id=run.space_id,
                )
                step.platform_run_id = platform_run_id
            step.status = "RESERVED"
            step.dispatch_token = str(uuid4())
            step.reason = None
            _set_status(run, "RUNNING")
            return step

    async def evidence(self, step) -> StepEvidence:
        run_id = step.platform_run_id
        if run_id is None:
            return StepEvidence("PENDING")
        async with self.database.session() as session:
            if step.runtime_kind == "OPENCODE":
                row = await session.get(ExternalRunRow, run_id)
                if row is None:
                    return StepEvidence("RESERVED")
                run = await session.get(WorkflowRunRow, step.workflow_run_id)
                if (row.instance_id != step.runtime_instance_id or row.agent_id != step.agent_id
                        or row.agent_version != step.agent_version or row.tenant_id != run.tenant_id
                        or row.user_id != str(run.owner_user_id)
                        or row.session_id != f"{run.workflow_run_id}:{step.step_id}"):
                    return StepEvidence("UNVERIFIED", reason="WORKFLOW_RUNTIME_BINDING_MISMATCH")
                ref = None
                if (row.terminal_status == "SUCCEEDED" and not row.error_code
                        and row.external_session_id and row.user_message_id and row.assistant_message_id):
                    ref = {"kind": "OPENCODE_MESSAGE", "instance_id": row.instance_id,
                           "session_id": row.external_session_id, "user_message_id": row.user_message_id,
                           "message_id": row.assistant_message_id}
                return StepEvidence(row.observation_state, row.terminal_status, ref, row.error_code)

            control = await session.get(RunControlRow, run_id)
            if control is None or control.fencing_token == 0:
                return StepEvidence("RESERVED")
            if control.state != "CLOSED" or control.terminal_sequence is None:
                return StepEvidence("ACTIVE")
            row = await session.get(RuntimeEventJournalRow, (run_id, control.terminal_sequence))
            if row is None or row.event_type != RuntimeEventType.RUN_COMPLETED.value:
                return StepEvidence("UNKNOWN", reason="NATIVE_TERMINAL_UNVERIFIED")
            terminal = journal_record_from_row(row)
            status = terminal.safe_payload.get("status")
            reason = terminal.safe_payload.get("safe_error_code")
            ref = None
            native_output = None
            if status == "SUCCEEDED":
                delivery = await session.get(ClientDeliveryEventRow, (run_id, terminal.sequence))
                if delivery is not None and delivery.event_id == terminal.event_id:
                    if delivery.payload.get("business_output_valid") is False:
                        reason = "BUSINESS_OUTPUT_INVALID"
                    elif step.bound_input is not None and step.runtime_kind == "NATIVE":
                        if (delivery.payload.get("business_output_valid") is True
                                and delivery.payload.get("output_disposition") == "ACCEPTED"
                                and isinstance(delivery.payload.get("output"), str)):
                            native_output = delivery.payload["output"]
                            ref = {"kind": "NATIVE_FEED", "terminal_sequence": terminal.sequence}
                    elif delivery.payload.get("output") or await self._native_text_available(session, run_id, terminal.sequence):
                        ref = {"kind": "NATIVE_FEED", "terminal_sequence": terminal.sequence}
            return StepEvidence("TERMINAL", status, ref, reason, native_output=native_output)

    @staticmethod
    async def _native_text_available(session, run_id, terminal_sequence):
        journal = RuntimeEventJournalRow
        feed = ClientDeliveryEventRow
        # 正文依旧由 Client Feed 交付。按 Journal 事件身份检查完整性，拒绝缺段的输出。
        text = feed.payload["text"].astext
        expected, delivered, has_text = (await session.execute(select(
            func.count(), func.count(text), func.bool_or(text != ""),
        ).select_from(journal).outerjoin(feed, and_(
            feed.run_id == journal.run_id, feed.cursor == journal.sequence,
            feed.event_id == journal.event_id, feed.event_type == "output.delta",
        )).where(
            journal.run_id == run_id, journal.sequence < terminal_sequence,
            journal.event_type == RuntimeEventType.OUTPUT_DELTA.value,
        ))).one()
        return expected == delivered and bool(has_text)

    async def record(self, run_id, step, evidence) -> bool:
        """提交已观察终态。返回值只表示本次是否推进到下一步骤。"""
        if evidence.runtime_status not in TERMINAL:
            return False
        async with self.database.transaction() as session:
            run = await self._lock_workflow(session, run_id)
            if run.status in TERMINAL or run.current_step != step.position:
                return False
            current = await self._step(session, run_id, step.position)
            if current.platform_run_id != step.platform_run_id:
                return False
            current.runtime_status = evidence.runtime_status
            current.result_ref = evidence.result_ref
            current.reason = evidence.reason
            if run.cancel_requested:
                current.status = evidence.runtime_status
                _set_status(run, "CANCELLED")
            elif evidence.runtime_status != "SUCCEEDED" or evidence.reason == "BUSINESS_OUTPUT_INVALID":
                current.status = "FAILED" if evidence.runtime_status == "SUCCEEDED" else evidence.runtime_status
                _set_status(run, current.status, evidence.reason)
            elif evidence.validation_error:
                current.reason = evidence.validation_error
                if evidence.validation_retryable:
                    _set_status(run, "PAUSED", current.reason)
                else:
                    current.status = "FAILED"
                    _set_status(run, "FAILED", current.reason)
            elif evidence.result_ref is None or (
                run.repair_baseline is not None and (evidence.next_input is None or
                    (current.position == 1 and evidence.artifact_ref is None))
            ) or (
                run.repair_baseline is None and
                current.position == 0 and (run.handoff_input is not None or run.approval_phase is not None)
                and (run.handoff_input is None or evidence.artifact_ref is None or evidence.next_input is None)
            ):
                current.reason = "WORKFLOW_RESULT_UNAVAILABLE" if evidence.result_ref is None else "WORKFLOW_VALIDATION_REQUIRED"
                _set_status(run, "PAUSED", current.reason)
            else:
                current.status = "SUCCEEDED"
                if run.repair_baseline is not None:
                    successor = await self._step(session, run_id, current.position + 1)
                    successor.bound_input = evidence.next_input
                    successor.input_digest = canonical_payload_digest(evidence.next_input)
                    run.current_step = successor.position
                    if current.position == 0:
                        _set_status(run, "PENDING")
                        return True
                    current.artifact_ref = evidence.artifact_ref
                    session.add(WorkflowApprovalRow(
                        approval_id=str(uuid4()), workflow_run_id=run_id, step_id=current.step_id,
                        attempt=current.attempt, approval_phase=run.approval_phase,
                        tenant_id=run.tenant_id, owner_user_id=run.owner_user_id, status="PENDING",
                        binding_digest=approval_binding_digest(run, current, successor),
                    ))
                    _set_status(run, "WAITING_APPROVAL")
                    return False
                if step.position == 1:
                    _set_status(run, "SUCCEEDED")
                else:
                    if run.handoff_input is not None:
                        current.artifact_ref = evidence.artifact_ref
                        successor = await self._step(session, run_id, 1)
                        successor.bound_input = evidence.next_input
                        successor.input_digest = canonical_payload_digest(evidence.next_input)
                    run.current_step = 1
                    if run.approval_phase is not None:
                        session.add(WorkflowApprovalRow(
                            approval_id=str(uuid4()), workflow_run_id=run_id, step_id=current.step_id,
                            attempt=current.attempt, approval_phase=run.approval_phase,
                            tenant_id=run.tenant_id, owner_user_id=run.owner_user_id, status="PENDING",
                            binding_digest=approval_binding_digest(run, current, successor),
                        ))
                        _set_status(run, "WAITING_APPROVAL")
                        return False
                    _set_status(run, "PENDING")
                    return True
            return False

    async def complete_replay(self, run_id, principal):
        """同一事务封存回放事实、消费批准并完成 Workflow。"""
        async with self.database.transaction() as session:
            run = await self._lock_workflow(session, run_id)
            if run.status in TERMINAL or run.cancel_requested or run.repair_baseline is None or run.current_step != 2:
                return
            await ObjectAuthorizationService.require_current_human_in_session(session, principal)
            require_owned(principal, run.owner_user_id)
            await WorkspaceService.require_write_access_in_session(session, principal, run.space_id)
            approval = await workflow_approval(session, run_id)
            if (approval is None or approval.status != "APPROVED" or approval.consumed_at is not None
                    or approval.decided_by is None or approval.decided_at is None):
                return
            consumer = await self._step(session, run_id, 2)
            try:
                producer = await verify_approval_binding(session, run, consumer, approval)
                await to_thread(repair.verify_current, run, producer)
                native = await self._step(session, run_id, 0)
                consume_approval(approval)
                consumer.result_ref = await replay_service.seal(
                    session, run, native, producer, consumer, approval, approval_binding_payload(run, producer, consumer),
                )
            except (CIError, WorkflowArtifactError, WorkflowApprovalError) as exc:
                approval.consumed_at = None
                approval.status = "INVALIDATED"
                _set_status(run, "PAUSED", exc.code)
                return
            consumer.status = "SUCCEEDED"
            _set_status(run, "SUCCEEDED")

    async def pause(self, run_id, position, reason, *, token=None):
        async with self.database.transaction() as session:
            run = await self._lock_workflow(session, run_id)
            if run.status in TERMINAL or run.cancel_requested or run.current_step != position:
                return
            step = await self._step(session, run_id, position)
            if token is not None and step.dispatch_token != token:
                return
            step.reason = reason
            _set_status(run, "PAUSED", reason)

    async def cancel(self, run_id, actor=None):
        """取消与子 Run 接纳竞争在同一 Run 锁下决出。"""
        async with self.database.transaction() as session:
            run = await self._lock_workflow(session, run_id)
            if run.space_id is not None:
                await WorkspaceService.require_write_access_in_session(session, actor, run.space_id)
            if run.status in TERMINAL:
                return None
            run.cancel_requested = True
            if run.approval_phase is not None:
                approval = await workflow_approval(session, run_id)
                if approval is not None and approval.status == "PENDING":
                    approval.status = "INVALIDATED"
            step = await self._step(session, run_id, run.current_step)
            if step.platform_run_id is not None:
                await lock_run_scope(session, step.platform_run_id)
                external = await session.get(ExternalRunRow, step.platform_run_id)
                control = await session.get(RunControlRow, step.platform_run_id)
                if external is not None or (control is not None and control.fencing_token > 0):
                    _set_status(run, "CANCELLING")
                    if not ((external is not None and external.terminal_status) or (control is not None and control.state == "CLOSED")):
                        reason = control.cancel_reason if control is not None else None
                        if reason is None:
                            await DurableRunControlService.request_cancel_locked(
                                session, step.platform_run_id, CancellationReason.REQUEST_CANCELLED.value,
                            )
                    return step
            step.status = "CANCELLED"
            step.dispatch_token = None
            _set_status(run, "CANCELLED")
            return None
