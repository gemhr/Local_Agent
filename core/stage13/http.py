"""独立受控 Provider HTTP composition；普通身份只有五层操作，无 GT/operator route。"""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets

from fastapi import Depends, FastAPI, Header
from fastapi.responses import JSONResponse, Response

from core.persistence.database import Database, DatabaseConfig
from core.stage13.contracts import (
    ArtifactRequest,
    DetailRequest,
    LookupRequest,
    ProviderError,
    RemoteRequest,
    SubmitRequest,
    WorkloadConfig,
)
from core.stage13.provider import ControlledCIProvider, ControlledFaults
from core.stage13.store import ProviderStore
from core.stage13.workload import Stage13Workload


def create_provider_app(
    provider: ControlledCIProvider, *, agent_token: str, owns_database=False
) -> FastAPI:
    """token 与 process-owned namespace/scope 绑定，不信任 caller 自报授权。"""
    if not agent_token:
        raise ValueError("Controlled Provider 必须配置 machine token")

    @asynccontextmanager
    async def lifespan(app):
        try:
            await provider.initialize()
            yield
        finally:
            if owns_database:
                await provider.store.database.dispose()

    async def authorized(authorization: str | None = Header(default=None)):
        if authorization is None or not secrets.compare_digest(
            authorization, f"Bearer {agent_token}"
        ):
            raise ProviderError("UNAUTHORIZED")

    app = FastAPI(
        title="Stage13 Controlled CI Provider",
        lifespan=lifespan,
        dependencies=[Depends(authorized)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        status = (
            401
            if exc.code == "UNAUTHORIZED"
            else (
                409
                if exc.code in {"CONFLICT", "KEY_CLOSED", "NAMESPACE_CONFLICT"}
                else 503
            )
        )
        return JSONResponse(status_code=status, content={"code": exc.code})

    @app.exception_handler(ValueError)
    async def invalid_request(request, exc):
        return JSONResponse(
            status_code=422, content={"code": "INVALID_CONTROLLED_REQUEST"}
        )

    def evidence_response(packet):
        # digest 对实际 HTTP body bytes；envelope metadata 在 header，避免自引用 digest。
        metadata = packet.model_dump(mode="json", exclude={"content"})
        return Response(
            packet.content.encode("utf-8"),
            media_type="application/json",
            headers={
                "X-Stage13-Evidence": json.dumps(
                    metadata, ensure_ascii=True, separators=(",", ":")
                ),
                "X-Content-SHA256": packet.digest,
            },
        )

    @app.post("/v1/submit")
    async def submit(request: SubmitRequest):
        receipt, replayed = await provider.submit_execution_with_replay(request)
        return JSONResponse(
            receipt.model_dump(mode="json"),
            headers={"X-Receipt-Replayed": str(replayed).lower()},
        )

    @app.post("/v1/lookup")
    async def lookup(request: LookupRequest):
        return await provider.lookup_by_business_key(request)

    @app.post("/v1/lookup-remote")
    async def lookup_remote(request: RemoteRequest):
        return await provider.lookup_by_remote_execution_id(request)

    @app.post("/v1/summary")
    async def summary(request: RemoteRequest):
        return evidence_response(await provider.get_ci_summary(request))

    @app.post("/v1/failure-detail")
    async def detail(request: DetailRequest):
        return evidence_response(await provider.fetch_failure_detail(request))

    @app.post("/v1/artifact")
    async def artifact(request: ArtifactRequest):
        return evidence_response(await provider.fetch_artifact(request))

    return app


def provider_app_from_environment() -> FastAPI:
    """uvicorn --factory 入口；连接串/token仅环境注入，不落盘/打印。"""
    config = WorkloadConfig.model_validate_json(
        Path(os.environ["STAGE13_PROVIDER_CONFIG_PATH"]).read_text(encoding="utf-8")
    )
    database = Database(DatabaseConfig(url=os.environ["STAGE13_PROVIDER_DATABASE_URL"]))
    workload = Stage13Workload(config)
    loss_keys = frozenset(
        json.loads(os.getenv("STAGE13_PROVIDER_RESPONSE_LOSS_KEYS", "[]"))
    )
    provider = ControlledCIProvider(
        ProviderStore(database, workload),
        faults=ControlledFaults(response_loss_keys=loss_keys),
    )
    return create_provider_app(
        provider,
        agent_token=os.environ["STAGE13_PROVIDER_AGENT_TOKEN"],
        owns_database=True,
    )
