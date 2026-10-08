"""WP08 版本保留、注册信息授权和离线证据 scope 边界。"""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from core.stage13.triage_candidate_vnext import VERSION, candidate_definitions
from core.stage13.triage_subject import definitions, definition_digest
from tests.test_stage13_execution_policy import request
from tests.test_stage13_wp04_triage import assembly, output, payload


def test_original_definitions_preserved_and_new_candidate_is_distinct():
    old = definitions()
    assert (
        definition_digest(old[0])
        == "9d7ff3490fc5aee8bec8537fcfe676caba3d93be3a3106511b20027cd15e6c81"
    )
    assert (
        definition_digest(old[1])
        == "ca473a5a06eba7b16358ea988e838d483b86794fa707587a7e9a799b71055c0f"
    )
    new = candidate_definitions(old, VERSION)
    assert new[0] is old[0]
    assert new[1].agent_id == old[1].agent_id
    assert new[1].agent_version != old[1].agent_version
    assert definition_digest(new[1]) != definition_digest(old[1])
    assert new[1].allowed_tools == old[1].allowed_tools
    assert new[1].model_profile_id == old[1].model_profile_id
    with pytest.raises(ValueError, match="UNKNOWN_STAGE13_CANDIDATE_VERSION"):
        candidate_definitions(old, "unknown")


def test_subject_manifest_endpoint_requires_service_auth(monkeypatch):
    from core.stage13.triage_http import app

    monkeypatch.setenv("LOCAL_AGENT_STAGE13_SERVICE_TOKEN", "unit-token")
    manifest = {"subject_id": "ci_triage_candidate", "subject_version": VERSION}
    monkeypatch.setattr(
        app.state,
        "stage13_service",
        SimpleNamespace(manifests={"candidate": manifest}),
        raising=False,
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://unit"
        ) as client:
            path = "/api/runtime/evaluation-subjects/stage13/v1"
            assert (await client.get(path)).status_code == 401
            response = await client.get(
                path, headers={"Authorization": "Bearer unit-token"}
            )
            assert response.json() == {"subjects": [manifest]}

    asyncio.run(run())


def test_offline_authorization_inherits_only_absent_evidence_scope(
    clean_database, monkeypatch
):
    monkeypatch.setenv("LOCAL_AGENT_STAGE13_CANDIDATE_VERSION", VERSION)

    async def run():
        p = payload()
        del p["visible_evidence"][0]["owner_scope_id"]
        service, calls, client = await assembly(
            clean_database, ["invalid", json.dumps(output(p))]
        )
        try:
            manifest = service.manifests["ci_triage_candidate"]
            assert manifest["subject_version"] == VERSION
            with pytest.raises(ValueError, match="EVIDENCE_SCOPE_DENIED"):
                await service.reserve_run(str(uuid4()), json.dumps(p), manifest)
            wrong = deepcopy(p)
            wrong["visible_evidence"][0]["owner_scope_id"] = "other-scope"
            with pytest.raises(ValueError, match="EVIDENCE_SCOPE_DENIED"):
                await service.evaluation_execute(**request(wrong, manifest))
            assert calls == []
            body = request(p, manifest)
            response = await service.evaluation_execute(**body)
            assert response["business_output_validation"]["status"] == "VALID"
            assert len(calls) == 2
            assert await service.evaluation_execute(**body) == response
            assert len(calls) == 2
        finally:
            await service.services.close(timeout=5)
            await client.aclose()

    asyncio.run(run())
