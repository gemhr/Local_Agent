"""隔离测试库的真实后端演练；临时 JWT 只走父进程私有管道。"""
import asyncio
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from uuid import uuid4,UUID

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    for line in (ROOT/".env.test").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k,v=line.split("=",1);os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))
    from tests._pg_fixtures import test_database_url
    from urllib.parse import urlsplit
    url=test_database_url();parts=urlsplit(url)
    if parts.hostname not in {"127.0.0.1","localhost"} or not parts.path.endswith("_test"): raise RuntimeError("ISOLATED_TEST_DATABASE_REQUIRED")
    os.environ["LOCAL_AGENT_DATABASE_URL"]=url
    os.environ["LOCAL_AGENT_STAGE21_BASEBAND_TOKEN_EXCEPTION"]="0"
    os.environ.setdefault("LOCAL_AGENT_REMOTE_API_BASE_URL","https://api.deepseek.com")
    from core.settings import Settings
    from core.persistence.database import Database,DatabaseConfig
    from core.persistence.models import UserRow,RoleRow,UserRoleRow,TenantRow
    from core.incidents.models import IncidentRow
    from core.ci_simulation.models import CIExecutionRow
    from core.feature_impact.models import TaskRow
    from sqlalchemy import select
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
    import jwt
    import uvicorn
    private=Ed25519PrivateKey.generate();public=private.public_key().public_bytes(Encoding.PEM,PublicFormat.SubjectPublicKeyInfo).decode()
    settings=replace(Settings.load(),jwt_public_key=public,jwt_issuer="coverage-rehearsal",jwt_audience="coverage-rehearsal",rate_limit_capacity=500)
    uid,service_uid=uuid4(),uuid4();tenant="00000000-0000-0000-0000-000000000001";scope="localagent:baseband:evaluate"
    selector=None;snapshot=None
    async def provision():
        nonlocal uid,selector,snapshot
        db=Database(DatabaseConfig.from_settings(settings))
        try:
            async with db.transaction() as session:
                if await session.get(TenantRow,tenant) is None: session.add(TenantRow(tenant_id=tenant));await session.flush()
                replay=os.getenv("COVERAGE_REPLAY_EVIDENCE")
                if replay:
                    saved=json.loads(Path(replay).read_text(encoding="utf-8"));selector=saved["selector"]
                    incident=await session.get(IncidentRow,selector["incident_id"])
                    execution=await session.get(CIExecutionRow,selector["execution_id"])
                    task=await session.get(TaskRow,selector["task_id"])
                    if (incident is None or execution is None or task is None or incident.execution_id!=execution.execution_id
                            or incident.owner_user_id!=task.owner_user_id or task.tenant_id!=tenant): raise RuntimeError("COVERAGE_REPLAY_BINDING_INVALID")
                    uid=incident.owner_user_id;snapshot=task.snapshot
                else: session.add(UserRow(id=uid,subject=str(uid),tenant_id=tenant))
                session.add(UserRow(id=service_uid,subject=str(service_uid),tenant_id=tenant,principal_kind="SERVICE",service_scopes=[scope]))
                await session.flush()
                for role,target in (("OPERATOR",uid),("SERVICE",service_uid)):
                    if replay and role=="OPERATOR":continue
                    r=await session.scalar(select(RoleRow).where(RoleRow.code==role))
                    if r is None:r=RoleRow(id=uuid4(),code=role);session.add(r);await session.flush()
                    session.add(UserRoleRow(user_id=target,role_id=r.id))
        finally:await db.dispose()
    asyncio.run(provision())
    now=datetime.now(timezone.utc)
    def token(sub,role,scopes):return jwt.encode(dict(iss=settings.jwt_issuer,aud=settings.jwt_audience,sub=str(sub),roles=[role],scopes=scopes,jti=uuid4().hex,iat=now,nbf=now,exp=now+timedelta(hours=1)),private,algorithm="EdDSA")
    with tempfile.TemporaryDirectory(prefix="coverage-rehearsal-") as directory:
        config=Path(directory)/"quality.json"
        grants=[dict(project_id=os.getenv("BASEBAND_PROJECT_ID","coverage-development"),service_user_id=str(service_uid),tenant_id=tenant,owner_user_id=str(uid),incident_ids=[selector["incident_id"]])] if selector else []
        config.write_text(json.dumps(dict(grants=grants)),encoding="utf-8")
        import server as backend
        backend.settings=replace(settings,baseband_quality_configuration_path=str(config))
        sock=socket.socket();sock.bind(("127.0.0.1",int(os.getenv("COVERAGE_PORT","0"))));port=sock.getsockname()[1]
        appserver=uvicorn.Server(uvicorn.Config(backend.app,log_level="error",access_log=False))
        thread=threading.Thread(target=lambda:appserver.run(sockets=[sock]),daemon=True);thread.start()
        end=time.monotonic()+60
        while not appserver.started and thread.is_alive() and time.monotonic()<end:time.sleep(.05)
        if not appserver.started:raise RuntimeError("COVERAGE_BACKEND_START_FAILED")
        print("COVERAGE_PRIVATE:"+json.dumps(dict(base_url=f"http://127.0.0.1:{port}",human_token=token(uid,"OPERATOR",[]),service_token=token(service_uid,"SERVICE",[scope]),selector=selector,
            input=dict(schema_version="coverage-execution-input.v1",task=snapshot,executor_ref="local-integer-executor@1") if snapshot else None)),flush=True)
        try:sys.stdin.readline()
        finally:appserver.should_exit=True;thread.join(30);sock.close()
        if thread.is_alive():raise RuntimeError("COVERAGE_BACKEND_SHUTDOWN_FAILED")


if __name__=="__main__":main()
