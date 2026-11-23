"""固定串行 Workflow 的应用入口。每次执行仍由原 Runtime 接纳。"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, replace
from uuid import UUID

from core.agent_platform.application import AgentApplicationError, ExecutionRequest
from core.agent_platform import workflow_artifacts as artifacts
from core.agent_platform.ci_status import AGENT_ID as CI_STATUS_AGENT
from core.agent_platform.contracts import AFTER_STEP_OUTPUT
from core.auth import AuthorizationAction, AuthError, require_owned
from core.chat_service import ChatRuntimeTransportError
from core.incidents.contracts import CIError
from core.persistence.repositories.workflows import TERMINAL
from core.persistence.repositories.workflow_approvals import WorkflowApprovalError
from core.runtime.cancellation import CancellationReason
from core.runtime.execution_aggregate import canonical_payload_digest
from core.runtime.memory_scope import MemoryAccessContext


logger = logging.getLogger(__name__)


class WorkflowError(RuntimeError):
    def __init__(self, code: str, status_code: int = 409):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class WorkflowService:
    def __init__(self, repository, workflows, application, authorization, supervisor, run_registry, admission_gate, *, ci_status=None):
        self._repository = repository
        self._workflows = workflows
        self._application = application
        self._authz = authorization
        self._supervisor = supervisor
        self._run_registry = run_registry
        self._admission_gate = admission_gate
        self._ci_status = ci_status
        self._workers: dict[str, asyncio.Task] = {}
        self._closed = False

    def _admitting(self):
        return not self._closed and self._admission_gate.accepts_new_runs

    @staticmethod
    def _request(task, session_id=None):
        return ExecutionRequest(
            task.agent_id, task.instruction, session_id=session_id,
            expected_agent_version=task.agent_version, runtime_instance_id=task.runtime_instance_id,
        )

    def _resolve(self, principal, workflow_id, expected_version=None):
        definition = self._workflows.get(workflow_id)
        if definition is None or definition.execution_mode != "SERIAL":
            raise WorkflowError("WORKFLOW_DEFINITION_UNAVAILABLE", 404)
        if expected_version is not None and definition.workflow_version != expected_version:
            raise WorkflowError("WORKFLOW_VERSION_MISMATCH")
        ci_binding = definition.result_binding == artifacts.CI_ARTIFACT_BINDING
        approval_steps = tuple(task.requires_approval for task in definition.tasks)
        if definition.approval_phase is None:
            if any(approval_steps):
                raise WorkflowError("WORKFLOW_STEP_CONSTRAINT_UNSUPPORTED", 422)
        elif not (ci_binding and definition.approval_phase == AFTER_STEP_OUTPUT and approval_steps == (True, False)):
            raise WorkflowError("WORKFLOW_STEP_CONSTRAINT_UNSUPPORTED", 422)
        if ci_binding:
            producer, consumer = definition.tasks
            if (self._ci_status is None or not producer.requires_result_validation or consumer.requires_result_validation
                    or consumer.agent_id != CI_STATUS_AGENT):
                raise WorkflowError("WORKFLOW_STEP_CONSTRAINT_UNSUPPORTED", 422)
        elif definition.result_binding is not None or any(task.requires_result_validation for task in definition.tasks):
            raise WorkflowError("WORKFLOW_STEP_CONSTRAINT_UNSUPPORTED", 422)
        tasks = []
        for task in definition.tasks:
            # Native 的 JSON 由已验证的上游输入填充，静态文本只是定义的一部分。
            pin = None if ci_binding and task.runtime_kind == "NATIVE" else self._application.validate_workflow_step(self._request(task), principal)
            tasks.append({**asdict(task), "capabilities": sorted(task.capabilities), "runtime_target": pin})
        digest = canonical_payload_digest({
            "workflow_id": workflow_id, "workflow_version": definition.workflow_version,
            "execution_mode": definition.execution_mode, "tasks": tasks,
            **({"result_binding": artifacts.CI_ARTIFACT_CONTRACT} if ci_binding else {}),
            **({"approval_phase": definition.approval_phase} if definition.approval_phase is not None else {}),
        })
        return definition, digest, tuple(task["runtime_target"] for task in tasks)

    async def create(self, principal, workflow_id, request_id: UUID, expected_version=None, *, incident_id=None, execution_id=None):
        await self._authz.require_current_human(principal)
        request_id = str(request_id)
        try:
            handoff_input = artifacts.initial_query(incident_id, execution_id) if incident_id is not None or execution_id is not None else None
        except artifacts.WorkflowArtifactError as exc:
            raise WorkflowError(exc.code, 422) from None
        run = await self._repository.find_request(principal, request_id)
        if run is None:
            if not self._admitting():
                raise WorkflowError("RUNTIME_SHUTTING_DOWN", 503)
            definition, digest, targets = self._resolve(principal, workflow_id, expected_version)
            if (definition.result_binding is not None) != (handoff_input is not None):
                raise WorkflowError("WORKFLOW_CI_INPUT_INVALID", 422)
            if handoff_input is not None:
                self._application.workflow_artifact_workspace(definition.tasks[0].runtime_instance_id, principal, targets[0])
                try:
                    await self._ci_status.workflow_route(principal, handoff_input)
                except CIError as exc:
                    raise WorkflowError(exc.code, exc.status) from None
            run, created = await self._repository.create(principal, request_id, definition, digest, handoff_input=handoff_input)
            if run is None:
                raise WorkflowError("WORKFLOW_DEFINITION_DRIFT")
            if created:
                await self._start(run.workflow_run_id, principal)
        if (run.workflow_id != workflow_id or run.handoff_input != handoff_input
                or (expected_version is not None and run.workflow_version != expected_version)):
            raise WorkflowError("WORKFLOW_REQUEST_CONFLICT")
        return await self._view(run.workflow_run_id)

    async def get(self, principal, run_id):
        await self._authorize(principal, run_id, AuthorizationAction.READ)
        return await self._view(run_id)

    async def decide_approval(self, principal, run_id, approval_id, decision, *, binding_digest=None):
        await self._authorize(principal, run_id, AuthorizationAction.APPROVE)
        try:
            approval = await self._repository.decide_approval(run_id, approval_id, principal, decision, binding_digest)
        except WorkflowApprovalError as exc:
            raise WorkflowError(exc.code, exc.status_code) from None
        return self._approval_view(approval)

    async def resume(self, principal, run_id):
        await self._authorize(principal, run_id, AuthorizationAction.READ)
        run, _, approval = await self._repository.snapshot(run_id)
        require_owned(principal, run.owner_user_id)
        if run.status == "WAITING_APPROVAL" and (approval is None or approval.status != "APPROVED"):
            return await self._view(run_id)
        if run.status not in TERMINAL and not run.cancel_requested:
            if not self._admitting():
                raise WorkflowError("RUNTIME_SHUTTING_DOWN", 503)
            await self._start(run_id, principal)
        return await self._view(run_id)

    async def cancel(self, principal, run_id):
        await self._authorize(principal, run_id, AuthorizationAction.CANCEL)
        step = await self._repository.cancel(run_id)
        if step is not None:
            evidence = await self._repository.evidence(step)
            if evidence.runtime_status is None and evidence.observation != "UNVERIFIED":
                local = self._run_registry.cancel(step.platform_run_id, CancellationReason.REQUEST_CANCELLED)
                if local is None and step.runtime_kind == "OPENCODE" and self._admitting():
                    try:
                        self._application.require_workflow_instance_access(step.runtime_instance_id, principal)
                    except (AuthError, AgentApplicationError):
                        pass  # 持久取消已提交，远端操作等待有权身份或原执行者处理。
                    else:
                        await self._application.cancel_detached_external(step.platform_run_id)
        return await self._view(run_id)

    async def _authorize(self, principal, run_id, action):
        await self._authz.require_current_human(principal)
        await self._authz.authorize(principal, "WORKFLOW_RUN", run_id, action)

    async def _view(self, run_id):
        run, steps, approval = await self._repository.snapshot(run_id)
        current = steps[run.current_step]
        evidence = await self._repository.evidence(current)
        if run.status == "CANCELLING" and evidence.runtime_status in TERMINAL:
            await self._repository.record(run_id, current, evidence)
            run, steps, approval = await self._repository.snapshot(run_id)
        frozen_ci_input = None
        if approval is not None and steps[1].bound_input == artifacts.freeze_native_input(run.handoff_input):
            frozen_ci_input = run.handoff_input
        return {
            "workflow_run_id": run.workflow_run_id,
            "workflow_id": run.workflow_id,
            "workflow_version": run.workflow_version,
            "status": run.status,
            "current_step": steps[run.current_step].step_id,
            "cancel_requested": run.cancel_requested,
            "reason": run.reason,
            "result_available": run.status == "SUCCEEDED" and steps[-1].result_ref is not None,
            **({
                "approval": self._approval_view(approval),
                "frozen_ci_input": frozen_ci_input,
                "requires_owner_resume": run.status in {"WAITING_APPROVAL", "PAUSED"}
                and approval.status == "APPROVED" and approval.consumed_at is None,
            } if approval is not None else {}),
            "steps": [{
                "step_id": step.step_id,
                "attempt": step.attempt,
                "status": step.status,
                "platform_run_id": step.platform_run_id,
                "runtime_kind": step.runtime_kind,
                "runtime_instance_id": step.runtime_instance_id,
                "runtime_status": step.runtime_status or (evidence.runtime_status if step.position == current.position else None),
                "runtime_observation": evidence.observation if step.position == current.position and step.platform_run_id else None,
                "reason": step.reason or (evidence.reason if step.position == current.position else None),
                "result_available": step.result_ref is not None,
                **({"artifact_ref": {key: value for key, value in step.artifact_ref.items() if key != "file_identity"}}
                   if step.artifact_ref is not None else {}),
            } for step in steps],
        }

    @staticmethod
    def _approval_view(approval):
        return {
            "approval_id": approval.approval_id,
            "step_id": approval.step_id,
            "attempt": approval.attempt,
            "approval_phase": approval.approval_phase,
            "status": approval.status,
            "binding_digest": approval.binding_digest,
            "decided_by": str(approval.decided_by) if approval.decided_by is not None else None,
            "decided_at": approval.decided_at,
            "created_at": approval.created_at,
            "consumed_at": approval.consumed_at,
        }

    async def _start(self, run_id, principal):
        if run_id in self._workers or not self._admitting():
            return
        slot = await self._supervisor.try_acquire_slot()
        if slot is None:
            return
        worker = self._drive(run_id, principal)
        try:
            task = asyncio.create_task(worker, name=f"workflow:{run_id}")
        except BaseException:
            worker.close()
            self._supervisor.release_slot(slot)
            raise
        self._workers[run_id] = task
        task.add_done_callback(lambda completed: self._worker_done(run_id, completed, slot))

    def _worker_done(self, run_id, task, slot):
        self._workers.pop(run_id, None)
        self._supervisor.release_slot(slot)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("Workflow 执行任务退出", extra={"workflow_run_id": run_id, "error_type": type(error).__name__})

    async def close(self):
        self._closed = True
        workers = tuple(self._workers.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def _drive(self, run_id, principal):
        while self._admitting():
            run, steps = await self._repository.read(run_id)
            if run.status in TERMINAL or run.cancel_requested:
                return
            step = steps[run.current_step]
            workspace = None
            prepare_artifact = None
            try:
                await self._authz.require_current_human(principal)
                require_owned(principal, run.owner_user_id)
                definition, digest, runtime_targets = self._resolve(principal, run.workflow_id, run.workflow_version)
                if digest != run.definition_digest or definition.approval_phase != run.approval_phase:
                    raise WorkflowError("WORKFLOW_DEFINITION_DRIFT")
                request = self._request(definition.tasks[step.position], session_id=f"{run_id}:{step.step_id}")
                if definition.result_binding is not None:
                    workspace = self._application.workflow_artifact_workspace(
                        definition.tasks[0].runtime_instance_id, principal, runtime_targets[0],
                    )
                    if step.position == 0:
                        request = replace(request, input=artifacts.producer_input(request.input, run))

                        def prepare_artifact(first, directory):
                            return artifacts.prepare_output(workspace, run_id, first=first, expected_directory=directory)
                    else:
                        if step.bound_input is None:
                            raise WorkflowError("WORKFLOW_BOUND_INPUT_INVALID")
                        request = replace(request, input=step.bound_input)
                        if step.platform_run_id is None:
                            artifacts.verify_frozen_input(workspace, run, steps[0], request.input)
            except WorkflowError as exc:
                await self._repository.pause(run_id, step.position, exc.code)
                return
            except (AgentApplicationError, AuthError, artifacts.WorkflowArtifactError) as exc:
                await self._repository.pause(run_id, step.position, self._error_code(exc))
                return

            access = MemoryAccessContext.from_principal(principal, request.agent_id, request.session_id)
            if not self._admitting():
                return
            try:
                prepared = await self._repository.prepare(
                    run_id, step.position, principal, access.conversation_id,
                    definition_digest=digest, runtime_target_digest=runtime_targets[step.position],
                    effective_input=request.input, prepare_artifact=prepare_artifact,
                )
            except (artifacts.WorkflowArtifactError, WorkflowApprovalError) as exc:
                await self._repository.pause(run_id, step.position, exc.code)
                return
            error = None
            if prepared is not None:
                step = prepared
                if not self._admitting():
                    return
                try:
                    await self._authz.require_current_human(principal)
                    await self._authz.authorize(principal, "RUN", step.platform_run_id, AuthorizationAction.PROCESS)
                    route = None
                    if workspace is not None:
                        if step.position == 0:
                            artifacts.prepare_output(workspace, run_id, first=False, expected_directory=step.artifact_directory)
                        else:
                            route = await self._ci_status.workflow_route(principal, json.loads(request.input))
                            artifacts.verify_frozen_input(workspace, run, steps[0], request.input)
                    await self._application.execute(
                        replace(request, run_id=step.platform_run_id), memory_access_context=access,
                        retrieval_cache_authz_domain=principal.authz_domain_id, require_new_run=True,
                        workflow_dispatch_token=step.dispatch_token,
                        **({"adaptive_route": route} if route is not None else {}),
                    )
                except (AgentApplicationError, AuthError, ChatRuntimeTransportError, CIError, artifacts.WorkflowArtifactError) as exc:
                    error = self._error_code(exc)
            elif step.platform_run_id is None:
                # 另一个事务可能刚完成预留，使用新快照进行观察。
                current, current_steps = await self._repository.read(run_id)
                if current.current_step != step.position:
                    continue
                step = current_steps[step.position]
                if step.platform_run_id is None:
                    return

            evidence = await self._repository.evidence(step)
            if (prepared is None and step.runtime_kind == "OPENCODE" and evidence.observation in {"ACTIVE", "UNKNOWN"}
                    and self._admitting() and self._run_registry.get(step.platform_run_id) is None):
                await self._application.read_external_result(step.platform_run_id, mark_unknown=False)
                evidence = await self._repository.evidence(step)
            if evidence.runtime_status in TERMINAL:
                if workspace is not None and evidence.runtime_status == "SUCCEEDED" and evidence.result_ref:
                    if step.position == 0:
                        evidence = await self._validate_ci_output(principal, run, step, evidence, workspace)
                    else:
                        try:
                            artifacts.verify_native_output(step.bound_input, evidence.native_output)
                        except artifacts.WorkflowArtifactError as exc:
                            evidence = replace(evidence, validation_error=exc.code)
                if await self._repository.record(run_id, step, evidence):
                    continue
            else:
                reason = evidence.reason or error or f"WORKFLOW_RUN_{evidence.observation}"
                await self._repository.pause(run_id, step.position, reason, token=step.dispatch_token)
            return

    @staticmethod
    def _error_code(exc):
        return exc.code if isinstance(exc, (AuthError, CIError, artifacts.WorkflowArtifactError)) else exc.error_code

    async def _validate_ci_output(self, principal, run, step, evidence, workspace):
        try:
            await self._authz.require_current_human(principal)
            await self._authz.authorize(principal, "RUN", step.platform_run_id, AuthorizationAction.READ)
            outcome = await self._application.read_external_result(step.platform_run_id, mark_unknown=False)
            ref = evidence.result_ref
            if (outcome is None or outcome["status"] != "SUCCEEDED" or outcome["read_error_code"]
                    or outcome["run_id"] != step.platform_run_id or outcome["runtime_instance_id"] != ref["instance_id"]
                    or outcome["external_session_id"] != ref["session_id"] or outcome["user_message_id"] != ref["user_message_id"]
                    or outcome["assistant_message_id"] != ref["message_id"] or outcome["output"] is None):
                raise artifacts.WorkflowArtifactError("WORKFLOW_RESULT_UNAVAILABLE", retryable=True)
            artifact_ref, query = artifacts.validate_output(workspace, run, step, outcome["output"])
            await self._ci_status.workflow_route(principal, query)
            return replace(evidence, artifact_ref=artifact_ref, next_input=artifacts.freeze_native_input(query))
        except artifacts.WorkflowArtifactError as exc:
            return replace(evidence, validation_error=exc.code, validation_retryable=exc.retryable)
        except AuthError as exc:
            return replace(evidence, validation_error=exc.code, validation_retryable=True)
        except CIError as exc:
            return replace(evidence, validation_error=exc.code)
