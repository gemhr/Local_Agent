"""Stage8 窄 Repository；不创建 session、不拥有事务。"""

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.persistence.models import BusinessReviewRow, MissionRunReferenceRow, FeatureTestMissionRow


async def add_mission(session: AsyncSession, values: dict) -> FeatureTestMissionRow:
    row = FeatureTestMissionRow(**values); session.add(row); await session.flush(); return row

async def get_mission(session: AsyncSession, mission_id: str, *, for_update: bool = False):
    query = select(FeatureTestMissionRow).where(FeatureTestMissionRow.mission_id == mission_id)
    if for_update: query = query.with_for_update()
    return await session.scalar(query)

async def update_mission_state(session: AsyncSession, mission_id: str, expected_version: int, status: str):
    return await session.scalar(update(FeatureTestMissionRow).where(
        FeatureTestMissionRow.mission_id == mission_id,
        FeatureTestMissionRow.version == expected_version,
    ).values(status=status, version=FeatureTestMissionRow.version + 1, updated_at=func.now()).returning(FeatureTestMissionRow))

async def add_run_reference(session: AsyncSession, values: dict) -> MissionRunReferenceRow:
    row = MissionRunReferenceRow(**values); session.add(row); await session.flush(); return row

async def list_run_references(session: AsyncSession, mission_id: str):
    return list((await session.scalars(select(MissionRunReferenceRow).where(
        MissionRunReferenceRow.mission_id == mission_id).order_by(MissionRunReferenceRow.created_at))).all())

async def add_review(session: AsyncSession, values: dict) -> BusinessReviewRow:
    row = BusinessReviewRow(**values); session.add(row); await session.flush(); return row

async def get_review(session: AsyncSession, review_id: str, *, for_update: bool = False):
    query = select(BusinessReviewRow).where(BusinessReviewRow.review_id == review_id)
    if for_update: query = query.with_for_update()
    return await session.scalar(query)

async def decide_review(session: AsyncSession, review_id: str, expected_status: str, status: str, *, decided_by: str | None, comment: str | None):
    return await session.scalar(update(BusinessReviewRow).where(
        BusinessReviewRow.review_id == review_id, BusinessReviewRow.status == expected_status,
    ).values(status=status, decided_at=func.now(), decided_by=decided_by, decision_comment=comment).returning(BusinessReviewRow))
