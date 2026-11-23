"""Workflow 单次步骤后决定的持久绑定。"""

from datetime import UTC, datetime

from sqlalchemy import select

from core.agent_platform.contracts import AFTER_STEP_OUTPUT
from core.persistence.models import WorkflowApprovalRow, WorkflowStepRunRow
from core.runtime.execution_aggregate import canonical_payload_digest


class WorkflowApprovalError(RuntimeError):
    def __init__(self, code: str, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


async def workflow_approval(session, run_id):
    return await session.scalar(select(WorkflowApprovalRow).where(WorkflowApprovalRow.workflow_run_id == run_id))


def approval_binding_digest(run, producer, native):
    return canonical_payload_digest({
        "workflow_run_id": run.workflow_run_id,
        "workflow_id": run.workflow_id,
        "workflow_version": run.workflow_version,
        "definition_digest": run.definition_digest,
        "tenant_id": run.tenant_id,
        "owner_user_id": str(run.owner_user_id),
        "approval_phase": run.approval_phase,
        "producer": {
            "step_id": producer.step_id,
            "attempt": producer.attempt,
            "platform_run_id": producer.platform_run_id,
            "result_ref": producer.result_ref,
            "artifact_ref": producer.artifact_ref,
            "artifact_directory": producer.artifact_directory,
        },
        "native": {
            "step_id": native.step_id,
            "attempt": native.attempt,
            "agent_id": native.agent_id,
            "agent_version": native.agent_version,
            "runtime_kind": native.runtime_kind,
            "bound_input": native.bound_input,
            "input_digest": native.input_digest,
        },
    })


async def verify_approval_binding(session, run, native, approval):
    producer = await session.scalar(select(WorkflowStepRunRow).where(
        WorkflowStepRunRow.workflow_run_id == run.workflow_run_id, WorkflowStepRunRow.position == 0,
    ))
    if (approval.approval_phase != AFTER_STEP_OUTPUT or approval.approval_phase != run.approval_phase
            or approval.workflow_run_id != run.workflow_run_id or approval.tenant_id != run.tenant_id
            or approval.owner_user_id != run.owner_user_id or run.current_step != 1
            or native is None or native.position != 1 or native.runtime_kind != "NATIVE"
            or native.bound_input is None or canonical_payload_digest(native.bound_input) != native.input_digest
            or producer is None or producer.step_id != approval.step_id or producer.attempt != approval.attempt
            or producer.status != "SUCCEEDED" or producer.runtime_status != "SUCCEEDED"
            or not producer.platform_run_id or not producer.result_ref or not producer.artifact_ref
            or approval.binding_digest != approval_binding_digest(run, producer, native)):
        raise WorkflowApprovalError("WORKFLOW_APPROVAL_BINDING_MISMATCH")


async def require_dispatch_approval(session, run, native):
    approval = await workflow_approval(session, run.workflow_run_id)
    if (approval is None or approval.status != "APPROVED" or approval.consumed_at is not None
            or approval.decided_by is None or approval.decided_at is None):
        raise WorkflowApprovalError("WORKFLOW_APPROVAL_REQUIRED")
    await verify_approval_binding(session, run, native, approval)
    return approval


def consume_approval(approval):
    approval.consumed_at = datetime.now(UTC)
