"""固定串行 Workflow 的应用入口。每次执行仍由原 Runtime 接纳。"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, replace
from uuid import UUID

from sqlalchemy import select

from core.agent_platform.application import AgentApplicationError, ExecutionRequest
from core.agent_platform import workflow_artifacts as artifacts
from core.agent_platform import ci_repair_artifacts as repair
from core.agent_platform.ci_status import AGENT_ID as CI_STATUS_AGENT
from core.agent_platform.contracts import AFTER_STEP_OUTPUT, CI_REPAIR_BINDING, WorkflowDefinition, WorkflowTask
from core.auth import AuthorizationAction, AuthError, require_owned
from core.chat_service import ChatRuntimeTransportError
from core.ci_simulation import tlv_input
from core.incidents.contracts import CIError
from core.incidents.models import InvestigationRow
from core.persistence.repositories.workflows import TERMINAL
from core.persistence.repositories.workflow_approvals import WorkflowApprovalError
from core.runtime.cancellation import CancellationReason
from core.runtime.execution_aggregate import canonical_payload_digest
from core.runtime.memory_scope import MemoryAccessContext
from core.runtime.snapshot_serialization import canonical_json
from core.workspace.service import WorkspaceService


logger = logging.getLogger(__name__)


class WorkflowError(RuntimeError):
    def __init__(self, code: str, status_code: int = 409):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class WorkflowService:
    def __init__(self, repository, workflows, application, authorization, supervisor, run_registry, admission_gate,
                 *, ci_status=None, ci_investigation=None):
        self._repository = repository
        self._workflows = workflows
        self._application = application
        self._authz = authorization
        self._supervisor = supervisor
        self._run_registry = run_registry
        self._admission_gate = admission_gate
        self._ci_status = ci_status
        self._ci_investigation = ci_investigation
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
        return self._resolve_definition(principal, definition)

    def _resolve_definition(self, principal, definition, *, published=False):
        ci_binding = definition.result_binding == artifacts.CI_ARTIFACT_BINDING
        repair_binding = definition.result_binding == CI_REPAIR_BINDING
        approval_steps = tuple(task.requires_approval for task in definition.tasks)
        if definition.approval_phase is None:
            if any(approval_steps):
                raise WorkflowError("WORKFLOW_STEP_CONSTRAINT_UNSUPPORTED", 422)
        elif not (definition.approval_phase == AFTER_STEP_OUTPUT and (
            ci_binding and approval_steps == (True, False) or repair_binding and approval_steps == (False, True, False)
        )):
            raise WorkflowError("WORKFLOW_STEP_CONSTRAINT_UNSUPPORTED", 422)
        if ci_binding:
            producer, consumer = definition.tasks
            if (self._ci_status is None or not producer.requires_result_validation or consumer.requires_result_validation
                    or consumer.agent_id != CI_STATUS_AGENT):
                raise WorkflowError("WORKFLOW_STEP_CONSTRAINT_UNSUPPORTED", 422)
        elif not repair_binding and (definition.result_binding is not None or any(task.requires_result_validation for task in definition.tasks)):
            raise WorkflowError("WORKFLOW_STEP_CONSTRAINT_UNSUPPORTED", 422)
        tasks = []
        for task in definition.tasks:
            # Native 的 JSON 由已验证的上游输入填充，静态文本只是定义的一部分。
            pin = None if task.runtime_kind == "REPLAY" or ci_binding and task.runtime_kind == "NATIVE" else (
                self._application.validate_workflow_step(self._request(task), principal))
            if published and task.runtime_kind == "OPENCODE":
                purpose = "WORKFLOW_CI_REPAIR" if repair_binding else "WORKFLOW_CI_ARTIFACT" if ci_binding else "WORKFLOW_TEXT"
                self._application.require_workspace_instance(task.runtime_instance_id, principal, purpose)
            if published and ci_binding and task.runtime_kind == "NATIVE":
                self._application.validate_agent_version(task.agent_id, task.agent_version)
            tasks.append({**asdict(task), "capabilities": sorted(task.capabilities), "runtime_target": pin})
        digest = canonical_payload_digest({
            "workflow_id": definition.workflow_id, "workflow_version": definition.workflow_version,
            "execution_mode": definition.execution_mode, "tasks": tasks,
            **({"result_binding": artifacts.CI_ARTIFACT_CONTRACT} if ci_binding else {}),
            **({"result_binding": {"binding": CI_REPAIR_BINDING, "input": "repair-input.v1",
                                  "artifact": repair.SCHEMA}} if repair_binding else {}),
            **({"approval_phase": definition.approval_phase} if definition.approval_phase is not None else {}),
        })
        return definition, digest, tuple(task["runtime_target"] for task in tasks)

    def publication(self, principal, definition):
        _, digest, targets = self._resolve_definition(principal, definition, published=True)
        if definition.result_binding == CI_REPAIR_BINDING:
            self._application.workflow_repair_repository(definition.tasks[1].runtime_instance_id, principal, targets[1])
        elif definition.result_binding is not None:
            self._application.workflow_artifact_workspace(definition.tasks[0].runtime_instance_id, principal, targets[0])
        snapshot = asdict(definition)
        snapshot["tasks"] = [{**asdict(task), "capabilities": sorted(task.capabilities)} for task in definition.tasks]
        return digest, {"definition": snapshot, "runtime_targets": list(targets)}

    async def _resolve_published(self, principal, space_id, workflow_id, version):
        row = await self._repository.published_version(principal, space_id, workflow_id, version)
        if row is None:
            raise WorkflowError("WORKFLOW_PUBLISHED_VERSION_UNAVAILABLE", 404)
        saved = row.definition_snapshot
        try:
            snapshot = saved["definition"]
            tasks = tuple(WorkflowTask(**{**item, "capabilities": frozenset(item["capabilities"])}) for item in snapshot["tasks"])
            definition = WorkflowDefinition(**{**snapshot, "tasks": tasks})
        except (KeyError, TypeError, ValueError):
            raise WorkflowError("WORKFLOW_DEFINITION_DRIFT") from None
        if (definition.workflow_id, definition.workflow_version) != (workflow_id, version):
            raise WorkflowError("WORKFLOW_DEFINITION_DRIFT")
        if definition.execution_mode != "SERIAL":
            raise WorkflowError("WORKFLOW_DEFINITION_DRIFT")
        exact, digest, targets = self._resolve_definition(principal, definition, published=True)
        if digest != row.definition_digest or list(targets) != saved.get("runtime_targets"):
            raise WorkflowError("WORKFLOW_DEFINITION_DRIFT")
        return exact, digest, targets

    async def create(self, principal, workflow_id, request_id: UUID, expected_version=None, *, incident_id=None, execution_id=None, space_id=None):
        await self._authz.require_current_human(principal)
        if space_id is not None:
            async with self._repository.database.session() as session:
                await WorkspaceService.require_access_in_session(session, principal, space_id)
        request_id = str(request_id)
        try:
            handoff_input = artifacts.initial_query(incident_id, execution_id) if incident_id is not None or execution_id is not None else None
        except artifacts.WorkflowArtifactError as exc:
            raise WorkflowError(exc.code, 422) from None
        run = await self._repository.find_request(principal, request_id)
        if run is None:
            if not self._admitting():
                raise WorkflowError("RUNTIME_SHUTTING_DOWN", 503)
            if space_id is None:
                definition, digest, targets = self._resolve(principal, workflow_id, expected_version)
            else:
                definition, digest, targets = await self._resolve_published(principal, space_id, workflow_id, expected_version)
            if (definition.result_binding is not None) != (handoff_input is not None):
                raise WorkflowError("WORKFLOW_CI_INPUT_INVALID", 422)
            baseline = None
            if definition.result_binding == CI_REPAIR_BINDING:
                if space_id is None or self._ci_investigation is None or not self._ci_investigation.real_model:
                    raise WorkflowError("CI_REPAIR_WORKSPACE_REQUIRED", 422)
                try:
                    async with self._repository.database.session() as session:
                        incident, execution, instance = await self._ci_investigation.incidents.resolution_scope(
                            session, principal, incident_id)
                    if (incident.status != "OPEN" or execution.execution_id != execution_id
                            or execution.case_id != tlv_input.PRIMARY_CASE or execution.outcome != "FAIL"
                            or instance.scenario_key != tlv_input.SCENARIO_KEY
                            or not tlv_input.is_file_execution(execution.execution_snapshot)):
                        raise WorkflowError("CI_REPAIR_TARGET_NOT_ALLOWED", 422)
                    instance_id = definition.tasks[1].runtime_instance_id
                    root = self._application.workflow_repair_repository(instance_id, principal, targets[1])
                    baseline = await asyncio.to_thread(repair.capture_baseline, root, instance_id)
                except CIError as exc:
                    raise WorkflowError(exc.code, exc.status) from None
                except artifacts.WorkflowArtifactError as exc:
                    raise WorkflowError(exc.code) from None
            elif handoff_input is not None:
                self._application.workflow_artifact_workspace(definition.tasks[0].runtime_instance_id, principal, targets[0])
                try:
                    await self._ci_status.workflow_route(principal, handoff_input)
                except CIError as exc:
                    raise WorkflowError(exc.code, exc.status) from None
            try:
                run, created = await self._repository.create(
                    principal, request_id, definition, digest, handoff_input=handoff_input, space_id=space_id,
                    **({"repair_baseline": baseline} if baseline is not None else {}),
                )
            except artifacts.WorkflowArtifactError as exc:
                raise WorkflowError(exc.code) from None
            if run is None:
                raise WorkflowError("WORKFLOW_DEFINITION_DRIFT")
            if created:
                await self._start(run.workflow_run_id, principal)
        if (run.workflow_id != workflow_id or run.space_id != space_id or run.handoff_input != handoff_input
                or (expected_version is not None and run.workflow_version != expected_version)):
            raise WorkflowError("WORKFLOW_REQUEST_CONFLICT")
        return await self._view(run.workflow_run_id)

    async def get(self, principal, run_id):
        await self._authorize(principal, run_id, AuthorizationAction.READ, share_workspace_read=True)
        return await self._view(run_id)

    async def decide_approval(self, principal, run_id, approval_id, decision, *, binding_digest=None, diff_digest=None):
        await self._authorize(principal, run_id, AuthorizationAction.APPROVE)
        try:
            approval = await self._repository.decide_approval(run_id, approval_id, principal, decision, binding_digest, diff_digest)
        except WorkflowApprovalError as exc:
            raise WorkflowError(exc.code, exc.status_code) from None
        return self._approval_view(approval)

    async def repair_artifact(self, principal, run_id):
        await self._authorize(principal, run_id, AuthorizationAction.READ, share_workspace_read=True)
        run, steps, approval = await self._repository.snapshot(run_id)
        if run.repair_baseline is None or approval is None:
            raise WorkflowError("CI_REPAIR_ARTIFACT_UNAVAILABLE", 404)
        artifact = steps[1].artifact_ref
        try:
            await asyncio.to_thread(repair.verify_frozen, artifact)
        except artifacts.WorkflowArtifactError as exc:
            raise WorkflowError(exc.code) from None
        from core.persistence.repositories.workflow_approvals import approval_binding_digest

        if approval.binding_digest != approval_binding_digest(run, steps[1], steps[2]):
            raise WorkflowError("WORKFLOW_APPROVAL_BINDING_MISMATCH")
        return artifact

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
        step = await self._repository.cancel(run_id, principal)
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

    async def _authorize(self, principal, run_id, action, *, share_workspace_read=False):
        await self._authz.require_current_human(principal)
        await self._authz.authorize(principal, "WORKFLOW_RUN", run_id, action, share_workspace_read=share_workspace_read)

    async def _view(self, run_id):
        run, steps, approval = await self._repository.snapshot(run_id)
        current = steps[run.current_step]
        evidence = await self._repository.evidence(current)
        if run.status == "CANCELLING" and evidence.runtime_status in TERMINAL:
            await self._repository.record(run_id, current, evidence)
            run, steps, approval = await self._repository.snapshot(run_id)
        frozen_ci_input = None
        if run.repair_baseline is None and approval is not None and steps[1].bound_input == artifacts.freeze_native_input(run.handoff_input):
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
            **({"repair_context": run.handoff_input,
                "repair_diagnosis": (steps[0].result_ref or {}).get("diagnosis"),
                "repair_artifact_available": approval is not None and steps[1].artifact_ref is not None,
                "repair_replay": (steps[2].result_ref or {}).get("receipt"),
                "repair_evidence_digest": (steps[2].result_ref or {}).get("evidence_digest")}
               if run.repair_baseline is not None else {}),
            **({
                "approval": self._approval_view(approval),
                "frozen_ci_input": frozen_ci_input,
                "requires_owner_resume": run.status in {"WAITING_APPROVAL", "PAUSED"}
                and approval.status == "APPROVED" and approval.consumed_at is None,
            } if approval is not None else {}),
            "steps": [{
                "step_id": step.step_id,
                "attempt": step.attempt,
                "position": step.position,
                "agent_id": step.agent_id,
                "agent_version": step.agent_version,
                "status": step.status,
                "platform_run_id": step.platform_run_id,
                "runtime_kind": step.runtime_kind,
                "runtime_instance_id": step.runtime_instance_id,
                "runtime_status": step.runtime_status or (evidence.runtime_status if step.position == current.position else None),
                "runtime_observation": evidence.observation if step.position == current.position and step.platform_run_id else None,
                "reason": step.reason or (evidence.reason if step.position == current.position else None),
                "result_available": step.result_ref is not None,
                **({"artifact_ref": {key: step.artifact_ref[key] for key in (
                    "workflow_run_id", "step_id", "attempt", "producer_run_id", "runtime_instance_id",
                    "relative_path", "size", "sha256",
                ) if key in step.artifact_ref}}
                   if step.artifact_ref is not None and run.repair_baseline is None else {}),
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
            bind_investigation = None
            try:
                await self._authz.require_current_human(principal)
                require_owned(principal, run.owner_user_id)
                if run.space_id is None:
                    definition, digest, runtime_targets = self._resolve(principal, run.workflow_id, run.workflow_version)
                else:
                    definition, digest, runtime_targets = await self._resolve_published(
                        principal, run.space_id, run.workflow_id, run.workflow_version,
                    )
                if digest != run.definition_digest or definition.approval_phase != run.approval_phase:
                    raise WorkflowError("WORKFLOW_DEFINITION_DRIFT")
                request = self._request(definition.tasks[step.position], session_id=f"{run_id}:{step.step_id}")
                if definition.result_binding == CI_REPAIR_BINDING:
                    if run.repair_baseline is None:
                        raise WorkflowError("CI_REPAIR_BASELINE_DRIFT")
                    self._application.workflow_repair_repository(definition.tasks[1].runtime_instance_id, principal, runtime_targets[1])
                    if step.runtime_kind == "REPLAY":
                        await self._repository.complete_replay(run_id, principal)
                        return
                    if step.position == 0:
                        origin = run.handoff_input
                        request = replace(request, input=request.input + "\nincident_id=" + origin["incident_id"]
                            + " execution_id=" + origin["execution_id"]
                            + "\n本任务使用文件场景的扩展报告合同。若证据支持 TLV 旧版本导致预检失败，"
                            + 'FINAL JSON 必须包含独立顶层字段 "failure_mechanism": "TLV_INPUT_REVISION_STALE"，'
                            + "保留原报告字段，不能仅在假设正文中提及故障机制。")

                        async def bind_investigation(session, platform_id):
                            await self._ci_investigation.incidents.admit_in_transaction(
                                session, principal, origin["incident_id"], run_id,
                                run_id=platform_id, session_id=request.session_id,
                            )
                    else:
                        repair.require_input(step.bound_input, (steps[0].result_ref or {}).get("repair_input_digest"))
                        request = replace(request, input=step.bound_input)
                elif definition.result_binding is not None:
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
                    **({"bind_investigation": bind_investigation} if bind_investigation is not None else {}),
                )
            except (artifacts.WorkflowArtifactError, WorkflowApprovalError, AuthError, CIError) as exc:
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
                if run.repair_baseline is not None and evidence.runtime_status == "SUCCEEDED" and evidence.result_ref:
                    evidence = await self._repair_output(principal, run, step, evidence)
                elif workspace is not None and evidence.runtime_status == "SUCCEEDED" and evidence.result_ref:
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

    async def _repair_output(self, principal, run, step, evidence):
        try:
            await self._authz.require_current_human(principal)
            await self._authz.authorize(principal, "RUN", step.platform_run_id, AuthorizationAction.READ)
            if step.position == 0:
                async with self._repository.database.session() as session:
                    investigation_id = await session.scalar(select(InvestigationRow.investigation_id).where(
                        InvestigationRow.run_id == step.platform_run_id,
                        InvestigationRow.incident_id == run.handoff_input["incident_id"],
                        InvestigationRow.session_id == f"{run.workflow_run_id}:{step.step_id}",
                    ))
                if investigation_id is None:
                    raise artifacts.WorkflowArtifactError("CI_REPAIR_IDENTITY_INVALID")
                row, _ = await self._ci_investigation.project_if_terminal(investigation_id)
                dto = await self._ci_investigation.repair_input(principal, run.handoff_input["incident_id"], investigation_id)
                if dto.execution_id != run.handoff_input["execution_id"] or dto.run_id != step.platform_run_id:
                    raise artifacts.WorkflowArtifactError("CI_REPAIR_IDENTITY_INVALID")
                raw = dto.canonical_bytes().decode("utf-8")
                repair.require_input(raw, dto.digest())
                report = row.report_payload
                result_ref = dict(evidence.result_ref, repair_input_digest=dto.digest(),
                    diagnosis={key: report[key] for key in ("diagnosis_status", "root_cause_hypothesis", "failure_mechanism")})
                return replace(evidence, result_ref=result_ref, next_input=raw)
            await self._read_confirmed_external(step, evidence.result_ref)
            artifact = await asyncio.to_thread(repair.freeze_patch, run, step, evidence.result_ref)
            replay = canonical_json({key: artifact[key] for key in (
                "schema_version", "workflow_run_id", "artifact_digest", "diff_digest", "baseline_revision",
            )})
            return replace(evidence, artifact_ref=artifact, next_input=replay)
        except artifacts.WorkflowArtifactError as exc:
            return replace(evidence, validation_error=exc.code, validation_retryable=exc.retryable)
        except AuthError as exc:
            return replace(evidence, validation_error=exc.code, validation_retryable=True)
        except CIError as exc:
            return replace(evidence, validation_error=exc.code)

    async def _read_confirmed_external(self, step, ref):
        outcome = await self._application.read_external_result(step.platform_run_id, mark_unknown=False)
        if (outcome is None or outcome["status"] != "SUCCEEDED" or outcome["read_error_code"]
                or outcome["run_id"] != step.platform_run_id or outcome["runtime_instance_id"] != ref["instance_id"]
                or outcome["external_session_id"] != ref["session_id"] or outcome["user_message_id"] != ref["user_message_id"]
                or outcome["assistant_message_id"] != ref["message_id"] or outcome["output"] is None):
            raise artifacts.WorkflowArtifactError("WORKFLOW_RESULT_UNAVAILABLE", retryable=True)
        return outcome

    async def _validate_ci_output(self, principal, run, step, evidence, workspace):
        try:
            await self._authz.require_current_human(principal)
            await self._authz.authorize(principal, "RUN", step.platform_run_id, AuthorizationAction.READ)
            outcome = await self._read_confirmed_external(step, evidence.result_ref)
            artifact_ref, query = artifacts.validate_output(workspace, run, step, outcome["output"])
            await self._ci_status.workflow_route(principal, query)
            return replace(evidence, artifact_ref=artifact_ref, next_input=artifacts.freeze_native_input(query))
        except artifacts.WorkflowArtifactError as exc:
            return replace(evidence, validation_error=exc.code, validation_retryable=exc.retryable)
        except AuthError as exc:
            return replace(evidence, validation_error=exc.code, validation_retryable=True)
        except CIError as exc:
            return replace(evidence, validation_error=exc.code)
