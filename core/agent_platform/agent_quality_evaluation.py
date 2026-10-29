"""独立质量评测入口；只有持久准入的首次赢家调用现有 Application。"""
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException, Request
from fastapi.responses import Response

from core.agent_platform.application import ExecutionRequest, AgentApplicationError
from core.agent_platform.quality_configuration import load_configuration, check_agent
from core.auth import Principal, AuthorizationAction, EVALUATION_EXECUTE_SCOPE, require_scope
from core.persistence.repositories.agent_quality import AgentQualityReceiptRepository
from core.runtime.agent_loop.contracts import LoopSuspended
from core.runtime.agent_loop.quality_contract import QualityEvaluationRequest, PROTOCOL, canonical_bytes, catalog_digest
from core.runtime.memory_scope import MemoryAccessContext
from core.runtime.admission import RuntimeAdmissionRejectedError


def register_agent_quality_evaluation(app, *, settings, require_service, bind_run, authorize_run, final_answer_capture):
    def trusted(request, project=None):
        principal = request.state.principal
        if not all((settings.quality_evaluation_project_id, settings.quality_evaluation_service_principal_id,
                    settings.quality_evaluation_configuration_path)):
            raise HTTPException(503, "QUALITY_EVALUATION_DISABLED")
        require_scope(principal, EVALUATION_EXECUTE_SCOPE)
        if (not isinstance(principal, Principal) or principal.principal_kind != "SERVICE" or not principal.tenant_id
                or str(principal.user_id) != settings.quality_evaluation_service_principal_id
                or project is not None and project != settings.quality_evaluation_project_id):
            raise HTTPException(403, "QUALITY_EVALUATION_BINDING_FORBIDDEN")
        return principal

    def repository(request):
        execution = getattr(request.app.state, "loop_execution_repository", None)
        if execution is None:
            raise HTTPException(503, "RUNTIME_NOT_READY")
        return AgentQualityReceiptRepository(execution.database), execution

    def wire(encoded, status=200):
        return Response(content=encoded, status_code=status, media_type="application/json")

    def pending(run_id):
        return wire(canonical_bytes({"protocol_version": PROTOCOL, "run_id": run_id,
            "status": "ADMITTED", "capture_status": "NOT_READY"}), 202)

    async def authorize_existing(request, execution, receipt):
        from sqlalchemy import select
        from core.persistence.models import RuntimeRunExecutionRow
        async with execution.database.session() as session:
            exists = await session.scalar(select(RuntimeRunExecutionRow.run_id).where(RuntimeRunExecutionRow.run_id == receipt.remote_run_id))
        # T0 前的 Receipt 只返回202；一旦有执行事实/封存，始终复用原对象授权。
        if exists is not None or receipt.sealed_response is not None:
            await authorize_run(request, receipt.remote_run_id, AuthorizationAction.READ, required_scope=EVALUATION_EXECUTE_SCOPE)

    @app.post("/api/runtime/evaluation-execute/agent-quality/v1")
    async def execute(payload: QualityEvaluationRequest, request: Request):
        principal = trusted(request, str(payload.evaluation_project_id))
        from core.runtime.agent_loop.quality_contract import parse_quality_json
        raw = await request.body()
        if len(raw) > 512 * 1024:
            raise HTTPException(413, "QUALITY_REQUEST_LIMIT")
        try:
            parse_quality_json(raw)
        except ValueError:
            raise HTTPException(422, "QUALITY_REQUEST_JSON_INVALID") from None
        if payload.run_id != payload.attempt_id:
            raise HTTPException(422, "QUALITY_RUN_BINDING_INVALID")
        run_id = str(payload.run_id)
        session_id = str(uuid5(NAMESPACE_URL, "agent-quality-session:" + run_id))
        service = require_service()
        execution_request = ExecutionRequest(agent_id=payload.agent_id, input=payload.query, run_id=run_id,
            session_id=session_id, timeout_seconds=payload.timeout_milliseconds / 1000)
        try:
            manifest, profile = load_configuration(settings.quality_evaluation_configuration_path)
            _, definition, _, _, _ = service._resolve_request(execution_request)
            pin = check_agent(manifest, definition)
            if (catalog_digest(manifest) != payload.execution_manifest_digest
                    or (profile["id"], profile["version"], catalog_digest(profile)) !=
                       (payload.capture_profile_id, payload.capture_profile_version, payload.capture_profile_digest)):
                raise ValueError("QUALITY_REQUEST_PIN_INVALID")
        except (ValueError, OSError, AgentApplicationError):
            raise HTTPException(422, "QUALITY_CONFIGURATION_PIN_INVALID") from None
        receipts, execution = repository(request)
        try:
            # 只覆盖 Receipt 事务；长执行由原 Runtime construction/RunRegistry 管理。
            with service._chat_service.admission_gate.admission():
                winner, receipt = await receipts.admit(correlation=payload.correlation(), principal=principal,
                    manifest=manifest, profile=profile)
        except RuntimeAdmissionRejectedError:
            raise HTTPException(503, "RUNTIME_SHUTTING_DOWN") from None
        except ValueError:
            raise HTTPException(409, "QUALITY_REQUEST_CONFLICT") from None
        if not winner:
            await authorize_existing(request, execution, receipt)
            return wire(receipt.sealed_response) if receipt.sealed_response is not None else pending(run_id)
        # 此后任何异常/进程退出都不会再次授予 Execute；恢复仅由原 Runtime Owner 负责。
        access = MemoryAccessContext.from_principal(principal, agent_id=payload.agent_id, session_id=session_id)
        await bind_run(request, run_id=run_id, agent_id=payload.agent_id, session_id=session_id)
        try:
            result = await service.execute_evaluation(execution_request, execution_policy=pin["execution_policy"],
                loop_correlation={"evaluation_run_id": str(payload.evaluation_run_id),
                    "attempt_id": str(payload.attempt_id), "request_id": str(payload.request_id)},
                memory_access_context=access)
        except LoopSuspended:
            return pending(run_id)
        final_status, _, answer = final_answer_capture(output=result.output, result=result)
        if final_status != "COMPLETE":
            answer = None
        encoded = await receipts.capture(execution, run_id, final_answer=answer)
        return wire(encoded) if encoded is not None else pending(run_id)

    from core.agent_platform.ci_synthetic_evaluation import register
    register(app,settings=settings,trusted=trusted,repository=repository,wire=wire,require_service=require_service,
        bind_run=bind_run,authorize_existing=authorize_existing,final_answer_capture=final_answer_capture)

    @app.get("/api/runtime/runs/{run_id}/agent-quality-evidence/v1")
    async def read(run_id: str, request: Request):
        principal = trusted(request)
        try:
            UUID(run_id)
        except ValueError:
            raise HTTPException(422, "INVALID_RUN_ID") from None
        receipts, execution = repository(request)
        receipt = await receipts.read(run_id)
        if (receipt is None or receipt.principal_id != str(principal.user_id) or receipt.tenant_id != principal.tenant_id
                or receipt.evaluation_project_id != settings.quality_evaluation_project_id):
            raise HTTPException(404, "QUALITY_RECEIPT_UNAVAILABLE")
        await authorize_existing(request, execution, receipt)
        if receipt.sealed_response is not None:
            return wire(receipt.sealed_response)
        encoded = await receipts.capture(execution, run_id)
        return wire(encoded) if encoded is not None else pending(run_id)
