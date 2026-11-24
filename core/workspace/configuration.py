"""工作台定义的编辑和发布，执行合同由原 Agent / Workflow 服务解析。"""

from dataclasses import replace
from uuid import uuid4

from sqlalchemy import select

from core.agent_platform.contracts import CI_REPAIR_BINDING
from core.auth import AUTHORIZATION_OBJECT_NOT_OWNED, AuthError
from core.persistence.models import WorkspaceAgentRow, WorkspaceWorkflowRow, WorkflowVersionRow
from core.workspace.service import WorkspaceError, WorkspaceService


def _check_revision(current: int, expected: int) -> None:
    if current != expected:
        raise WorkspaceError("WORKSPACE_REVISION_CONFLICT", 409, "The configuration has changed. Reload before editing.")


class WorkspaceConfiguration:
    def __init__(self, database, application, workflow, templates):
        self.database = database
        self.application = application
        self.workflow = workflow
        self.templates = {key: item for key, item in templates.items() if key in {
            "runtime_handoff", "ci_artifact_handoff", "ci_artifact_review", "ci_repair_review",
        }}

    async def get_catalog(self, principal, space_id):
        async with self.database.session() as session:
            await WorkspaceService.require_access_in_session(session, principal, space_id)
        agent = self.application.workspace_text_agent()
        return {
            "agents": [{
                "id": agent.agent_id, "version": agent.agent_version, "name": agent.display_name,
                "runtime_kinds": ["NATIVE", "OPENCODE"], "input_type": "text",
                "output_type": "text", "supports_configuration": True,
            }],
            "templates": [self._template_view(item) for item in self.templates.values()],
        }

    async def get_runtimes(self, principal, space_id):
        async with self.database.session() as session:
            await WorkspaceService.require_access_in_session(session, principal, space_id)
        approved_artifact_instances = {self._template_view(item)["fixed_runtime_instance_id"] for item in self.templates.values()}
        return [
            item for item in self.application.workspace_runtime_instances(principal)
            if not {"WORKFLOW_CI_ARTIFACT", "WORKFLOW_CI_REPAIR"}.intersection(item["purposes"])
            or item["id"] in approved_artifact_instances
        ]

    @staticmethod
    def _template_view(template):
        ci = template.result_binding is not None
        repair = template.result_binding == CI_REPAIR_BINDING
        external = next(task for task in template.tasks if task.runtime_kind == "OPENCODE")
        return {
            "id": template.workflow_id, "version": template.workflow_version,
            "instructions_editable": not ci, "requires_ci_input": ci, "approval_phase": template.approval_phase,
            "runtime_purpose": "WORKFLOW_CI_REPAIR" if repair else "WORKFLOW_CI_ARTIFACT" if ci else "WORKFLOW_TEXT",
            "fixed_runtime_instance_id": external.runtime_instance_id if ci else None,
            "steps": [{
                "id": task.task_id, "agent_id": task.agent_id, "agent_version": task.agent_version,
                "runtime_kind": task.runtime_kind, "instruction": task.instruction,
                "result_contract": ("REPAIR_INPUT", "REPAIR_DIFF", "REPLAY")[position] if repair
                    else ("CI_ARTIFACT" if position == 0 else "CI_STATUS") if ci else "TEXT",
                "requires_approval": task.requires_approval,
            } for position, task in enumerate(template.tasks)],
        }

    def _template(self, template_id, version):
        template = self.templates.get(template_id)
        if template is None or template.workflow_version != version:
            raise WorkspaceError("WORKSPACE_TEMPLATE_UNAVAILABLE", 422, "The template version is unavailable.")
        return template

    @staticmethod
    def _require_template_instance(template, instance_id):
        if template.result_binding is not None and instance_id != WorkspaceConfiguration._template_view(template)["fixed_runtime_instance_id"]:
            raise WorkspaceError("RUNTIME_INSTANCE_PURPOSE_DENIED", 422, "The CI template does not allow this instance.")

    @staticmethod
    def agent_view(row):
        return {
            "id": row.id, "space_id": row.space_id, "owner_user_id": row.owner_user_id,
            "revision": row.revision, "name": row.name, "description": row.description,
            "base_agent_id": row.base_agent_id, "base_agent_version": row.base_agent_version,
            "instructions": row.instructions,
        }

    @staticmethod
    async def agent_in_space(session, principal, space_id, agent_id):
        row = await session.get(WorkspaceAgentRow, agent_id)
        if row is None or row.space_id != space_id or row.tenant_id != principal.tenant_id:
            raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)
        return row

    async def agents(self, principal, space_id, agent_id=None):
        async with self.database.session() as session:
            await WorkspaceService.require_access_in_session(session, principal, space_id)
            if agent_id is not None:
                return self.agent_view(await self.agent_in_space(session, principal, space_id, agent_id))
            rows = await session.scalars(select(WorkspaceAgentRow).where(
                WorkspaceAgentRow.space_id == space_id, WorkspaceAgentRow.tenant_id == principal.tenant_id,
            ).order_by(WorkspaceAgentRow.name, WorkspaceAgentRow.id))
            return [self.agent_view(row) for row in rows]

    async def save_agent(self, principal, space_id, payload, agent_id=None):
        async with self.database.transaction() as session:
            await WorkspaceService.require_write_access_in_session(session, principal, space_id, manage=True)
            self.application.workspace_text_agent(payload.base_agent_id, payload.base_agent_version)
            values = payload.model_dump(exclude={"expected_revision"})
            if agent_id is None:
                row = WorkspaceAgentRow(
                    id=uuid4(), space_id=space_id, tenant_id=principal.tenant_id, owner_user_id=principal.user_id,
                    revision=1, **values,
                )
                session.add(row)
            else:
                row = await self.agent_in_space(session, principal, space_id, agent_id)
                _check_revision(row.revision, payload.expected_revision)
                for key, value in values.items():
                    setattr(row, key, value)
                row.revision += 1
            return self.agent_view(row)

    @staticmethod
    async def _workflow_in_space(session, principal, space_id, workflow_id):
        row = await session.get(WorkspaceWorkflowRow, workflow_id)
        if row is None or row.space_id != space_id or row.tenant_id != principal.tenant_id:
            raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)
        return row

    async def _workflow_view(self, session, row, manage):
        versions = (await session.scalars(select(WorkflowVersionRow).where(
            WorkflowVersionRow.workflow_id == row.id, WorkflowVersionRow.definition_snapshot.is_not(None),
        ))).all()
        releases = []
        for version in sorted(versions, key=lambda item: int(item.workflow_version), reverse=True):
            snapshot = version.definition_snapshot
            definition = snapshot["definition"]
            releases.append({
                "version": version.workflow_version, "name": snapshot["name"], "description": snapshot["description"],
                "definition_digest": version.definition_digest, "requires_ci_input": definition["result_binding"] is not None,
                "runtime_instance_id": next(task["runtime_instance_id"] for task in definition["tasks"] if task["runtime_kind"] == "OPENCODE"),
                "approval_phase": definition["approval_phase"],
                "steps": definition["tasks"],
            })
        return {
            "id": row.id, "space_id": row.space_id, "template_id": row.template_id, "template_version": row.template_version,
            "name": row.draft["name"] if manage else releases[0]["name"],
            "draft": row.draft if manage else None, "revision": row.revision if manage else None,
            "versions": releases,
        }

    async def workflows(self, principal, space_id, workflow_id=None):
        async with self.database.session() as session:
            access = await WorkspaceService.require_access_in_session(session, principal, space_id)
            manage = access.role in {"OWNER", "ADMIN"}
            if workflow_id is not None:
                row = await self._workflow_in_space(session, principal, space_id, workflow_id)
                if not manage and row.latest_version == 0:
                    raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)
                return await self._workflow_view(session, row, manage)
            query = select(WorkspaceWorkflowRow).where(
                WorkspaceWorkflowRow.space_id == space_id, WorkspaceWorkflowRow.tenant_id == principal.tenant_id,
            ).order_by(WorkspaceWorkflowRow.id)
            if not manage:
                query = query.where(WorkspaceWorkflowRow.latest_version > 0)
            return [await self._workflow_view(session, row, manage) for row in (await session.scalars(query)).all()]

    async def copy_workflow(self, principal, space_id, payload):
        async with self.database.transaction() as session:
            await WorkspaceService.require_write_access_in_session(session, principal, space_id, manage=True)
            template = self._template(payload.template_id, payload.template_version)
            external = next(task for task in template.tasks if task.runtime_kind == "OPENCODE")
            native = next(task for task in template.tasks if task.runtime_kind == "NATIVE")
            draft = {"name": payload.name, "description": "", "runtime_instance_id": None,
                     "external_instruction": external.instruction, "native_instruction": native.instruction}
            row = WorkspaceWorkflowRow(
                id=f"ws_{uuid4().hex}", space_id=space_id, tenant_id=principal.tenant_id, owner_user_id=principal.user_id,
                template_id=template.workflow_id, template_version=template.workflow_version, draft=draft,
                revision=1, latest_version=0,
            )
            session.add(row)
            return await self._workflow_view(session, row, True)

    async def save_workflow(self, principal, space_id, workflow_id, payload):
        async with self.database.transaction() as session:
            await WorkspaceService.require_write_access_in_session(session, principal, space_id, manage=True)
            row = await self._workflow_in_space(session, principal, space_id, workflow_id)
            _check_revision(row.revision, payload.expected_revision)
            template = self._template(row.template_id, row.template_version)
            purpose = self._template_view(template)["runtime_purpose"]
            if template.result_binding is not None and (
                payload.external_instruction is not None or payload.native_instruction is not None
            ):
                raise WorkspaceError("WORKSPACE_WORKFLOW_INSTRUCTIONS_FIXED", 422, "This template has fixed instructions.")
            if payload.runtime_instance_id is not None:
                self._require_template_instance(template, payload.runtime_instance_id)
                self.application.require_workspace_instance(payload.runtime_instance_id, principal, purpose)
            row.draft = {
                "name": payload.name, "description": payload.description, "runtime_instance_id": payload.runtime_instance_id,
                "external_instruction": payload.external_instruction or row.draft["external_instruction"],
                "native_instruction": payload.native_instruction or row.draft["native_instruction"],
            }
            row.revision += 1
            return await self._workflow_view(session, row, True)

    async def publish(self, principal, space_id, workflow_id, revision):
        async with self.database.transaction() as session:
            await WorkspaceService.require_write_access_in_session(session, principal, space_id, manage=True)
            row = await self._workflow_in_space(session, principal, space_id, workflow_id)
            _check_revision(row.revision, revision)
            if row.published_revision == revision:
                return await self._workflow_view(session, row, True)
            template = self._template(row.template_id, row.template_version)
            draft = row.draft
            if not draft["runtime_instance_id"]:
                raise WorkspaceError("WORKSPACE_WORKFLOW_RUNTIME_REQUIRED", 422, "Select an eligible OpenCode instance.")
            self._require_template_instance(template, draft["runtime_instance_id"])
            next_version = row.latest_version + 1
            tasks = template.tasks
            if template.result_binding != CI_REPAIR_BINDING:
                external, native = tasks
                tasks = (
                    replace(external, instruction=draft["external_instruction"], runtime_instance_id=draft["runtime_instance_id"]),
                    replace(native, instruction=draft["native_instruction"]),
                )
            definition = replace(template, workflow_id=workflow_id, workflow_version=str(next_version), tasks=tasks)
            digest, snapshot = self.workflow.publication(principal, definition)
            snapshot.update(name=draft["name"], description=draft["description"])
            session.add(WorkflowVersionRow(
                workflow_id=workflow_id, workflow_version=str(next_version), definition_digest=digest,
                definition_snapshot=snapshot,
            ))
            row.latest_version = next_version
            row.published_revision = revision
            await session.flush()
            return await self._workflow_view(session, row, True)
