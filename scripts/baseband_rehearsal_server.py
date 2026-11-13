"""隔离测试库的完整后端演练，临时 JWT 仅通过父进程私有管道传递。"""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    for line in (ROOT / ".env.test").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    from tests._pg_fixtures import test_database_url
    from urllib.parse import urlsplit
    url = test_database_url()
    if urlsplit(url).hostname not in {"127.0.0.1", "localhost"} or not urlsplit(url).path.endswith("_test"):
        raise RuntimeError("ISOLATED_TEST_DATABASE_REQUIRED")
    os.environ["LOCAL_AGENT_DATABASE_URL"] = url
    for key, value in dict(LOCAL_AGENT_LLM_BACKEND="remote", LOCAL_AGENT_REMOTE_PROVIDER_KIND="deepseek",
            LOCAL_AGENT_REMOTE_API_BASE_URL="https://api.deepseek.com", LOCAL_AGENT_REMOTE_MODEL_NAME="deepseek-flash",
            LOCAL_AGENT_REMOTE_CONTEXT_WINDOW="1000000", LOCAL_AGENT_MODEL_MAX_TOKENS="4096",
            LOCAL_AGENT_REMOTE_ENABLE_THINKING="0", LOCAL_AGENT_REMOTE_TIMEOUT_SECONDS="120", LOCAL_AGENT_REMOTE_VERIFY_TLS="1").items():
        os.environ.setdefault(key, value)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from sqlalchemy import select
    from core.auth import Principal
    from core.settings import Settings
    from core.persistence.database import Database, DatabaseConfig
    from core.persistence.models import UserRow, RoleRow, UserRoleRow
    from core.incidents.service import IncidentService
    from core.ci_simulation.baseband import TEMPLATES
    import jwt
    import uvicorn
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    settings = replace(Settings.load(), jwt_public_key=public, jwt_issuer="baseband-rehearsal",
        jwt_audience="baseband-rehearsal", rate_limit_capacity=500)
    uid, service_uid = uuid4(), uuid4()
    tenant = "00000000-0000-0000-0000-000000000001"
    issued = datetime.now(timezone.utc)
    principal = Principal(uid, str(uid), frozenset({"USER"}), uuid4().hex, issued, issued + timedelta(hours=1), tenant_id=tenant)
    scope = "localagent:baseband:evaluate"

    async def provision():
        nonlocal uid
        db = Database(DatabaseConfig.from_settings(settings))
        try:
            replay = []
            if os.environ.get("BASEBAND_REPLAY_EVIDENCE"):
                from core.incidents.models import IncidentRow
                from core.ci_simulation.models import CIExecutionRow
                saved = json.loads(Path(os.environ["BASEBAND_REPLAY_EVIDENCE"]).read_text(encoding="utf-8"))
                async with db.session() as session:
                    for result in saved["candidate_results"]:
                        source = result["kernel_value"]["Artifact"]["Body"]["source"]
                        incident = await session.get(IncidentRow, source["incident_id"])
                        execution = await session.get(CIExecutionRow, source["execution_id"])
                        if incident is None or execution is None or incident.execution_id != execution.execution_id or incident.tenant_id != tenant:
                            raise RuntimeError("BASEBAND_REPLAY_SOURCE_INVALID")
                        if replay and uid != incident.owner_user_id:
                            raise RuntimeError("BASEBAND_REPLAY_OWNER_INVALID")
                        uid = incident.owner_user_id
                        replay.append(dict(incident_id=incident.incident_id, execution_id=execution.execution_id,
                            instance_id=execution.instance_id, execution_request_id=execution.request_id,
                            input=execution.execution_snapshot["baseband"], investigation_id=source["investigation_id"], run_id=source["run_id"]))
            async with db.transaction() as session:
                if not replay:
                    session.add(UserRow(id=uid, subject=str(uid), tenant_id=tenant))
                session.add(UserRow(id=service_uid, subject=str(service_uid), tenant_id=tenant, principal_kind="SERVICE", service_scopes=[scope]))
                await session.flush()
                for role, target in (("USER", uid), ("SERVICE", service_uid)):
                    if replay and role == "USER":
                        continue
                    row = await session.scalar(select(RoleRow).where(RoleRow.code == role))
                    if row is None:
                        row = RoleRow(id=uuid4(), code=role); session.add(row); await session.flush()
                    session.add(UserRoleRow(user_id=target, role_id=row.id))
            if replay:
                return replay
            service = IncidentService(db)
            sources = []
            for template in TEMPLATES:
                instance, _ = await service.create_scenario(principal, template["scenario_key"], str(uuid4()))
                request_id = str(uuid4())
                execution, incident, _ = await service.execute(principal, instance.instance_id, request_id)
                sources.append(dict(case_key=template["scenario_key"], instance_id=instance.instance_id, execution_request_id=request_id,
                    incident_id=incident.incident_id, execution_id=execution.execution_id, input=execution.execution_snapshot["baseband"]))
            return sources
        finally:
            await db.dispose()

    sources = asyncio.run(provision())
    def token(subject, role, scopes):
        return jwt.encode(dict(iss=settings.jwt_issuer, aud=settings.jwt_audience, sub=str(subject), roles=[role], scopes=scopes,
            jti=uuid4().hex, iat=issued, nbf=issued, exp=issued + timedelta(hours=1)), private, algorithm="EdDSA")
    with tempfile.TemporaryDirectory(prefix="baseband-rehearsal-") as temp:
        config = Path(temp) / "quality.json"
        config.write_text(json.dumps(dict(grants=[dict(project_id=os.environ.get("BASEBAND_PROJECT_ID", "baseband-development"),
            service_user_id=str(service_uid), tenant_id=tenant, owner_user_id=str(uid), incident_ids=[s["incident_id"] for s in sources])])), encoding="utf-8")
        import server as backend
        backend.settings = replace(settings, baseband_quality_configuration_path=str(config))
        sock = socket.socket(); sock.bind(("127.0.0.1", int(os.environ.get("BASEBAND_PORT", "0")))); port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(backend.app, log_level="error", access_log=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
        limit = time.monotonic() + 60
        while not server.started and thread.is_alive() and time.monotonic() < limit:
            time.sleep(.05)
        if not server.started:
            server.should_exit = True; thread.join(15); sock.close()
            raise RuntimeError("BASEBAND_BACKEND_START_FAILED")
        # 含临时凭据，父驱动禁止打印本行。
        print("BASEBAND_PRIVATE:" + json.dumps(dict(base_url=f"http://127.0.0.1:{port}", sources=sources,
            service_token=token(service_uid,"SERVICE",[scope]), human_token=token(uid,"USER",[]))), flush=True)
        try:
            sys.stdin.readline()
        finally:
            server.should_exit = True; thread.join(30); sock.close()
        if thread.is_alive():
            raise RuntimeError("BASEBAND_BACKEND_SHUTDOWN_FAILED")


if __name__ == "__main__":
    main()
