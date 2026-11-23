"""Workspace v1 HTTP 合同。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, StringConstraints

from core.auth import Principal, get_principal
from core.persistence.models import SpaceMemberRow, UserRow
from core.workspace.service import MemberRole, SpaceAccess, SpaceRole, WorkspaceService

router = APIRouter(prefix="/spaces", tags=["workspace"])


class TeamCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120, pattern=r"^[^\x00]+$")]


class MemberPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: MemberRole


class SpaceSummary(BaseModel):
    id: UUID
    tenant_id: str
    kind: Literal["PERSONAL", "TEAM"]
    name: str
    owner_user_id: UUID
    created_at: datetime
    updated_at: datetime
    role: SpaceRole
    can_manage_members: bool


class MemberSummary(BaseModel):
    user_id: UUID
    display_name: str | None
    role: SpaceRole
    active: bool


def _space(access: SpaceAccess) -> SpaceSummary:
    row = access.space
    return SpaceSummary(
        id=row.id, tenant_id=row.tenant_id, kind=row.kind, name=row.name, owner_user_id=row.owner_user_id,
        created_at=row.created_at, updated_at=row.updated_at, role=access.role,
        can_manage_members=access.can_manage_members,
    )


def _member(user: UserRow, member: SpaceMemberRow | None) -> MemberSummary:
    return MemberSummary(
        user_id=user.id, display_name=user.display_name, role="OWNER" if member is None else member.role,
        active=user.disabled_at is None and user.principal_kind == "HUMAN",
    )


def _service(request: Request) -> WorkspaceService:
    return request.app.state.workspace_service


Actor = Annotated[Principal, Depends(get_principal)]
Workspace = Annotated[WorkspaceService, Depends(_service)]


@router.get("")
async def list_spaces(principal: Actor, workspace: Workspace) -> list[SpaceSummary]:
    return [_space(access) for access in await workspace.list_spaces(principal)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_team(payload: TeamCreate, principal: Actor, workspace: Workspace) -> SpaceSummary:
    return _space(await workspace.create_team(principal, payload.name))


@router.get("/{space_id}")
async def get_space(space_id: UUID, principal: Actor, workspace: Workspace) -> SpaceSummary:
    return _space(await workspace.get_space(principal, space_id))


@router.get("/{space_id}/members")
async def list_members(space_id: UUID, principal: Actor, workspace: Workspace) -> list[MemberSummary]:
    return [_member(user, member) for user, member in await workspace.list_members(principal, space_id)]


@router.put("/{space_id}/members/{user_id}")
async def put_member(
    space_id: UUID, user_id: UUID, payload: MemberPut, principal: Actor, workspace: Workspace,
) -> MemberSummary:
    user, member = await workspace.put_member(principal, space_id, user_id, payload.role)
    return _member(user, member)


@router.delete("/{space_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(space_id: UUID, user_id: UUID, principal: Actor, workspace: Workspace) -> Response:
    await workspace.remove_member(principal, space_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
