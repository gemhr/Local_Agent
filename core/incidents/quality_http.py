"""分离 HUMAN 导出与精确 SERVICE 业务检查权限。"""
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, HTTPException
from core.auth import require_scope, AuthError
from core.incidents.http import human, call
from core.incidents.contracts import QualityExport, QualityEvaluate, CIError
from core.incidents import quality, repair_quality

router = APIRouter(prefix="/api/ci/quality")
SCOPE = "localagent:ci-business:evaluate"


@router.get("/repair-evidence/{workflow_run_id}/{evidence_digest}")
async def repair_evidence(workflow_run_id: UUID, evidence_digest: str, request: Request,
                          project_id: UUID = Query()):
    state = request.app.state
    if getattr(state, "workflow_service", None) is None:
        raise HTTPException(503, detail="RUNTIME_NOT_READY")
    try:
        body = await repair_quality.export(state.workflow_service._repository, state.repair_quality_configuration,
            request.state.principal, str(workflow_run_id), evidence_digest, str(project_id))
    except AuthError as exc:
        raise HTTPException(exc.status_code, detail=exc.code) from None
    except CIError as exc:
        raise HTTPException(exc.status, detail=exc.code) from None
    return Response(body, media_type="application/json", headers={"X-Content-SHA256": evidence_digest,
                                                                   "Cache-Control": "no-store"})


from core.incidents.baseband_quality import Evaluate as BasebandEvaluate, EvaluateV2 as JointEvaluate, evaluate as evaluate_baseband


@router.post("/baseband/evaluate")
async def baseband_evaluate(body: BasebandEvaluate | JointEvaluate, request: Request):
    try:
        state = request.app.state
        return await evaluate_baseband(state.incident_service, state.ci_investigation_service.repository,
            state.baseband_quality_configuration, request.state.principal, body)
    except AuthError as exc:
        raise HTTPException(exc.status_code, detail=exc.code) from None
    except CIError as exc:
        raise HTTPException(exc.status, detail=exc.code) from None


@router.post("/exports")
async def export(body: QualityExport, request: Request):
    return await call(quality.export_facts(request.app.state.incident_service, human(request), body))


@router.post("/evaluate")
async def evaluate(body: QualityEvaluate, request: Request):
    try:
        require_scope(request.state.principal, SCOPE)
        return quality.evaluate(body)
    except AuthError as exc:
        raise HTTPException(exc.status_code, detail=exc.code) from None
    except CIError as exc:
        raise HTTPException(exc.status, detail=exc.code) from None


from core.incidents.coverage_quality import Evaluate as CoverageEvaluate, EvaluateV2 as CoverageEvaluateV2, evaluate as evaluate_coverage


@router.post("/coverage-execution/evaluate")
async def coverage_evaluate(body: CoverageEvaluate | CoverageEvaluateV2, request: Request):
    try:
        state = request.app.state
        return await evaluate_coverage(state.incident_service, state.baseband_quality_configuration, request.state.principal, body, state.loop_execution_repository)
    except AuthError as exc:
        raise HTTPException(exc.status_code, detail=exc.code) from None
    except CIError as exc:
        raise HTTPException(exc.status, detail=exc.code) from None
