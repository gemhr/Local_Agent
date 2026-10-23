"""无模型业务进程；复用现有 PG、JWT、Redis 准入，不提供 Runtime 执行接口。"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from core.auth import AuthService, AuthError, require_scope
from core.persistence.database import Database, DatabaseConfig
from core.persistence.readiness import check_schema_readiness
from core.redis_service import RedisService, RedisTokenBucketRateLimiter, RedisUnavailableError
from core.feature_impact.http import router
from core.feature_impact.service import FeatureImpactService
from core.settings import Settings


def create_app(settings=None):
    settings = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app):
        database = Database(DatabaseConfig.from_settings(settings))
        redis = RedisService(settings)
        try:
            readiness = await check_schema_readiness(database)
            if not readiness.ready:
                raise RuntimeError("FEATURE_DATABASE_NOT_READY")
            app.state.feature_impact_service = FeatureImpactService(database)
            app.state.auth_service = AuthService(database, settings)
            app.state.rate_limiter = RedisTokenBucketRateLimiter(redis.client, settings)
            yield
        finally:
            await redis.close()
            await database.dispose()

    app = FastAPI(title="Feature Impact Business", lifespan=lifespan)
    app.include_router(router)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        try:
            p = await app.state.auth_service.authenticate(request.headers.get("authorization"))
            request.state.principal = p
            if request.url.path == "/api/feature-impact/quality/evaluate":
                require_scope(p, "localagent:feature-impact:evaluate")
            elif p.principal_kind != "HUMAN":
                return JSONResponse({"detail": "FEATURE_HUMAN_REQUIRED"}, status_code=403)
            decision = await app.state.rate_limiter.check(p.authz_domain_id)
            if not decision.allowed:
                return JSONResponse({"detail": "RATE_LIMITED"}, status_code=429)
        except AuthError as exc:
            return JSONResponse({"detail": exc.code}, status_code=exc.status_code)
        except RedisUnavailableError:
            return JSONResponse({"detail": "RATE_LIMIT_UNAVAILABLE"}, status_code=503)
        return await call_next(request)

    @app.get("/health")
    async def health():
        return dict(status="ok", mode="feature-impact-business", real_agent_execution="NOT_EXECUTED")
    return app
