"""独立 Loop 评测协议；只读证据查询不触发执行或恢复。"""

import uuid

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from core.agent_platform.application import AgentApplicationError, ExecutionRequest
from core.auth import AuthorizationAction, EVALUATION_EXECUTE_SCOPE, require_scope
from core.runtime.agent_loop.contracts import LoopSuspended
from core.runtime.agent_loop.evidence import capture_loop_evidence

PROTOCOL = "localagent-agent-loop-evaluation-execute.v1"


class LoopEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: StrictStr = Field(min_length=1, max_length=255)
    query: StrictStr = Field(max_length=65536)
    run_id: uuid.UUID
    evaluation_project_id: uuid.UUID
    evaluation_run_id: uuid.UUID
    attempt_id: uuid.UUID
    request_id: uuid.UUID
    timeout_seconds: float = Field(default=300, gt=0, le=300, allow_inf_nan=False)


def register_agent_loop_evaluation(app, *, settings, require_service, bind_run,
                                   authorize_run, authorization_service, final_answer_capture):
    """装配现有 Application、对象授权和 durable repository。"""
    def trusted(request, project_id=None):
        principal = request.state.principal
        if not settings.loop_evaluation_project_id or not settings.loop_evaluation_service_principal_id:
            raise HTTPException(503, "LOOP_EVALUATION_DISABLED")
        require_scope(principal, EVALUATION_EXECUTE_SCOPE)
        if (principal.principal_kind != "SERVICE"
                or str(principal.user_id) != settings.loop_evaluation_service_principal_id
                or (project_id is not None and project_id != settings.loop_evaluation_project_id)):
            raise HTTPException(403, "LOOP_EVALUATION_BINDING_FORBIDDEN")

    def repository(request):
        repo = getattr(request.app.state, "loop_execution_repository", None)
        if repo is None:
            raise HTTPException(503, "RUNTIME_NOT_READY")
        return repo

    @app.post("/api/runtime/evaluation-execute/agent-loop/v1")
    async def execute_loop(payload: LoopEvaluationRequest, request: Request):
        trusted(request, str(payload.evaluation_project_id))
        run_id = str(payload.run_id)
        if payload.run_id != payload.attempt_id:
            raise HTTPException(422, "LOOP_EVALUATION_RUN_BINDING_INVALID")
        service = require_service()
        # 验证注册 policy 后才绑定对象；不会给普通请求提供 policy 覆盖参数。
        from core.runtime.agent_loop.contracts import LOOP_POLICY
        execution = ExecutionRequest(agent_id=payload.agent_id, input=payload.query,
                                     run_id=run_id, timeout_seconds=payload.timeout_seconds)
        try:
            _, definition, _, _, _ = service._resolve_request(execution)
            if definition.business_options.get("execution_policy") != LOOP_POLICY:
                raise AgentApplicationError("LOOP_POLICY_REQUIRED", "Agent 未启用 durable Loop 策略")
            await bind_run(request, run_id=run_id, agent_id=payload.agent_id)
            result = await service.execute_agent_loop(execution, loop_correlation={
                "evaluation_run_id": str(payload.evaluation_run_id),
                "attempt_id": str(payload.attempt_id), "request_id": str(payload.request_id)})
        except LoopSuspended:
            return JSONResponse(status_code=202, content={"protocol_version": PROTOCOL,
                "run_id": run_id, "status": "ADMITTED", "capture_status": "NOT_READY"})
        except AgentApplicationError as exc:
            raise HTTPException(422, exc.error_code) from None
        final_status, final_error, answer = final_answer_capture(output=result.output, result=result)
        sealed = await capture_loop_evidence(repository(request), run_id)
        return JSONResponse(content={"protocol_version": PROTOCOL, "run_id": result.run_id,
            "status": result.status.value, "stop_reason": result.stop_reason.value,
            "error_code": result.error_code, "safe_message": result.safe_message,
            "capture_status": sealed["body"]["coverage"], "capture_error_code": None,
            "loop_evidence": sealed["body"], "loop_evidence_digest": sealed["digest"],
            "final_answer_capture_status": final_status, "final_answer_capture_error_code": final_error,
            "final_answer_evidence": answer})

    @app.get("/api/runtime/runs/{run_id}/loop-evidence/v1")
    async def read_evidence(run_id: str, request: Request):
        trusted(request)
        try:
            uuid.UUID(run_id)
        except ValueError:
            raise HTTPException(422, "INVALID_RUN_ID") from None
        await authorize_run(request, run_id, AuthorizationAction.READ, required_scope=EVALUATION_EXECUTE_SCOPE)
        await authorization_service(request)._require_existing_binding(request.state.principal, "RUN", run_id)
        try:
            sealed = await capture_loop_evidence(repository(request), run_id)
        except ValueError as exc:
            if str(exc) == "LOOP_EVIDENCE_NOT_READY":
                return JSONResponse(status_code=202, content={"schema_version": "loop-evidence-query.v1",
                    "run_id": run_id, "capture_status": "NOT_READY", "loop_evidence": None,
                    "loop_evidence_digest": None})
            raise HTTPException(404, "LOOP_EVIDENCE_UNAVAILABLE") from None
        return {"schema_version": "loop-evidence-query.v1", "run_id": run_id,
                "capture_status": sealed["body"]["coverage"],
                "loop_evidence": sealed["body"], "loop_evidence_digest": sealed["digest"]}
