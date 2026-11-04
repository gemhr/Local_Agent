"""Go 定向集成使用的真实 HTTP/PG 入口；模型只复述实际工具结果。"""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import tests.conftest  # 加载现有隔离测试配置，不输出连接凭据。
from tests._pg_fixtures import test_database_url
from tests.test_ci_status_routing import setup_query
from tests.test_agent_loop_runtime import native
from core.persistence.database import Database, DatabaseConfig
from core.persistence.models import RoleRow, UserRoleRow
from core.auth import AuthService
from core.agent_platform.adaptive_evaluation import Configuration, AdaptiveEvaluationService, register
from core.agent_platform.ci_diagnosis import CIInvestigationService
from core.runtime.agent_loop.quality_contract import catalog_digest
from core.runtime.model_invocation import ModelAdapterResponse
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from sqlalchemy import select
import jwt
import uvicorn


async def main():
    database = Database(DatabaseConfig(url=test_database_url(), use_null_pool=True))
    with TemporaryDirectory(prefix="adaptive-public-") as directory:
        q = await setup_query(database, Path(directory))

        def invoke(messages, *, max_tokens, generation_options, run_context):
            if run_context.loop_iteration_no == 1:
                response = native("ci_query_execution_status", "public-status")
                return replace(response, native_tool_calls=(response.native_tool_call,), native_tool_call=None)
            observed = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
            # 只从本次 Run 的真实 Tool Observation 中返回结果；不读 Go ExpectedOutput。
            content = observed[-1]["content"]
            return ModelAdapterResponse(content, finish_reason="stop", tool_call_count=0)

        q.model.invoke_request = invoke
        async with database.transaction() as session:
            role = await session.scalar(select(RoleRow).where(RoleRow.code == "USER"))
            if role is None:
                role = RoleRow(id=uuid4(), code="USER")
                session.add(role)
                await session.flush()
            session.add(UserRoleRow(user_id=q.principal.user_id, role_id=role.id))
        key = Ed25519PrivateKey.generate()
        auth = AuthService(database, NS(jwt_public_key=key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo),
            jwt_issuer="public-adaptive-test", jwt_audience="public-adaptive-test", jwt_allowed_algorithm="EdDSA", jwt_clock_skew_seconds=0))
        now = datetime.now(UTC)
        token = jwt.encode(dict(iss=auth.issuer, aud=auth.audience, sub=str(q.principal.user_id), roles=["USER"],
            jti=str(uuid4()), iat=now, nbf=now, exp=now + timedelta(minutes=15)), key, algorithm="EdDSA")
        detail = dict(execution_id=q.execution.execution_id, state_revision=q.execution.state_revision, summary=q.execution.summary)
        business = dict(slice="STATUS", incident_id=q.incident.incident_id, execution_id=q.execution.execution_id, execution_digest=catalog_digest(detail))
        expected = dict(schema_version="ci-status.v1", kind="STATUS", incident_id=q.incident.incident_id, **detail)
        # 私有父子进程管道传递一次性 token；不得写文件或日志。
        print(json.dumps(dict(principal_id=str(q.principal.user_id), tenant_id=q.principal.tenant_id,
            input=business, expected_output=expected, token=token)), flush=True)
        config = Configuration.model_validate(json.loads(await asyncio.to_thread(sys.stdin.readline)))
        app = FastAPI()
        app.state.adaptive_evaluation_service = AdaptiveEvaluationService(config, q.incidents, q.app,
            CIInvestigationService(q.incidents, q.app, q.repo, None, q.authorization, real_model=False), q.repo)
        from server import request_id_and_auth_middleware
        from core.redis_service import RedisTokenBucketRateLimiter
        app.state.auth_service = auth
        # 显式测试配置关闭限流；认证、HUMAN 授权及原 middleware 均实际执行。
        app.state.rate_limiter = RedisTokenBucketRateLimiter(None, NS(rate_limit_enabled=False, rate_limit_capacity=10, rate_limit_refill_rate=1))
        app.middleware("http")(request_id_and_auth_middleware)

        register(app)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        running = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started and not running.done():
            await asyncio.sleep(.01)
        print(json.dumps({"url": f"http://127.0.0.1:{port}"}), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        server.should_exit = True
        await running
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
