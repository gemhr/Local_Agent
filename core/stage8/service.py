"""Stage8 Application Service：业务状态与事务的唯一 Owner。"""

import uuid
from dataclasses import asdict
from sqlalchemy.exc import IntegrityError

from core.stage8 import repositories as repo
from core.stage8.domain import *


class Stage8Error(RuntimeError): pass
class Stage8NotFoundError(Stage8Error): pass
class Stage8ConflictError(Stage8Error): pass
class Stage8ValidationError(Stage8Error): pass


def _mission(row):
    return FeatureTestMission(row.mission_id, row.feature_id, MissionStatus(row.status), row.version, row.created_at, row.updated_at, row.title, row.summary)
def _ref(row): return MissionRunReference(row.mission_id, row.run_id, row.run_purpose, row.created_at)
def _review(row): return BusinessReview(row.review_id, row.mission_id, ReviewType(row.review_type), row.subject_id, row.subject_version, row.subject_digest, ReviewStatus(row.status), row.created_at, row.decided_at, row.decided_by, row.decision_comment)


class MissionService:
    def __init__(self, database): self.database = database

    async def create_mission(self, feature_id: str, *, title=None, summary=None, mission_id=None):
        if not feature_id.strip(): raise Stage8ValidationError("feature_id is required")
        async with self.database.transaction() as session:
            row = await repo.add_mission(session, dict(mission_id=mission_id or uuid.uuid4().hex, feature_id=feature_id, status=MissionStatus.CREATED.value, version=1, title=title, summary=summary))
            return _mission(row)
    async def get_mission(self, mission_id):
        async with self.database.session() as session: row = await repo.get_mission(session, mission_id)
        if row is None: raise Stage8NotFoundError("mission not found")
        return _mission(row)
    async def transition_mission(self, mission_id, to_status: MissionStatus | str, expected_version: int, *, session=None):
        try: target = MissionStatus(to_status)
        except ValueError as exc: raise Stage8ValidationError("invalid mission status") from exc
        if session is None:
            async with self.database.transaction() as owned_session:
                return await self.transition_mission(
                    mission_id, target, expected_version, session=owned_session
                )
        current = await repo.get_mission(session, mission_id, for_update=True)
        if current is None: raise Stage8NotFoundError("mission not found")
        if current.version != expected_version: raise Stage8ConflictError("stale mission version")
        if target not in ALLOWED_TRANSITIONS[MissionStatus(current.status)]: raise Stage8ValidationError("invalid mission transition")
        row = await repo.update_mission_state(session, mission_id, expected_version, target.value)
        if row is None: raise Stage8ConflictError("stale mission version")
        return _mission(row)
    async def attach_run_reference(self, mission_id, run_id, run_purpose):
        if not run_id.strip() or not run_purpose.strip(): raise Stage8ValidationError("run_id and run_purpose are required")
        async with self.database.transaction() as session:
            if await repo.get_mission(session, mission_id) is None: raise Stage8NotFoundError("mission not found")
            try: row = await repo.add_run_reference(session, dict(mission_id=mission_id, run_id=run_id, run_purpose=run_purpose, reference_id=uuid.uuid4().hex))
            except IntegrityError as exc: raise Stage8ConflictError("run reference already exists") from exc
            return _ref(row)
    async def list_run_references(self, mission_id):
        async with self.database.session() as session:
            if await repo.get_mission(session, mission_id) is None: raise Stage8NotFoundError("mission not found")
            rows = await repo.list_run_references(session, mission_id)
        return [_ref(r) for r in rows]


class BusinessReviewService:
    def __init__(self, database): self.database = database
    async def create_review(self, mission_id, review_type, *, subject_id=None, subject_version=None, subject_digest=None, review_id=None):
        try: kind = ReviewType(review_type)
        except ValueError as exc: raise Stage8ValidationError("invalid review_type") from exc
        async with self.database.transaction() as session:
            if await repo.get_mission(session, mission_id) is None: raise Stage8NotFoundError("mission not found")
            row = await repo.add_review(session, dict(review_id=review_id or uuid.uuid4().hex, mission_id=mission_id, review_type=kind.value, subject_id=subject_id, subject_version=subject_version, subject_digest=subject_digest, status=ReviewStatus.PENDING.value))
            return _review(row)
    async def get_review(self, review_id):
        async with self.database.session() as session: row = await repo.get_review(session, review_id)
        if row is None: raise Stage8NotFoundError("review not found")
        return _review(row)
    async def _decide(self, review_id, mission_id, target, *, decided_by=None, comment=None, subject_id=None, subject_version=None, subject_digest=None):
        async with self.database.transaction() as session:
            row = await repo.get_review(session, review_id, for_update=True)
            if row is None: raise Stage8NotFoundError("review not found")
            if row.mission_id != mission_id: raise Stage8ValidationError("review mission binding mismatch")
            if row.status != ReviewStatus.PENDING.value:
                if row.status == target: return _review(row)
                raise Stage8ConflictError("review already decided")
            if (row.subject_id is not None or row.subject_version is not None or row.subject_digest is not None) and (
                row.subject_id,
                row.subject_version,
                row.subject_digest,
            ) != (subject_id, subject_version, subject_digest):
                raise Stage8ConflictError("review subject binding mismatch")
            decided = await repo.decide_review(session, review_id, ReviewStatus.PENDING.value, target, decided_by=decided_by, comment=comment)
            return _review(decided)
    async def approve_review(self, review_id, mission_id, **kwargs): return await self._decide(review_id, mission_id, ReviewStatus.APPROVED.value, **kwargs)
    async def reject_review(self, review_id, mission_id, **kwargs): return await self._decide(review_id, mission_id, ReviewStatus.REJECTED.value, **kwargs)


class TestPlanRepository:
    """TestPlan subject 的最小 PostgreSQL 持久化入口。"""
    def __init__(self, database): self.database = database

    async def save(self, mission_id, subject_id, version, digest, payload):
        try:
            async with self.database.transaction() as session:
                return await repo.add_test_plan(session, dict(
                    subject_id=subject_id, mission_id=mission_id, version=version,
                    subject_digest=digest, payload=payload,
                ))
        except IntegrityError as exc:
            raise Stage8ConflictError("test plan subject already exists or mission binding is invalid") from exc
