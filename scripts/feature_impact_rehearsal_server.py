"""隔离本机演练进程：真实 JWT/PG/Redis，公开投影经私有管道交给测试驱动。"""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from sqlalchemy import select
    import jwt
    import uvicorn
    from core.settings import Settings
    from core.persistence.database import Database, DatabaseConfig
    from core.persistence.models import UserRow, RoleRow, UserRoleRow
    from core.feature_impact.service import FeatureImpactService
    from core.feature_impact.knowledge import development_input
    from core.feature_impact.app import create_app
    from core.auth import Principal
    envfile = ROOT / ".env.test"
    if envfile.exists():
        for line in envfile.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    from urllib.parse import urlsplit
    url = os.environ.get("LOCAL_AGENT_TEST_DATABASE_URL")
    if not url:
        from tests._pg_fixtures import test_database_url
        url = test_database_url()
    if urlsplit(url).hostname not in {"127.0.0.1", "localhost"} or not urlsplit(url).path.endswith("_test"):
        raise RuntimeError("ISOLATED_TEST_DATABASE_REQUIRED")
    os.environ["LOCAL_AGENT_DATABASE_URL"] = url
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    settings = replace(Settings.load(), jwt_public_key=public, jwt_issuer="feature-rehearsal", jwt_audience="feature-rehearsal", rate_limit_capacity=300)
    uid, service_uid = uuid4(), uuid4()
    tenant = "00000000-0000-0000-0000-000000000001"
    issued = datetime.now(timezone.utc)
    p = Principal(uid, str(uid), frozenset({"USER"}), uuid4().hex, issued, issued + timedelta(hours=1), tenant_id=tenant)
    scope = "localagent:feature-impact:evaluate"

    async def provision():
        db = Database(DatabaseConfig.from_settings(settings))
        try:
            async with db.transaction() as session:
                session.add_all([UserRow(id=uid, subject=str(uid), tenant_id=tenant),
                    UserRow(id=service_uid, subject=str(service_uid), tenant_id=tenant, principal_kind="SERVICE", service_scopes=[scope])])
                await session.flush()
                for role, target in (("USER", uid), ("SERVICE", service_uid)):
                    row = (await session.execute(select(RoleRow).where(RoleRow.code == role))).scalar_one_or_none()
                    if row is None:
                        row = RoleRow(id=uuid4(), code=role); session.add(row); await session.flush()
                    session.add(UserRoleRow(user_id=target, role_id=row.id))
            task = await FeatureImpactService(db).create(p, str(uuid4()))
            return task["task_id"]
        finally:
            await db.dispose()

    task_id = asyncio.run(provision())
    def token(subject, role, scopes):
        return jwt.encode(dict(iss=settings.jwt_issuer, aud=settings.jwt_audience, sub=str(subject), roles=[role], scopes=scopes,
            jti=uuid4().hex, iat=issued, nbf=issued, exp=issued + timedelta(hours=1)), private, algorithm="EdDSA")
    sock = socket.socket(); sock.bind(("127.0.0.1", int(os.environ.get("FEATURE_IMPACT_PORT", "0")))); port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
    limit = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < limit:
        time.sleep(.05)
    if not server.started:
        raise RuntimeError("BUSINESS_APP_START_FAILED")
    # 本行包含临时凭据，只能由父测试进程读取，禁止复制进日志或证据文件。
    print(json.dumps(dict(base_url=f"http://127.0.0.1:{port}", service_token=token(service_uid,"SERVICE",[scope]),
        human_token=token(uid,"USER",[]), task_id=task_id, input=dict(task_id=task_id, snapshot=development_input(), candidate_kind="RULE", candidate_version="dependency-lifecycle.v1", report=None)), ensure_ascii=False), flush=True)
    try:
        sys.stdin.readline()
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
    if thread.is_alive():
        raise RuntimeError("BUSINESS_APP_SHUTDOWN_FAILED")


if __name__ == "__main__":
    main()
