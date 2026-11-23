"""固定串行 Workflow 的应用入口。每次执行仍由原 Runtime 接纳。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, replace
from uuid import UUID

from core.agent_platform.application import AgentApplicationError, ExecutionRequest
from core.auth import AuthorizationAction, AuthError, require_owned
from core.chat_service import ChatRuntimeTransportError
from core.persistence.repositories.workflows import TERMINAL
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
    def __init__(self, repository, workflows, application, authorization, supervisor, run_registry, admission_gate):
        self._repository = repository
        self._workflows = workflows
        self._application = application
        self._authz = authorization
        self._supervisor = supervisor
        self._run_registry = run_registry
        self._admission_gate = admission_gate
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
        if any(task.requires_result_validation or task.requires_approval for task in definition.tasks):
            raise WorkflowError("WORKFLOW_STEP_CONSTRAINT_UNSUPPORTED", 422)
        tasks = []
        for task in definition.tasks:
            pin = self._application.validate_workflow_step(self._request(task), principal)
            tasks.append({**asdict(task), "capabilities": sorted(task.capabilities), "runtime_target": pin})
        digest = canonical_payload_digest({
            "workflow_id": workflow_id, "workflow_version": definition.workflow_version,
            "execution_mode": definition.execution_mode, "tasks": tasks,
        })
        return definition, digest, tuple(task["runtime_target"] for task in tasks)

    async def create(self, principal, workflow_id, request_id: UUID, expected_version=None):
        await self._authz.require_current_human(principal)
        request_id = str(request_id)
        run = await self._repository.find_request(principal, request_id)
        if run is None:
            if not self._admitting():
                raise WorkflowError("RUNTIME_SHUTTING_DOWN", 503)
            definition, digest, _ = self._resolve(principal, workflow_id, expected_version)
            run, created = await self._repository.create(principal, request_id, definition, digest)
            if run is None:
                raise WorkflowError("WORKFLOW_DEFINITION_DRIFT")
            if created:
                await self._start(run.workflow_run_id, principal)
        if run.workflow_id != workflow_id or (expected_version is not None and run.workflow_version != expected_version):
            raise WorkflowError("WORKFLOW_REQUEST_CONFLICT")
        return await self._view(run.workflow_run_id)

    async def get(self, principal, run_id):
        await self._authorize(principal, run_id, AuthorizationAction.READ)
        return await self._view(run_id)

    async def resume(self, principal, run_id):
        await self._authorize(principal, run_id, AuthorizationAction.READ)
        run, _ = await self._repository.read(run_id)
        require_owned(principal, run.owner_user_id)
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
        run, steps = await self._repository.read(run_id)
        current = steps[run.current_step]
        evidence = await self._repository.evidence(current)
        if run.status == "CANCELLING" and evidence.runtime_status in TERMINAL:
            await self._repository.record(run_id, current, evidence)
            run, steps = await self._repository.read(run_id)
        return {
            "workflow_run_id": run.workflow_run_id,
            "workflow_id": run.workflow_id,
            "workflow_version": run.workflow_version,
            "status": run.status,
            "current_step": steps[run.current_step].step_id,
            "cancel_requested": run.cancel_requested,
            "reason": run.reason,
            "result_available": run.status == "SUCCEEDED" and steps[-1].result_ref is not None,
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
            } for step in steps],
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
            try:
                await self._authz.require_current_human(principal)
                require_owned(principal, run.owner_user_id)
                definition, digest, runtime_targets = self._resolve(principal, run.workflow_id, run.workflow_version)
                if digest != run.definition_digest:
                    raise WorkflowError("WORKFLOW_DEFINITION_DRIFT")
            except WorkflowError as exc:
                await self._repository.pause(run_id, step.position, exc.code)
                return
            except (AgentApplicationError, AuthError) as exc:
                await self._repository.pause(run_id, step.position, self._error_code(exc))
                return

            request = self._request(definition.tasks[step.position], session_id=f"{run_id}:{step.step_id}")
            access = MemoryAccessContext.from_principal(principal, request.agent_id, request.session_id)
            if not self._admitting():
                return
            prepared = await self._repository.prepare(
                run_id, step.position, principal, access.conversation_id,
                definition_digest=digest, runtime_target_digest=runtime_targets[step.position],
            )
            error = None
            if prepared is not None:
                step = prepared
                if not self._admitting():
                    return
                try:
                    await self._authz.require_current_human(principal)
                    await self._authz.authorize(principal, "RUN", step.platform_run_id, AuthorizationAction.PROCESS)
                    await self._application.execute(
                        replace(request, run_id=step.platform_run_id), memory_access_context=access,
                        retrieval_cache_authz_domain=principal.authz_domain_id, require_new_run=True,
                        workflow_dispatch_token=step.dispatch_token,
                    )
                except (AgentApplicationError, AuthError, ChatRuntimeTransportError) as exc:
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
                if await self._repository.record(run_id, step, evidence):
                    continue
            else:
                reason = evidence.reason or error or f"WORKFLOW_RUN_{evidence.observation}"
                await self._repository.pause(run_id, step.position, reason, token=step.dispatch_token)
            return

    @staticmethod
    def _error_code(exc):
        return exc.code if isinstance(exc, AuthError) else exc.error_code
