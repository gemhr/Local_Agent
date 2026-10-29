"""CI 工作台的 HUMAN 业务 API。"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from core.ci_simulation.execution import SCENARIOS
from core.incidents.contracts import (CIError, CreateScenario, Intent, ExecutionIntent, ActionIntent,
    ConfirmAction, VerificationIntent, CloseIncident)
from core.incidents import operations, verification, feedback
from core.incidents.feedback import FeedbackIntent


def human(request: Request):
    principal = getattr(request.state, "principal", None)
    if principal is None or principal.principal_kind != "HUMAN":
        raise HTTPException(403, detail="CI_HUMAN_REQUIRED")
    return principal


router = APIRouter(prefix="/api/ci", dependencies=[Depends(human)])


async def call(awaitable):
    try:
        return await awaitable
    except CIError as exc:
        raise HTTPException(exc.status, detail=exc.code) from None


@router.get("/scenarios")
async def scenarios():
    return {"items": SCENARIOS}


@router.post("/scenarios")
async def create_scenario(body: CreateScenario, request: Request, response: Response):
    row, created = await call(request.app.state.incident_service.create_scenario(human(request), body.scenario_key, str(body.request_id)))
    response.status_code = 201 if created else 200
    return dict(instance_id=row.instance_id, scenario_key=row.scenario_key, created_at=row.created_at)


@router.post("/scenarios/{instance_id}/executions")
async def execute(instance_id: str, body: ExecutionIntent, request: Request, response: Response):
    execution, incident, created = await call(request.app.state.incident_service.execute(human(request), instance_id, str(body.request_id),
        case_id=body.case_id, expected_state_revision=body.expected_state_revision))
    response.status_code = 201 if created else 200
    return dict(execution_id=execution.execution_id, outcome=execution.outcome,
        incident_id=incident.incident_id if incident else None, completed_at=execution.completed_at, state_revision=execution.state_revision)


@router.get("/incidents")
async def incidents(request: Request, limit: int = Query(20, ge=1, le=50)):
    return {"items": await call(request.app.state.incident_service.list_incidents(human(request), limit))}


@router.get("/incidents/{incident_id}")
async def detail(incident_id: str, request: Request):
    return await call(request.app.state.incident_service.detail(human(request), incident_id))


@router.post("/incidents/{incident_id}/investigations", status_code=202)
async def start(incident_id: str, body: Intent, request: Request):
    return await call(request.app.state.ci_investigation_service.start(human(request), incident_id, str(body.request_id)))


@router.get("/incidents/{incident_id}/investigations/{investigation_id}")
async def investigation(incident_id: str, investigation_id: str, request: Request):
    return await call(request.app.state.ci_investigation_service.get(human(request), incident_id, investigation_id))


@router.get("/incidents/{incident_id}/evidence/{evidence_id}")
async def evidence(incident_id: str, evidence_id: str, request: Request):
    return await call(request.app.state.incident_service.read_evidence(human(request), incident_id, evidence_id))


@router.get("/incidents/{incident_id}/state")
async def state(incident_id: str, request: Request):
    return await call(operations.current_state(request.app.state.incident_service, human(request), incident_id))


@router.post("/incidents/{incident_id}/actions", status_code=201)
async def preview_action(incident_id: str, body: ActionIntent, request: Request):
    return await call(operations.create_action(request.app.state.incident_service, human(request), incident_id, body))


@router.get("/incidents/{incident_id}/actions/{action_id}")
async def action(incident_id: str, action_id: str, request: Request):
    return await call(operations.read_action(request.app.state.incident_service, human(request), incident_id, action_id))


@router.post("/incidents/{incident_id}/actions/{action_id}/confirm")
async def confirm(incident_id: str, action_id: str, body: ConfirmAction, request: Request):
    return await call(operations.confirm_action(request.app.state.incident_service, human(request), incident_id, action_id, body))


@router.post("/incidents/{incident_id}/verifications", status_code=201)
async def verify(incident_id: str, body: VerificationIntent, request: Request):
    return await call(verification.create_verification(request.app.state.incident_service, human(request), incident_id, body))


@router.get("/incidents/{incident_id}/verifications/{verification_id}")
async def verification_result(incident_id: str, verification_id: str, request: Request):
    return await call(verification.read_verification(request.app.state.incident_service, human(request), incident_id, verification_id))


@router.get("/incidents/{incident_id}/executions/{execution_id}")
async def execution_detail(incident_id: str, execution_id: str, request: Request):
    return await call(request.app.state.incident_service.execution_detail(human(request), incident_id, execution_id))


@router.get("/incidents/{incident_id}/executions/{execution_id}/evidence/{evidence_id}")
async def execution_evidence(incident_id: str, execution_id: str, evidence_id: str, request: Request):
    return await call(request.app.state.incident_service.execution_detail(human(request), incident_id, execution_id, evidence_id))


@router.post("/incidents/{incident_id}/close")
async def close(incident_id: str, body: CloseIncident, request: Request):
    return await call(verification.close_incident(request.app.state.incident_service, human(request), incident_id, body))


@router.post("/incidents/{incident_id}/feedback")
async def create_feedback(incident_id: str, body: FeedbackIntent, request: Request, response: Response):
    result, created = await call(feedback.submit(request.app.state.incident_service, human(request), incident_id, body))
    response.status_code = 201 if created else 200
    return result


@router.get("/incidents/{incident_id}/feedback")
async def feedback_history(incident_id: str, request: Request):
    return await call(feedback.history(request.app.state.incident_service, human(request), incident_id))


@router.get("/incidents/{incident_id}/feedback/{feedback_id}/projection")
async def feedback_projection(incident_id: str, feedback_id: str, request: Request):
    from core.incidents.feedback_projection import project
    return await call(project(request.app.state.incident_service, human(request), incident_id, feedback_id))
