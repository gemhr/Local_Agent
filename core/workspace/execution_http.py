"""Workspace 配置、版本发布和任务提交的 v1 HTTP 路由。"""

from uuid import UUID

from fastapi import APIRouter, Request

from core.workspace.contracts import AgentConfig, AgentTask, AgentUpdate, TaskLaunch, WorkflowCopy, WorkflowEdit, WorkflowPublish
from core.workspace.http import Actor

router = APIRouter(prefix="/spaces/{space_id}", tags=["workspace execution"])


@router.get("/execution-catalog")
async def execution_catalog(space_id: UUID, principal: Actor, request: Request):
    return await request.app.state.workspace_configuration.get_catalog(principal, space_id)


@router.get("/runtime-instances")
async def runtime_instances(space_id: UUID, principal: Actor, request: Request):
    return await request.app.state.workspace_configuration.get_runtimes(principal, space_id)


@router.get("/agents")
async def list_agents(space_id: UUID, principal: Actor, request: Request):
    return await request.app.state.workspace_configuration.agents(principal, space_id)


@router.get("/agents/{agent_id}")
async def read_agent(space_id: UUID, agent_id: UUID, principal: Actor, request: Request):
    return await request.app.state.workspace_configuration.agents(principal, space_id, agent_id)


@router.post("/agents", status_code=201)
async def create_agent(space_id: UUID, payload: AgentConfig, principal: Actor, request: Request):
    return await request.app.state.workspace_configuration.save_agent(principal, space_id, payload)


@router.put("/agents/{agent_id}")
async def update_agent(space_id: UUID, agent_id: UUID, payload: AgentUpdate, principal: Actor, request: Request):
    return await request.app.state.workspace_configuration.save_agent(principal, space_id, payload, agent_id)


@router.get("/workflows")
async def list_workflows(space_id: UUID, principal: Actor, request: Request):
    return await request.app.state.workspace_configuration.workflows(principal, space_id)


@router.get("/workflows/{workflow_id}")
async def read_workflow(space_id: UUID, workflow_id: str, principal: Actor, request: Request):
    return await request.app.state.workspace_configuration.workflows(principal, space_id, workflow_id)


@router.post("/workflows", status_code=201)
async def copy_workflow(space_id: UUID, payload: WorkflowCopy, principal: Actor, request: Request):
    return await request.app.state.workspace_configuration.copy_workflow(principal, space_id, payload)


@router.put("/workflows/{workflow_id}")
async def update_workflow(space_id: UUID, workflow_id: str, payload: WorkflowEdit, principal: Actor, request: Request):
    return await request.app.state.workspace_configuration.save_workflow(principal, space_id, workflow_id, payload)


@router.post("/workflows/{workflow_id}/publish")
async def publish_workflow(space_id: UUID, workflow_id: str, payload: WorkflowPublish, principal: Actor, request: Request):
    return await request.app.state.workspace_configuration.publish(principal, space_id, workflow_id, payload.expected_revision)


@router.post("/tasks", status_code=202)
async def create_task(space_id: UUID, payload: TaskLaunch, principal: Actor, request: Request):
    if isinstance(payload, AgentTask):
        return await request.app.state.workspace_tasks.launch_agent(principal, space_id, payload)
    return await request.app.state.workspace_tasks.launch_workflow(principal, space_id, payload)
