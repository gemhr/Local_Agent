"""Stage8-WP5 静态 Web Demo 的最小 HTTP 合同测试。"""

import httpx
import pytest

import server


@pytest.mark.asyncio
async def test_stage8_web_route_and_static_assets(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    transport = httpx.ASGITransport(app=server.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/stage8")
        script = await client.get("/static/stage8/app.js")
        stylesheet = await client.get("/static/stage8/styles.css")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "AgentCore" in page.text
    assert "Mission Workflow" in page.text
    assert script.status_code == 200
    assert "textContent" in script.text
    assert "innerHTML" not in script.text
    assert "localStorage" not in script.text
    assert "Authorization" in script.text
    assert "/api/stage8/ci/runs/${encodeURIComponent(payload.ci_run_id)}/analyze" in script.text
    assert "/api/stage8/missions/${state.missionId}/transition" in script.text
    assert "READY_FOR_EXECUTION" in script.text
    assert "CONTEXT_READY" in script.text
    assert "agents/failure-triage/run" not in script.text
    assert "Tool Approval Pending" in script.text
    assert "Observe Once" in page.text
    assert "Submit SUCCESS" not in page.text
    assert "PRODUCT-Like Failure" not in page.text
    assert "Test-Data-Like Failure" not in page.text
    assert "submit-success" not in page.text
    assert "submit-product" not in page.text
    assert "submit-test-data" not in page.text
    assert (
        "`/api/stage8/execution-jobs/${state.job.job_id}/observe`, "
        "{ method: 'POST', body: JSON.stringify({}) }"
    ) in script.text
    assert "/api/stage8/executions/${state.job.execution_id}/result" not in script.text
    assert "submitResult" not in script.text
    assert stylesheet.status_code == 200
    route_paths = {route.path for route in server.app.routes if hasattr(route, "path")}
    assert "/api/stage8/agents/ci-guardian/run" not in route_paths
    assert "/api/stage8/ci/runs/{ci_run_id}/analyze" in route_paths
    assert "/api/stage8/execution-jobs/{job_id}/observe" in route_paths
