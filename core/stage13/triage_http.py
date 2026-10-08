"""显式 opt-in 的 Stage13 API；标准应用的 v2/v3/v4 路由不变。"""

from contextlib import asynccontextmanager
import hashlib
import hmac
import os

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from core.llm_engine import RemoteLLMEngine
from core.persistence.database import Database, DatabaseConfig
from core.settings import Settings
from core.stage13.triage_runtime import compose


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    agent_id: str
    query: str
    timeout_seconds: float = Field(gt=0, le=180)
    expected_subject_manifest: dict
    execution_policy: dict | None = None


def configured_model(settings):
    return {
        "provider": settings.remote_provider_kind,
        "model": settings.remote_model_name,
        "revision": os.getenv("LOCAL_AGENT_STAGE13_MODEL_REVISION") or None,
        "context_window": settings.remote_context_window,
        "max_tokens": settings.model_max_tokens,
        "temperature": 0,
        "thinking": False,
        "retry_attempts": 1,
        "endpoint_digest": hashlib.sha256(
            settings.remote_api_base_url.encode()
        ).hexdigest(),
    }


@asynccontextmanager
async def lifespan(app):
    if os.getenv("LOCAL_AGENT_STAGE13_ENABLED") != "1":
        raise RuntimeError("STAGE13_OPT_IN_REQUIRED")
    settings = Settings.load()
    database = Database(DatabaseConfig.from_settings(settings))
    engine = RemoteLLMEngine(
        settings.remote_api_base_url,
        settings.remote_model_name,
        api_key=settings.remote_api_key,
        timeout_seconds=settings.remote_timeout_seconds,
        verify_tls=settings.remote_verify_tls,
        enable_thinking=False,
        provider_kind=settings.remote_provider_kind,
        trust_env=settings.remote_trust_env,
    )
    service = None
    try:
        await database.verify_reachable()
        service = await compose(
            database,
            engine,
            configured_model(settings),
            os.environ["LOCAL_AGENT_STAGE13_SCOPE"],
        )
        app.state.stage13_service = service
        yield
    finally:
        if service:
            await service.services.close(timeout=20)
        await engine.aclose()
        await database.close()


app = FastAPI(lifespan=lifespan)
router = APIRouter()


@router.get("/api/runtime/evaluation-subjects/stage13/v1")
async def evaluation_subjects(request: Request):
    """向已认证评测方提供实际注册的冻结主体，不导出输入或 GT。"""
    expected_token = os.getenv("LOCAL_AGENT_STAGE13_SERVICE_TOKEN", "")
    if not expected_token or not hmac.compare_digest(
        request.headers.get("authorization", ""), "Bearer " + expected_token
    ):
        raise HTTPException(401, "SERVICE_CREDENTIAL_REQUIRED")
    service = getattr(request.app.state, "stage13_service", None)
    if service is None:
        raise HTTPException(503, "STAGE13_OPT_IN_REQUIRED")
    return {"subjects": list(service.manifests.values())}


@router.post("/api/runtime/evaluation-execute/stage13/v1")
async def evaluation_execute(body: EvaluationRequest, request: Request):
    expected_token = os.getenv("LOCAL_AGENT_STAGE13_SERVICE_TOKEN", "")
    if not expected_token or not hmac.compare_digest(
        request.headers.get("authorization", ""), "Bearer " + expected_token
    ):
        raise HTTPException(401, "SERVICE_CREDENTIAL_REQUIRED")
    service = getattr(request.app.state, "stage13_service", None)
    if service is None:
        raise HTTPException(503, "STAGE13_OPT_IN_REQUIRED")
    try:
        return await service.evaluation_execute(**body.model_dump())
    except TimeoutError:
        raise HTTPException(504, "STAGE13_REQUEST_DEADLINE_EXCEEDED") from None
    except (ValueError, KeyError) as exc:
        code = str(exc)
        allowed = {
            "IDENTITY_MISMATCH",
            "NON_EXECUTABLE_SUBJECT",
            "RUN_BINDING_CONFLICT",
            "UNAUTHORIZED_INPUT_FIELDS",
            "INPUT_BUDGET_EXCEEDED",
            "ANALYSIS_DEADLINE_EXCEEDED",
            "ACTUAL_RUNTIME_IDENTITY_MISMATCH",
        }
        raise HTTPException(
            409, code if code in allowed else "STAGE13_CONTRACT_REJECTED"
        ) from None


app.include_router(router)
