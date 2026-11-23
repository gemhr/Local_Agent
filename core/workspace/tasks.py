"""工作台任务接纳，复用现有 Run producer 与 WorkflowService。"""

from uuid import uuid4

from sqlalchemy import select

from core.agent_platform.application import AgentApplicationError, ExecutionRequest
from core.auth import AuthorizationAction, ObjectAuthorizationService
from core.chat_service import ChatRuntimeTransportError
from core.persistence.models import ClientDeliveryEventRow, ExternalRunRow, RunControlRow, WorkspaceAgentSubmissionRow
from core.persistence.repositories.runtime import lock_run_scope
from core.runtime.execution_aggregate import canonical_payload_digest
from core.runtime.memory_scope import MemoryAccessContext
from core.workspace.configuration import WorkspaceConfiguration, _check_revision
from core.workspace.service import WorkspaceError, WorkspaceService


class WorkspaceTasks:
    def __init__(self, database, application, workflow, authorization, supervisor, admission_gate):
        self.database = database
        self.application = application
        self.workflow = workflow
        self.authorization = authorization
        self.supervisor = supervisor
        self.admission_gate = admission_gate

    @staticmethod
    def _run_view(row):
        return {
            "kind": "AGENT", "run_id": row.run_id, "status": "ACCEPTED", "space_id": row.space_id,
            "events_url": f"/api/v1/runs/{row.run_id}/events",
        }

    @staticmethod
    async def _admitted(session, row) -> bool:
        if row.configuration["runtime_kind"] == "OPENCODE":
            return await session.get(ExternalRunRow, row.run_id) is not None
        control = await session.get(RunControlRow, row.run_id)
        if control is None or control.fencing_token == 0:
            return False
        # Native 的首次 fence 和 started 早于计划及执行根写入。
        return await session.scalar(select(ClientDeliveryEventRow.cursor).where(
            ClientDeliveryEventRow.run_id == row.run_id, ClientDeliveryEventRow.event_type == "run.started",
        ).limit(1)) is not None

    async def launch_workflow(self, principal, space_id, payload):
        run = await self.workflow.create(
            principal, payload.workflow_id, payload.request_id, payload.version, space_id=space_id,
            incident_id=str(payload.incident_id) if payload.incident_id is not None else None,
            execution_id=str(payload.execution_id) if payload.execution_id is not None else None,
        )
        run_id = run["workflow_run_id"]
        return {
            "kind": "WORKFLOW", "workflow_run_id": run_id, "status": run["status"],
            "space_id": space_id, "detail_url": f"/api/v1/workflow-runs/{run_id}",
        }

    async def launch_agent(self, principal, space_id, payload):
        request_digest = canonical_payload_digest({
            "space_id": str(space_id), **payload.model_dump(mode="json"),
        })
        slot = None
        stream = None
        try:
            async with self.database.transaction() as session:
                await lock_run_scope(session, f"workspace-agent:{principal.tenant_id}:{principal.user_id}:{payload.request_id}")
                await WorkspaceService.require_write_access_in_session(session, principal, space_id)
                prior = await session.scalar(select(WorkspaceAgentSubmissionRow).where(
                    WorkspaceAgentSubmissionRow.tenant_id == principal.tenant_id,
                    WorkspaceAgentSubmissionRow.owner_user_id == principal.user_id,
                    WorkspaceAgentSubmissionRow.request_id == payload.request_id,
                ))
                if prior is not None:
                    if prior.request_digest != request_digest:
                        raise WorkspaceError("WORKSPACE_REQUEST_CONFLICT", 409, "This request key is already in use.")
                    if not await self._admitted(session, prior):
                        raise WorkspaceError("WORKSPACE_ADMISSION_UNCONFIRMED", 409, "Execution admission is not confirmed.")
                    return self._run_view(prior)
                if not self.admission_gate.accepts_new_runs:
                    raise WorkspaceError("RUNTIME_SHUTTING_DOWN", 503, "Runtime is shutting down.")
                try:
                    request, configuration = await self._resolve_agent(session, principal, space_id, payload)
                except AgentApplicationError as exc:
                    raise WorkspaceError(exc.error_code, 422, exc.safe_message) from None
                slot = await self.supervisor.try_acquire_slot()
                if slot is None:
                    raise WorkspaceError("WORKSPACE_RUNTIME_BUSY", 503, "Runtime capacity is full. Retry this request.")
                access = MemoryAccessContext.from_principal(principal, request.agent_id, request.session_id)
                row = WorkspaceAgentSubmissionRow(
                    run_id=request.run_id, request_id=payload.request_id, space_id=space_id,
                    tenant_id=principal.tenant_id, owner_user_id=principal.user_id, request_digest=request_digest,
                    configuration=configuration,
                )
                await ObjectAuthorizationService.bind_new_in_transaction(
                    session, principal, "RUN", request.run_id, space_id=space_id,
                )
                await ObjectAuthorizationService.bind_new_in_transaction(
                    session, principal, "CONVERSATION", access.conversation_id, space_id=space_id,
                )
                session.add(row)
            await self.authorization.authorize(principal, "RUN", request.run_id, AuthorizationAction.PROCESS)
            if not self.admission_gate.accepts_new_runs:
                raise WorkspaceError("RUNTIME_SHUTTING_DOWN", 503, "Runtime is shutting down.")
            stream = self.application.stream(
                request, memory_access_context=access, retrieval_cache_authz_domain=principal.authz_domain_id,
                require_new_run=payload.runtime_kind == "NATIVE",
            )
            event = await anext(stream, None)
            async with self.database.session() as session:
                if (event is None or event.kind != "started" or event.run_id != request.run_id
                        or not await self._admitted(session, row)):
                    raise WorkspaceError("WORKSPACE_ADMISSION_UNCONFIRMED", 409, "Execution admission is not confirmed.")
            self.supervisor.spawn(request.run_id, stream, reserved_slot=slot)
            slot = stream = None
            return self._run_view(row)
        except (AgentApplicationError, ChatRuntimeTransportError) as exc:
            raise WorkspaceError(exc.error_code, 409 if exc.error_code == "RUN_ALREADY_ACCEPTED" else 503,
                                 "Execution admission failed.") from None
        finally:
            if slot is not None:
                self.supervisor.release_slot(slot)
            if stream is not None:
                await stream.aclose()

    async def _resolve_agent(self, session, principal, space_id, payload):
        snapshot = {"runtime_kind": payload.runtime_kind, "runtime_instance_id": payload.runtime_instance_id}
        if payload.config_id is not None:
            config = await WorkspaceConfiguration.agent_in_space(session, principal, space_id, payload.config_id)
            _check_revision(config.revision, payload.expected_revision)
            agent = self.application.workspace_text_agent(config.base_agent_id, config.base_agent_version)
            text = f"配置任务指令\n{config.instructions}\n\n本次任务说明\n{payload.input}"
            snapshot["agent_config"] = {
                **WorkspaceConfiguration.agent_view(config), "id": str(config.id),
                "space_id": str(config.space_id), "owner_user_id": str(config.owner_user_id),
            }
        else:
            agent = self.application.workspace_text_agent(payload.agent_id, payload.agent_version)
            text = payload.input
        if payload.runtime_instance_id is not None:
            snapshot["runtime_target_digest"] = self.application.require_workspace_instance(
                payload.runtime_instance_id, principal, "AGENT_TEXT",
            )
        snapshot.update(agent_id=agent.agent_id, agent_version=agent.agent_version, input_digest=canonical_payload_digest(text))
        run_id = str(uuid4())
        return ExecutionRequest(
            agent.agent_id, text, run_id=run_id, session_id=run_id,
            expected_agent_version=agent.agent_version, runtime_instance_id=payload.runtime_instance_id,
            timeout_seconds=payload.timeout_seconds,
        ), snapshot
