"""Stage8 窄 Repository；不创建 session、不拥有事务。"""

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.persistence.models import BusinessReviewRow, MissionRunReferenceRow, FeatureTestMissionRow, Stage8TestPlanRow, Stage8GeneratedCaseArtifactRow, Stage8ExternalExecutionJobRow, Stage8CIRunRow, Stage8CIAnalysisRow, Stage8TicketContinuationRow, DurableContinuationRow


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

async def add_test_plan(session: AsyncSession, values: dict) -> Stage8TestPlanRow:
    row = Stage8TestPlanRow(**values); session.add(row); await session.flush(); return row

async def get_generated_case_artifact(session: AsyncSession, *, mission_id: str, subject_id: str, version: int, digest: str, scenario_id: str):
    return await session.scalar(select(Stage8GeneratedCaseArtifactRow).where(
        Stage8GeneratedCaseArtifactRow.mission_id == mission_id,
        Stage8GeneratedCaseArtifactRow.test_plan_subject_id == subject_id,
        Stage8GeneratedCaseArtifactRow.test_plan_version == version,
        Stage8GeneratedCaseArtifactRow.test_plan_digest == digest,
        Stage8GeneratedCaseArtifactRow.scenario_id == scenario_id,
    ))

async def get_generated_case_artifact_by_id(session: AsyncSession, artifact_id: str):
    return await session.scalar(select(Stage8GeneratedCaseArtifactRow).where(
        Stage8GeneratedCaseArtifactRow.artifact_id == artifact_id
    ))

async def add_generated_case_artifact(session: AsyncSession, values: dict):
    row = Stage8GeneratedCaseArtifactRow(**values); session.add(row); await session.flush(); return row

async def list_generated_case_artifacts(session: AsyncSession, mission_id: str):
    return list((await session.scalars(select(Stage8GeneratedCaseArtifactRow).where(
        Stage8GeneratedCaseArtifactRow.mission_id == mission_id
    ).order_by(Stage8GeneratedCaseArtifactRow.created_at, Stage8GeneratedCaseArtifactRow.artifact_id))).all())

async def decide_review(session: AsyncSession, review_id: str, expected_status: str, status: str, *, decided_by: str | None, comment: str | None):
    return await session.scalar(update(BusinessReviewRow).where(
        BusinessReviewRow.review_id == review_id, BusinessReviewRow.status == expected_status,
    ).values(status=status, decided_at=func.now(), decided_by=decided_by, decision_comment=comment).returning(BusinessReviewRow))


async def get_current_test_plan(session: AsyncSession, mission_id: str):
    """返回 mission 当前版本的 TestPlan subject。

    当前 subject 由稳定 version、创建时间和 subject_id 顺序确定。
    """
    return await session.scalar(select(Stage8TestPlanRow).where(
        Stage8TestPlanRow.mission_id == mission_id,
    ).order_by(
        Stage8TestPlanRow.version.desc(), Stage8TestPlanRow.created_at.desc(),
        Stage8TestPlanRow.subject_id.desc(),
    ).limit(1))


async def get_approved_test_plan_review(session: AsyncSession, mission_id: str):
    """只返回批准当前 TestPlan subject 的 BusinessReview。

    不能把同一 mission 下旧版本（或缺少 TestPlan subject）的 APPROVED
    Review 当作当前执行门禁。
    """
    plan = await get_current_test_plan(session, mission_id)
    if plan is None:
        return None
    return await session.scalar(select(BusinessReviewRow).where(
        BusinessReviewRow.mission_id == mission_id,
        BusinessReviewRow.review_type == "TEST_PLAN",
        BusinessReviewRow.status == "APPROVED",
        BusinessReviewRow.subject_id == plan.subject_id,
        BusinessReviewRow.subject_version == plan.version,
        BusinessReviewRow.subject_digest == plan.subject_digest,
    ).order_by(BusinessReviewRow.decided_at.desc()))


async def add_execution_job(session, values: dict):
    row = Stage8ExternalExecutionJobRow(**values); session.add(row); await session.flush(); return row


async def get_execution_job(session, execution_id: str, *, for_update=False):
    query = select(Stage8ExternalExecutionJobRow).where(Stage8ExternalExecutionJobRow.execution_id == execution_id)
    if for_update: query = query.with_for_update()
    return await session.scalar(query)


async def get_execution_job_by_id(session, job_id: str, *, for_update=False):
    query = select(Stage8ExternalExecutionJobRow).where(
        Stage8ExternalExecutionJobRow.job_id == job_id
    )
    if for_update: query = query.with_for_update()
    return await session.scalar(query)

async def list_execution_jobs(session, mission_id: str):
    return list((await session.scalars(select(Stage8ExternalExecutionJobRow).where(
        Stage8ExternalExecutionJobRow.mission_id == mission_id
    ).order_by(
        Stage8ExternalExecutionJobRow.created_at.desc(),
        Stage8ExternalExecutionJobRow.job_id.desc(),
    ))).all())


async def update_execution_job(session, execution_id: str, values: dict):
    return await session.scalar(update(Stage8ExternalExecutionJobRow).where(
        Stage8ExternalExecutionJobRow.execution_id == execution_id,
    ).values(**values, version=Stage8ExternalExecutionJobRow.version + 1, updated_at=func.now()).returning(Stage8ExternalExecutionJobRow))


async def update_execution_job_by_id(session, job_id: str, values: dict):
    return await session.scalar(update(Stage8ExternalExecutionJobRow).where(
        Stage8ExternalExecutionJobRow.job_id == job_id,
    ).values(**values, version=Stage8ExternalExecutionJobRow.version + 1, updated_at=func.now()).returning(Stage8ExternalExecutionJobRow))


async def claim_execution_triage(session, execution_id: str):
    return await session.scalar(update(Stage8ExternalExecutionJobRow).where(
        Stage8ExternalExecutionJobRow.execution_id == execution_id,
        Stage8ExternalExecutionJobRow.status == "FAILED",
        Stage8ExternalExecutionJobRow.triage_payload.is_(None),
    ).values(
        triage_payload={"state": "RUNNING"},
        version=Stage8ExternalExecutionJobRow.version + 1,
        updated_at=func.now(),
    ).returning(Stage8ExternalExecutionJobRow))


async def add_ticket_continuation(session: AsyncSession, values: dict):
    row = Stage8TicketContinuationRow(**values)
    session.add(row)
    await session.flush()
    return row


async def get_ticket_continuation(session: AsyncSession, continuation_id: str, *, for_update: bool = False):
    query = select(Stage8TicketContinuationRow).where(
        Stage8TicketContinuationRow.continuation_id == continuation_id
    )
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


async def get_ticket_continuation_by_approval(session: AsyncSession, approval_id: str, *, for_update: bool = False):
    query = select(Stage8TicketContinuationRow).where(
        Stage8TicketContinuationRow.approval_id == approval_id
    )
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


async def add_generic_continuation(session: AsyncSession, values: dict):
    row = DurableContinuationRow(**values); session.add(row); await session.flush(); return row

async def get_generic_continuation(session: AsyncSession, continuation_id: str, *, for_update=False):
    query = select(DurableContinuationRow).where(DurableContinuationRow.continuation_id == continuation_id)
    if for_update: query = query.with_for_update()
    return await session.scalar(query)


async def list_ready_ticket_continuations(session: AsyncSession):
    return list((await session.scalars(select(Stage8TicketContinuationRow).where(
        Stage8TicketContinuationRow.state.in_(("PENDING_APPROVAL", "READY"))
    ).order_by(Stage8TicketContinuationRow.created_at, Stage8TicketContinuationRow.continuation_id))).all())

async def get_ci_run(session, ci_run_id: str, *, for_update: bool = False):
    query = select(Stage8CIRunRow).where(Stage8CIRunRow.ci_run_id == ci_run_id)
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)

async def list_ci_runs(session, suite_id: str | None = None):
    query = select(Stage8CIRunRow).order_by(Stage8CIRunRow.completed_at)
    if suite_id is not None:
        query = query.where(Stage8CIRunRow.suite_id == suite_id)
    return list((await session.scalars(query)).all())

async def add_ci_run(session, values: dict):
    row = Stage8CIRunRow(**values); session.add(row); await session.flush(); return row

async def get_ci_analysis(session, ci_run_id: str):
    return await session.scalar(select(Stage8CIAnalysisRow).where(Stage8CIAnalysisRow.ci_run_id == ci_run_id))

async def add_ci_analysis(session, values: dict):
    row = Stage8CIAnalysisRow(**values); session.add(row); await session.flush(); return row
