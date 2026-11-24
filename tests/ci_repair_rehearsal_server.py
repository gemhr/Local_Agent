"""独立 Go 集成测试使用的环回 HTTP 服务，仅读取已完成的测试 Workflow。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
from urllib.parse import urlsplit
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def controlled_failure(raw):
    from core.agent_platform.ci_repair_replay import evaluate_case
    from core.ci_simulation.tlv_input import PRIMARY_CASE
    from core.runtime.snapshot_serialization import canonical_json, sha256_digest, text_digest

    body = json.loads(raw)
    artifact, receipt, approval = (body[key] for key in ("artifact", "receipt", "approval"))
    after = artifact["after"].replace("11020007", "11020700")
    candidate = after.encode("utf-8")
    git_blob = hashlib.sha1(f"blob {len(candidate)}\0".encode() + candidate).hexdigest()
    patch = re.sub(r"(?m)^(index [a-f0-9]+\.\.)[a-f0-9]+( 100644)$", rf"\g<1>{git_blob}\g<2>",
                   artifact["diff"].replace("11020007", "11020700"))
    del artifact["artifact_digest"]
    artifact.update(after=after, diff=patch, after_sha256=text_digest(after), diff_digest=text_digest(patch),
                    explanation="受控错误候选：11020700")
    artifact["artifact_digest"] = sha256_digest(artifact)
    binding = json.loads(approval["binding"])
    binding["producer"]["artifact_ref"] = artifact
    replay = binding["replay"]
    locator = json.loads(replay["bound_input"])
    locator.update(artifact_digest=artifact["artifact_digest"], diff_digest=artifact["diff_digest"])
    replay["bound_input"] = canonical_json(locator)
    replay["input_digest"] = sha256_digest(replay["bound_input"])
    approval.update(binding=canonical_json(binding), binding_digest=sha256_digest(binding))
    del receipt["receipt_digest"]
    receipt.update(approval_binding_digest=approval["binding_digest"], artifact_digest=artifact["artifact_digest"],
                   diff_digest=artifact["diff_digest"], after_sha256=artifact["after_sha256"])
    receipt["results"]["candidate"][0] = evaluate_case(json.loads(body["interface"]), after, PRIMARY_CASE)
    receipt["receipt_digest"] = sha256_digest(receipt)
    honest_failure = canonical_json(body)
    del receipt["receipt_digest"]
    receipt["results"]["candidate"][0].update(oracle_status="PASS", reason=None)
    receipt["receipt_digest"] = sha256_digest(receipt)
    return honest_failure, canonical_json(body)


async def main():
    url = os.environ["LOCAL_AGENT_TEST_DATABASE_URL"]
    if urlsplit(url).hostname not in {"127.0.0.1", "localhost"} or not urlsplit(url).path.endswith("_test"):
        raise RuntimeError("ISOLATED_TEST_DATABASE_REQUIRED")
    os.environ["LOCAL_AGENT_DATABASE_URL"] = url

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from fastapi import Response
    import jwt
    from sqlalchemy import select
    import uvicorn
    import server as backend
    from core.agent_platform.workflow import WorkflowService
    from core.auth import AuthService, ObjectAuthorizationService
    from core.incidents.repair_quality import Configuration, SCOPE
    from core.persistence.database import Database, DatabaseConfig
    from core.persistence.models import RoleRow, UserRoleRow, UserRow, WorkflowRunRow
    from core.persistence.repositories.workflows import WorkflowRepository
    from core.redis_service import RedisTokenBucketRateLimiter
    from core.runtime.snapshot_serialization import canonical_json, sha256_digest, text_digest

    project = str(UUID(os.environ["CI_REPAIR_REHEARSAL_PROJECT"]))
    db = Database(DatabaseConfig(url=url, use_null_pool=True))
    repository = WorkflowRepository(db)
    private = Ed25519PrivateKey.generate()
    settings = replace(backend.settings, jwt_public_key=private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
                       jwt_issuer="ci-repair-rehearsal", jwt_audience="ci-repair-rehearsal", rate_limit_enabled=False)
    uid, other_uid, wrong_space_project = uuid4(), uuid4(), str(uuid4())
    async with db.transaction() as session:
        run = (await session.scalars(select(WorkflowRunRow).where(WorkflowRunRow.status == "SUCCEEDED",
            WorkflowRunRow.repair_baseline.is_not(None)))).one()
        service_role = await session.scalar(select(RoleRow).where(RoleRow.code == "SERVICE"))
        if service_role is None:
            service_role = RoleRow(id=uuid4(), code="SERVICE")
            session.add(service_role)
        for account in (uid, other_uid):
            session.add(UserRow(id=account, subject=str(account), tenant_id=run.tenant_id, principal_kind="SERVICE", service_scopes=[SCOPE]))
        await session.flush()
        session.add_all([UserRoleRow(user_id=account, role_id=service_role.id) for account in (uid, other_uid)])
    _, steps, _ = await repository.snapshot(run.workflow_run_id, lock=False)
    ref = steps[2].result_ref
    wire = ref["evidence"]
    failure, disagreement = controlled_failure(wire)
    altered = json.loads(wire)
    altered["approval"]["binding"] += "\n"
    # 保留原审批摘要，重算最外层响应摘要。Go 必须识别审批篡改。
    assert altered["approval"]["binding_digest"] != text_digest(altered["approval"]["binding"])
    assert altered["receipt"]["receipt_digest"] == sha256_digest(
        {key: value for key, value in altered["receipt"].items() if key != "receipt_digest"})
    changed_approval = canonical_json(altered)
    fixture_bodies = {text_digest(raw): raw for raw in (failure, disagreement, changed_approval)}

    def token(user, role, scopes):
        now = datetime.now(UTC)
        return jwt.encode(dict(iss=settings.jwt_issuer, aud=settings.jwt_audience, sub=str(user), roles=[role], scopes=scopes,
            jti=uuid4().hex, iat=now, nbf=now, exp=now + timedelta(minutes=30)), private, algorithm="EdDSA")

    service_token, human_token = token(uid, "SERVICE", [SCOPE]), token(run.owner_user_id, "USER", [])
    app_state = backend.app.state
    app_state.auth_service = AuthService(db, settings)
    app_state.rate_limiter = RedisTokenBucketRateLimiter(None, settings)
    app_state.workflow_service = WorkflowService(repository, {}, None, ObjectAuthorizationService(db), None, None, None)
    go_origin = os.environ["CI_REPAIR_REHEARSAL_GO_ORIGIN"]
    grant = dict(project_id=project, service_user_id=str(uid), tenant_id=run.tenant_id, owner_user_id=str(run.owner_user_id), space_id=str(run.space_id))
    app_state.repair_quality_configuration = Configuration.model_validate(dict(grants=[
        grant, dict(grant, project_id=wrong_space_project, space_id=str(uuid4()))],
        gate_sources=[dict(project_id=project, api_origin=go_origin, web_origin=go_origin, token_env="CI_REPAIR_REHEARSAL_GO_READ_KEY")]))

    async def app(scope, receive, send):
        # 仅明确标记的负例由测试层提供。正例及未知 digest 经真实生产路由和认证。
        if scope["type"] == "http" and scope["method"] == "GET":
            prefix = f"/api/ci/quality/repair-evidence/{run.workflow_run_id}/"
            digest = scope["path"].removeprefix(prefix)
            if scope["path"].startswith(prefix) and digest in fixture_bodies:
                auth = dict(scope["headers"]).get(b"authorization", b"")
                valid = secrets.compare_digest(auth, ("Bearer " + service_token).encode()) and scope["query_string"] == f"project_id={project}".encode()
                response = Response(fixture_bodies[digest].encode() if valid else b"", status_code=200 if valid else 403,
                                    media_type="application/json", headers={"X-Content-SHA256": digest})
                await response(scope, receive, send)
                return
        await backend.app(scope, receive, send)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error", access_log=False))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started and not task.done():
            await asyncio.sleep(.02)
        if not server.started:
            await task
            raise RuntimeError("CI_REPAIR_HTTP_START_FAILED")
        # 含隔离环境临时凭据，仅供测试父进程私有管道读取。
        print("CI_REPAIR_PRIVATE:" + json.dumps(dict(url=f"http://127.0.0.1:{port}", service=service_token, human=human_token,
            other_service=token(other_uid, "SERVICE", [SCOPE]), wrong_space_project=wrong_space_project,
            workflow=run.workflow_run_id, receipt=ref["receipt"], evidence_digest=ref["evidence_digest"],
            failure_digest=text_digest(failure), disagreement_digest=text_digest(disagreement),
            changed_approval_digest=text_digest(changed_approval))), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        server.should_exit = True
        await task
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
