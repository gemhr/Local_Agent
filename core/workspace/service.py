"""Workspace 服务与 Space 授权，沿用既有 Principal 和当前 HUMAN 校验。"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import (
    AUTHORIZATION_FORBIDDEN, AUTHORIZATION_OBJECT_NOT_OWNED,
    AuthError, ObjectAuthorizationService, Principal,
)
from core.persistence.database import Database
from core.persistence.models import SpaceMemberRow, SpaceRow, UserRow
from core.persistence.repositories.spaces import SpaceRepository

MemberRole = Literal["ADMIN", "MEMBER"]
SpaceRole = Literal["OWNER", "ADMIN", "MEMBER"]


class WorkspaceError(Exception):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        self.code = code
        self.status_code = status_code
        self.message = message
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SpaceAccess:
    space: SpaceRow
    role: SpaceRole

    @property
    def can_manage_members(self) -> bool:
        return self.space.kind == "TEAM" and self.role in {"OWNER", "ADMIN"}


class WorkspaceService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    async def require_access_in_session(
        session: AsyncSession, principal: Principal, space_id: UUID, *,
        resource_tenant_id: str | None = None,
    ) -> SpaceAccess:
        """供业务事务复用。已绑定资源应传入其从持久化记录解析的 tenant。"""
        await ObjectAuthorizationService.require_current_human_in_session(session, principal)
        if resource_tenant_id is not None and resource_tenant_id != principal.tenant_id:
            raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)
        result = await session.execute(
            SpaceRepository.visible_to(principal.tenant_id, principal.user_id).where(SpaceRow.id == space_id)
        )
        row = result.one_or_none()
        if row is None:
            raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)
        return SpaceAccess(*row)

    @staticmethod
    async def require_write_access_in_session(
        session: AsyncSession, principal: Principal, space_id: UUID, *, manage: bool = False,
    ) -> SpaceAccess:
        """与成员写入使用同一把事务锁，锁后读取角色。"""
        if not await SpaceRepository.lock_space(session, space_id, principal.tenant_id):
            raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)
        access = await WorkspaceService.require_access_in_session(session, principal, space_id)
        if manage and access.role not in {"OWNER", "ADMIN"}:
            raise AuthError(AUTHORIZATION_FORBIDDEN, 403)
        return access

    async def list_spaces(self, principal: Principal) -> list[SpaceAccess]:
        async with self.database.transaction() as session:
            await ObjectAuthorizationService.require_current_human_in_session(session, principal)
            await SpaceRepository.ensure_personal(session, principal.tenant_id, principal.user_id)
            result = await session.execute(
                SpaceRepository.visible_to(principal.tenant_id, principal.user_id)
                .order_by(SpaceRow.kind, SpaceRow.created_at, SpaceRow.id)
            )
            return [SpaceAccess(*row) for row in result]

    async def create_team(self, principal: Principal, name: str) -> SpaceAccess:
        async with self.database.transaction() as session:
            await ObjectAuthorizationService.require_current_human_in_session(session, principal)
            space = SpaceRow(
                id=uuid4(), tenant_id=principal.tenant_id, kind="TEAM",
                name=name, owner_user_id=principal.user_id,
            )
            session.add(space)
            await session.flush()
            return SpaceAccess(space, "OWNER")

    async def get_space(self, principal: Principal, space_id: UUID) -> SpaceAccess:
        async with self.database.session() as session:
            return await self.require_access_in_session(session, principal, space_id)

    async def list_members(self, principal: Principal, space_id: UUID):
        async with self.database.session() as session:
            access = await self.require_access_in_session(session, principal, space_id)
            self._require_team(access)
            return await SpaceRepository.members(session, access.space)

    @staticmethod
    def _require_team(access: SpaceAccess) -> None:
        if access.space.kind != "TEAM":
            raise WorkspaceError("SPACE_MEMBERS_UNAVAILABLE", 404, "This space has no team roster.")

    async def _require_member_write(
        self, session: AsyncSession, principal: Principal, space_id: UUID, user_id: UUID,
    ) -> None:
        # 同一团队的成员写入串行化。等待锁结束后再读当前角色，确保撤权生效。
        if not await SpaceRepository.lock_space(session, space_id, principal.tenant_id):
            raise AuthError(AUTHORIZATION_OBJECT_NOT_OWNED, 404)
        access = await self.require_access_in_session(session, principal, space_id)
        self._require_team(access)
        if not access.can_manage_members:
            raise AuthError(AUTHORIZATION_FORBIDDEN, 403)
        if user_id == access.space.owner_user_id:
            raise WorkspaceError("SPACE_OWNER_IMMUTABLE", 409, "The team owner cannot be changed.")

    async def put_member(self, principal: Principal, space_id: UUID, user_id: UUID, role: MemberRole):
        async with self.database.transaction() as session:
            await self._require_member_write(session, principal, space_id, user_id)
            user = await session.scalar(select(UserRow).where(
                UserRow.id == user_id, UserRow.tenant_id == principal.tenant_id,
                UserRow.principal_kind == "HUMAN", UserRow.disabled_at.is_(None),
            ).with_for_update(read=True))
            if user is None:
                raise WorkspaceError("SPACE_MEMBER_USER_NOT_FOUND", 404, "Eligible user not found.")
            member = await session.get(SpaceMemberRow, (space_id, user_id))
            if member is None:
                member = SpaceMemberRow(
                    space_id=space_id, user_id=user_id, tenant_id=principal.tenant_id, role=role,
                )
                session.add(member)
            else:
                member.role = role
            await session.flush()
            return user, member

    async def remove_member(self, principal: Principal, space_id: UUID, user_id: UUID) -> None:
        async with self.database.transaction() as session:
            await self._require_member_write(session, principal, space_id, user_id)
            member = await session.get(SpaceMemberRow, (space_id, user_id))
            if member is None:
                raise WorkspaceError("SPACE_MEMBER_NOT_FOUND", 404, "Team member not found.")
            await session.delete(member)
