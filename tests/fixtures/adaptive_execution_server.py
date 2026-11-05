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
from core.auth import ObjectAuthorizationService
from core.agent_platform.application import AgentApplicationService
from core.chat_service import ChatService
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


class ObservedInvestigator:
    """受控取证行为只读本轮真实 Observation，不持有 Case/GT/执行 manifest。"""
    def __init__(self):
        self.requests = {}

    def invoke_request(self, messages, *, max_tokens, generation_options, run_context):
        from tests.test_ci_adaptive_stop import calls
        from core.agent_platform.ci_evidence import minimum_coverage
        self.requests.setdefault(run_context.run_id, []).append(dict(iteration=run_context.loop_iteration_no,
            tool_choice=generation_options.get("tool_choice"), tool_names=[t["function"]["name"] for t in generation_options.get("tools", [])]))
        observed = [json.loads(json.loads(m["content"])["content"]) for m in messages if m.get("role") == "tool"]
        contexts = [v for v in observed if "evidence" in v]
        if not contexts:
            return calls(("context", "ci_get_execution_context", {}))
        context = contexts[-1]
        seen = {v["evidence_id"]: v for v in observed if "evidence_id" in v}
        required = [e for kind in ("PARSER_LOG", "ENVIRONMENT_RECORD", "CASE_REQUIREMENTS")
            for e in context["evidence"] if e["kind"] == kind and e["availability"] == "AVAILABLE"]
        pending = [e for e in required if e["evidence_id"] not in seen]
        if pending and generation_options.get("tool_choice") != "none":
            return calls(*[("read-" + e["kind"], "ci_read_execution_evidence", {"evidence_id": e["evidence_id"]}) for e in pending[:2]])
        sufficient = minimum_coverage(seen)
        # 沿用 WP2 的公开确定性额外比较读取，只验证限制，不冒充真实模型收益。
        comparison = next(e for e in context["evidence"] if e["kind"] == "COMPARISON_LOG")
        if sufficient and generation_options.get("tool_choice") != "none" and comparison["evidence_id"] not in seen:
            return calls(("comparison", "ci_read_execution_evidence", {"evidence_id": comparison["evidence_id"]}))
        report = dict(schema_version="ci-diagnosis.v1", kind="REPORT", incident_id=context["incident_id"],
            diagnosis_status="SUPPORTED" if sufficient else "INSUFFICIENT_EVIDENCE", failure_stage="PARSER" if sufficient else "UNKNOWN",
            root_cause_hypothesis="已观察解析错误与环境能力，需核对字段版本兼容性" if sufficient else "现有观察不足以确认根因",
            supporting_evidence=[dict(evidence_id=e["evidence_id"], digest=e["digest"]) for e in seen.values() if e["kind"] in {"PARSER_LOG", "ENVIRONMENT_RECORD", "CASE_REQUIREMENTS"}],
            uncertainties=[] if sufficient else ["缺少有效解析日志或环境能力记录"],
            recommended_checks=["核对解析与解码能力记录"], recommended_owner_role="TOOL_MAINTAINER" if sufficient else "UNKNOWN")
        return ModelAdapterResponse(json.dumps(report, ensure_ascii=False), finish_reason="stop", tool_call_count=0)


async def diagnosis_setup(database, directory):
    from tests.test_ci_adaptive_stop import build
    from tests.test_ci_incident_resolution import setup_incident
    from core.incidents.verification import create_verification
    from core.incidents.contracts import VerificationIntent
    q = await build(database, directory)
    q.app = AgentApplicationService(ChatService(q.factory._router, coordinated_runtime_factory=q.factory), q.factory._agent_registry)
    q.authorization = ObjectAuthorizationService(database)
    provider = ObservedInvestigator()
    q.model.invoke_request = provider.invoke_request
    _, _, _, limited, limited_incident = await setup_incident(database, "limited_diagnostics", principal=q.principal)
    verification = await create_verification(q.incidents, q.principal, q.incident.incident_id,
        VerificationIntent(request_id=uuid4(), mode="SINGLE", case_ids=[q.execution.case_id], expected_state_revision=q.execution.state_revision))
    from core.ci_simulation.models import CIExecutionRow
    async with database.session() as session:
        verified = await session.get(CIExecutionRow, verification["items"][0]["execution_id"])
    cases = {}
    for key, slice, incident, execution in (("S1", "STATUS", q.incident, q.execution), ("S2", "STATUS", q.incident, verified),
            ("D1", "DIAGNOSIS", q.incident, q.execution), ("D2", "DIAGNOSIS", limited_incident, limited)):
        detail = await q.incidents.execution_detail(q.principal, incident.incident_id, execution.execution_id)
        projection = dict(execution_id=detail["execution_id"], state_revision=detail["state_revision"], summary=detail["summary"])
        cases[key] = dict(input=dict(slice=slice, incident_id=incident.incident_id, execution_id=execution.execution_id, execution_digest=catalog_digest(projection)),
            expected_output=dict(schema_version="ci-status.v1", kind="STATUS", incident_id=incident.incident_id, **projection) if slice == "STATUS" else None,
            ground_truth=None, expected_business="STATUS" if slice == "STATUS" else "SUPPORTED" if key == "D1" else "INSUFFICIENT_EVIDENCE",
            facts=dict(instance_id=execution.instance_id, state_revision=execution.state_revision, summary=execution.summary,
                execution_snapshot=execution.execution_snapshot, evidence_manifest=execution.evidence_manifest, evidence_digest=execution.evidence_digest))
    return q, provider, cases


async def main():
    database = Database(DatabaseConfig(url=test_database_url(), use_null_pool=True))
    with TemporaryDirectory(prefix="adaptive-public-") as directory:
        diagnosis = "--diagnosis" in sys.argv
        if diagnosis:
            q, provider, cases = await diagnosis_setup(database, Path(directory))
        else:
            q = await setup_query(database, Path(directory))

        def invoke(messages, *, max_tokens, generation_options, run_context):
            if run_context.loop_iteration_no == 1:
                response = native("ci_query_execution_status", "public-status")
                return replace(response, native_tool_calls=(response.native_tool_call,), native_tool_call=None)
            observed = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
            # 只从本次 Run 的真实 Tool Observation 中返回结果；不读 Go ExpectedOutput。
            content = observed[-1]["content"]
            return ModelAdapterResponse(content, finish_reason="stop", tool_call_count=0)

        if not diagnosis:
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
            input=business, expected_output=expected, token=token, **({"cases": cases} if diagnosis else {}))), flush=True)
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
        command = await asyncio.to_thread(sys.stdin.readline)
        if diagnosis and command.strip():
            audit = {}
            for run_id in json.loads(command)["audit"]:
                image = await q.repo.load(run_id)
                frame = next(f for f in image.loop_frames if f.frame_role == "ROOT")
                last = max(image.context_snapshots, key=lambda s: s.version)
                audit[run_id] = dict(stop=frame.adaptive_decision, seed_digest=frame.seed_digest,
                    observation_cursors=[i.observation_cursor for i in image.loop_iterations if i.state == "OBSERVED"],
                    committed_tools=sum(t.state == "COMMITTED" for t in image.tool_invocations),
                    final_options=last.payload["options"], requests=provider.requests[run_id])
            print(json.dumps(audit, ensure_ascii=False), flush=True)
        server.should_exit = True
        await running
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
