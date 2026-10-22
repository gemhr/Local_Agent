"""分离 HUMAN 导出与精确 SERVICE 业务检查权限。"""
from fastapi import APIRouter, Request, HTTPException
from core.auth import require_scope, AuthError
from core.incidents.http import human, call
from core.incidents.contracts import QualityExport, QualityEvaluate, CIError
from core.incidents import quality

router = APIRouter(prefix="/api/ci/quality")
SCOPE = "localagent:ci-business:evaluate"


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
