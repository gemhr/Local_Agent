"""HUMAN 规划与验证接口；工具不暴露执行读取能力。"""
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Request, HTTPException
from pydantic import Field
from core.feature_impact.reports import Strict
from core.feature_impact.service import BusinessError, human

router = APIRouter(prefix="/api/feature-impact")


class Intent(Strict):
    request_id: UUID


class Submission(Intent):
    raw_output: str = Field(max_length=100000)


class Decision(Strict):
    finding_id: str
    decision: Literal["ACCEPT", "REJECT", "NEEDS_CLARIFICATION"]
    reason: str = Field(min_length=1, max_length=1000)


class Review(Intent):
    report_version: int
    report_digest: str
    decisions: list[Decision] = Field(min_length=1, max_length=30)


class Execution(Intent):
    plan_digest: str
    test_id: str


class QualityInput(Strict):
    task_id: str
    snapshot: dict
    candidate_kind: Literal["RULE", "HUMAN"]
    candidate_version: str
    report: dict | None = None


class QualityRequest(Strict):
    protocol: Literal["feature-impact-request.v1"]
    project_id: UUID
    run_id: UUID
    attempt_id: UUID
    request_id: UUID
    case_id: str
    frozen_input_digest: str
    input: QualityInput


@router.post("/quality/evaluate")
async def quality(body: QualityRequest, request: Request):
    from core.auth import require_scope, AuthError
    from core.feature_impact.quality import evaluate
    try:
        require_scope(request.state.principal, "localagent:feature-impact:evaluate")
        return evaluate(body.model_dump(mode="json"))
    except AuthError as exc:
        raise HTTPException(exc.status_code, exc.code) from None
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "FEATURE_QUALITY_INPUT_INVALID") from None


async def call(request, method, *args, **kwargs):
    p = getattr(request.state, "principal", None)
    if p is None:
        raise HTTPException(401, "AUTH_REQUIRED")
    try:
        human(p)
        return await getattr(request.app.state.feature_impact_service, method)(p, *args, **kwargs)
    except BusinessError as exc:
        raise HTTPException(exc.status, exc.code) from None
    except ValueError:
        raise HTTPException(422, "FEATURE_INPUT_INVALID") from None


@router.get("/tasks")
async def tasks(request: Request):
    return await call(request, "tasks")


@router.post("/tasks")
async def create(body: Intent, request: Request):
    return await call(request, "create", str(body.request_id))


@router.get("/tasks/{task_id}")
async def task(task_id: str, request: Request):
    return await call(request, "task", task_id)


@router.get("/tasks/{task_id}/evidence")
async def evidence(task_id: str, source_ref: str, request: Request):
    return await call(request, "task", task_id, source_ref=source_ref)


@router.get("/tasks/{task_id}/search")
async def search(task_id: str, request: Request, query: str = ""):
    return await call(request, "task", task_id, query=query)


@router.post("/tasks/{task_id}/reports")
async def submit(task_id: str, body: Submission, request: Request):
    return await call(request, "submit", task_id, str(body.request_id), body.raw_output)


@router.post("/tasks/{task_id}/rule-reports")
async def rule(task_id: str, body: Intent, request: Request):
    return await call(request, "submit", task_id, str(body.request_id), rule=True)


@router.post("/tasks/{task_id}/investigations")
async def investigate(task_id: str, body: Intent, request: Request):
    await call(request, "task", task_id)
    investigator = getattr(request.app.state, "feature_investigation_service", None)
    if investigator is not None:
        try:
            return await investigator.start(request.state.principal, task_id, str(body.request_id))
        except BusinessError as exc:
            raise HTTPException(exc.status, exc.code) from None
    raise HTTPException(409, "REAL_AGENT_EXECUTION_NOT_ENABLED")


@router.get("/reports/{report_id}")
async def report(report_id: str, request: Request):
    return await call(request, "report", report_id)


@router.post("/reports/{report_id}/reviews")
async def review(report_id: str, body: Review, request: Request):
    return await call(request, "review", report_id, body.model_dump(mode="json"))


@router.get("/coverage-plans/{plan_id}")
async def plan(plan_id: str, request: Request):
    return await call(request, "plan", plan_id)


@router.post("/coverage-plans/{plan_id}/executions")
async def execute(plan_id: str, body: Execution, request: Request):
    return await call(request, "execute", plan_id, body.model_dump(mode="json"))


@router.get("/executions/{execution_id}")
async def execution(execution_id: str, request: Request):
    return await call(request, "execution", execution_id)
