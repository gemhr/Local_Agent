"""任务、审批与 Run 观察接口。任务操作沿用原路由。"""

from dataclasses import asdict
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from core.workspace.http import Actor
from core.workspace.observations import approval_page, run_result, task_detail, task_page


router = APIRouter(tags=["workspace observation"])
Kind = Literal["AGENT", "WORKFLOW"]
TaskStatus = Literal["PENDING", "RUNNING", "WAITING_APPROVAL", "AWAITING_OWNER", "PAUSED", "UNKNOWN",
                     "SUCCEEDED", "FAILED", "REJECTED", "CANCELLING", "CANCELLED"]


@router.get("/workspace/tasks")
async def tasks(request: Request, principal: Actor, space_id: UUID | None = None, kind: Kind | None = None,
                status: TaskStatus | None = None, mine: bool = False,
                offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100)):
    return await task_page(request.app.state.workspace_service.database, principal, space_id=space_id,
                           kind=kind, status=status, mine=mine, offset=offset, limit=limit)


@router.get("/workspace/tasks/{kind}/{task_id}")
async def detail(request: Request, principal: Actor, kind: Kind, task_id: UUID):
    state = request.app.state
    return await task_detail(state.workspace_service.database, state.agent_application_service, state.workflow_service,
                             state.authorization_service, principal, kind, str(task_id))


@router.get("/workspace/approvals")
async def approvals(request: Request, principal: Actor, space_id: UUID | None = None,
                    status: Literal["PENDING", "APPROVED", "REJECTED", "INVALIDATED", "AWAITING_OWNER"] | None = None,
                    offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100)):
    return await approval_page(request.app.state.workspace_service.database, principal, space_id=space_id,
                               status=status, offset=offset, limit=limit)


@router.get("/runs/{run_id}/events/history")
async def history(request: Request, principal: Actor, run_id: str, after_cursor: int = Query(0, ge=0, le=2**53 - 1),
                  limit: int = Query(50, ge=1, le=100)):
    state = request.app.state
    await state.authorization_service.authorize(principal, "RUN", run_id, "SUBSCRIBE", share_workspace_read=True)
    if state.client_event_feed is None:
        raise HTTPException(503, "CLIENT_EVENT_FEED_UNAVAILABLE")
    events = await state.client_event_feed.read_after(run_id, after_cursor, limit + 1)
    items = events[:limit]
    return {"run_id": run_id, "events": [asdict(event) for event in items],
            "next_cursor": items[-1].cursor if items else after_cursor, "has_more": len(events) > limit}


@router.get("/runs/{run_id}/confirmed-result")
async def confirmed_result(request: Request, principal: Actor, run_id: str):
    state = request.app.state
    await state.authorization_service.authorize(principal, "RUN", run_id, "READ", share_workspace_read=True)
    return await run_result(state.workspace_service.database, state.agent_application_service, run_id)
