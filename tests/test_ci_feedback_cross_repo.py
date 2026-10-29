"""单条 Python 实际反馈投影 → Go HTTP Intake/PG/Review 联调，不调用模型。"""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from uuid import UUID, uuid4

import pytest
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from sqlalchemy.engine import make_url

from core.incidents import feedback
from core.incidents.http import router
from tests._pg_fixtures import test_database_url as resolve_test_database_url
from tests.test_ci_investigation_feedback import sealed, intent
from tests.test_ci_incident_resolution import setup_incident


@pytest.mark.asyncio
async def test_python_projection_go_review_http_postgres(clean_database):
    service, principal, _, _, incident = await setup_incident(clean_database)
    row = await sealed(service, principal, incident)
    saved, _ = await feedback.submit(service, principal, incident.incident_id,
        intent(row).model_copy(update={'decision': 'REJECT', 'reason': 'SECRET_REASON_DO_NOT_EXPORT'}))
    app = FastAPI()
    app.state.incident_service = service
    token = str(uuid4())
    @app.middleware('http')
    async def trusted_test_identity(request: Request, call_next):
        if request.headers.get('Authorization') != 'Bearer ' + token:
            from starlette.responses import JSONResponse
            return JSONResponse({'error': 'UNAUTHORIZED'}, status_code=401)
        request.state.principal = principal
        return await call_next(request)
    app.include_router(router)
    @app.post('/test/withdraw')
    async def withdraw():
        await feedback.submit(service, principal, incident.incident_id,
            intent(row).model_copy(update={'decision': 'WITHDRAW', 'supersedes': UUID(saved['feedback_id'])}))
        return {'status': 'RECORDED'}
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    repo = Path(__file__).resolve().parents[2] / 'AgentEvalOps'
    dsn = make_url(resolve_test_database_url())
    assert dsn.host in ('127.0.0.1', 'localhost') and dsn.port == 5433 and dsn.database.endswith('_test')
    env = dict(os.environ)
    env.update(AC_FEEDBACK_TEST_URL=f'http://127.0.0.1:{port}', AC_FEEDBACK_TEST_TOKEN=token,
        AC_FEEDBACK_TEST_CONNECTION=json.dumps(dict(base_url=f'http://127.0.0.1:{port}', token_env='AC_FEEDBACK_TEST_TOKEN',
            tenant_id=principal.tenant_id, owner_user_id=str(principal.user_id))),
        AC_FEEDBACK_TEST_INCIDENT=incident.incident_id, AC_FEEDBACK_TEST_ID=saved['feedback_id'],
        AC_FEEDBACK_TEST_PG_URL=dsn.set(drivername='postgresql', database='postgres').render_as_string(hide_password=False),
        G1_TEST_PYTHON=str(repo / 'backend/.venv/Scripts/python.exe'))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.05)
        assert server.started, 'local Python producer failed to start'
        result = await asyncio.to_thread(subprocess.run,
            ['go', 'test', '-tags=integration', './internal/postgres', '-run', '^TestBusinessFeedbackPythonHTTP$', '-count=1', '-v'],
            cwd=repo / 'go-backend', env=env, capture_output=True, text=True, encoding='utf-8', timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'Python Feedback -> authenticated HTTP projection' in result.stdout
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        sock.close()
        assert not thread.is_alive(), 'producer shutdown incomplete'
