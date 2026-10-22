"""夜间、候选关联及本人通知的薄 HTTP 接线。"""
from fastapi import APIRouter, Depends, Request, Response, Query
from core.incidents.http import human, call
from core.incidents.contracts import CreateNightly, NightlyClock, AdvanceNightly, GroupDecision
from core.incidents import nightly, grouping, notifications

router = APIRouter(prefix="/api/ci", dependencies=[Depends(human)])


@router.get("/nightly/options")
async def options(request: Request):
    return await call(nightly.options(request.app.state.incident_service, human(request)))


@router.post("/nightly/batches")
async def create(body: CreateNightly, request: Request, response: Response):
    result = await call(nightly.create(request.app.state.incident_service, human(request), body))
    response.status_code = 201 if result["created"] else 200
    return result


@router.get("/nightly/batches")
async def batches(request: Request):
    return await call(nightly.list_batches(request.app.state.incident_service, human(request)))


@router.get("/nightly/batches/{batch_id}")
async def detail(batch_id: str, request: Request):
    return await call(nightly.detail(request.app.state.incident_service, human(request), batch_id))


@router.post("/nightly/batches/{batch_id}/clock")
async def clock(batch_id: str, body: NightlyClock, request: Request):
    return await call(nightly.clock(request.app.state.incident_service, human(request), batch_id, body))


@router.post("/nightly/batches/{batch_id}/advance")
async def advance(batch_id: str, body: AdvanceNightly, request: Request):
    return await call(nightly.advance(request.app.state.incident_service, human(request), batch_id, body))


@router.get("/nightly/batches/{batch_id}/failures")
async def failures(batch_id: str, request: Request):
    return await call(nightly.failures(request.app.state.incident_service, human(request), batch_id))


async def group_request(request, batch_id, group_id=None, body=None):
    service, principal = request.app.state.incident_service, human(request)
    async with service.database.transaction() as session:
        batch, manager = await nightly.scope(session, principal, batch_id, lock=body is not None)
        if body is not None:
            return await grouping.decide(session, principal, batch, group_id, manager, body)
        if group_id is None:
            return await grouping.list_groups(session, principal, batch, manager)
        row = await grouping.get_group(session, principal, batch, group_id, manager)
        return await grouping.payload(session, principal, row, manager)


@router.get("/nightly/batches/{batch_id}/groups")
async def groups(batch_id: str, request: Request):
    return await call(group_request(request, batch_id))


@router.get("/nightly/batches/{batch_id}/groups/{group_id}")
async def group(batch_id: str, group_id: str, request: Request):
    return await call(group_request(request, batch_id, group_id))


@router.post("/nightly/batches/{batch_id}/groups/{group_id}/decisions")
async def decision(batch_id: str, group_id: str, body: GroupDecision, request: Request):
    return await call(group_request(request, batch_id, group_id, body))


@router.get("/notifications")
async def inbox(request: Request, unread_only: bool = False, limit: int = Query(50, ge=1, le=50)):
    return await call(notifications.inbox(request.app.state.incident_service, human(request), unread_only, limit))


@router.post("/notifications/{notification_id}/read")
async def read(notification_id: str, request: Request):
    return await call(notifications.mark_read(request.app.state.incident_service, human(request), notification_id))
