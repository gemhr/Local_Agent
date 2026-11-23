"""Workspace 空间归属、成员和个人空间首建的 PostgreSQL 查询。"""

from uuid import UUID, uuid4

from sqlalchemy import and_, case, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.persistence.models import SpaceMemberRow, SpaceRow, UserRow


class SpaceRepository:
    @staticmethod
    def visible_to(tenant_id: str, user_id: UUID):
        owner = SpaceRow.owner_user_id == user_id
        return (
            select(SpaceRow, case((owner, "OWNER"), else_=SpaceMemberRow.role).label("space_role"))
            .outerjoin(SpaceMemberRow, and_(
                SpaceMemberRow.space_id == SpaceRow.id,
                SpaceMemberRow.tenant_id == SpaceRow.tenant_id,
                SpaceMemberRow.user_id == user_id,
            ))
            .where(SpaceRow.tenant_id == tenant_id, or_(
                owner, and_(SpaceRow.kind == "TEAM", SpaceMemberRow.role.is_not(None)),
            ))
        )

    @staticmethod
    async def ensure_personal(session: AsyncSession, tenant_id: str, user_id: UUID) -> None:
        existing = await session.scalar(select(SpaceRow.id).where(
            SpaceRow.tenant_id == tenant_id, SpaceRow.owner_user_id == user_id, SpaceRow.kind == "PERSONAL",
        ))
        if existing is None:
            await session.execute(insert(SpaceRow).values(
                id=uuid4(), tenant_id=tenant_id, owner_user_id=user_id, kind="PERSONAL", name="个人空间",
            ).on_conflict_do_nothing(
                index_elements=[SpaceRow.tenant_id, SpaceRow.owner_user_id],
                index_where=text("kind = 'PERSONAL'"),
            ))

    @staticmethod
    async def lock_space(session: AsyncSession, space_id: UUID, tenant_id: str) -> bool:
        return await session.scalar(select(SpaceRow.id).where(
            SpaceRow.id == space_id, SpaceRow.tenant_id == tenant_id,
        ).with_for_update()) is not None

    @staticmethod
    async def members(session: AsyncSession, space: SpaceRow):
        return (await session.execute(
            select(UserRow, SpaceMemberRow)
            .outerjoin(SpaceMemberRow, and_(
                SpaceMemberRow.user_id == UserRow.id,
                SpaceMemberRow.tenant_id == UserRow.tenant_id,
                SpaceMemberRow.space_id == space.id,
            ))
            .where(UserRow.tenant_id == space.tenant_id, or_(
                UserRow.id == space.owner_user_id, SpaceMemberRow.user_id.is_not(None),
            ))
            .order_by((UserRow.id == space.owner_user_id).desc(), UserRow.display_name, UserRow.id)
        )).all()
