"""分离 HUMAN 导出与精确 SERVICE 业务检查权限。"""
from fastapi import APIRouter, Request, HTTPException
from core.auth import require_scope, AuthError
from core.incidents.http import human, call
from core.incidents.contracts import QualityExport, QualityEvaluate, CIError
from core.incidents import quality

router = APIRouter(prefix="/api/ci/quality")
SCOPE = "localagent:ci-business:evaluate"


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
