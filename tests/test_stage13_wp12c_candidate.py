"""v4 版本隔离及真实 Runtime repair/replay 的授权输入边界。"""

import asyncio
import json
import pytest

from core.stage13.triage_candidate_v3 import candidate_v3_definitions
from core.stage13.triage_candidate_v4 import VERSION, candidate_v4_definitions
from core.stage13.triage_candidate_v4b import (
    VERSION as SECOND_VERSION,
    candidate_v4b_definitions,
)
from core.stage13.triage_subject import definition_digest, definitions
from tests.test_stage13_execution_policy import request
from tests.test_stage13_wp04_triage import assembly, output, payload


@pytest.mark.parametrize(
    "version,factory",
    [(VERSION, candidate_v4_definitions), (SECOND_VERSION, candidate_v4b_definitions)],
    ids=["v4-first", "v4-final"],
)
def test_v4_preserves_v3_identity_and_execution_profiles(version, factory):
    previous = candidate_v3_definitions(definitions())
    current = factory(definitions())
    assert definition_digest(previous[1]) == (
        "7decd49830c2776b5e3f655297ac88260e715f49242e4ad1774599af7c395199"
    )
    assert current[0] == previous[0]
    assert current[1].agent_version == version
    assert definition_digest(current[1]) != definition_digest(previous[1])
    assert current[1].allowed_tools == previous[1].allowed_tools
    assert current[1].model_profile_id == previous[1].model_profile_id


@pytest.mark.parametrize(
    "version,factory",
    [(VERSION, candidate_v4_definitions), (SECOND_VERSION, candidate_v4b_definitions)],
    ids=["v4-first", "v4-final"],
)
def test_v4_runtime_repair_and_replay_never_receive_gt(
    clean_database, monkeypatch, version, factory
):
    monkeypatch.setenv("LOCAL_AGENT_STAGE13_CANDIDATE_VERSION", version)

    async def run():
        p = payload()
        valid = output(p)
        service, calls, client = await assembly(
            clean_database, ["invalid", json.dumps(valid)]
        )
        try:
            manifest = service.manifests["ci_triage_candidate"]
            assert manifest["subject_version"] == version
            assert manifest["agent_definition_digest"] == definition_digest(
                factory(definitions())[1]
            )
            body = request(p, manifest)
            response = await service.evaluation_execute(**body)
            assert response["business_output_validation"]["status"] == "VALID"
            assert response["business_output_validation"]["output"] == valid
            assert len(calls) == 2
            assert await service.evaluation_execute(**body) == response
            assert len(calls) == 2
            for call in calls:
                wire = json.dumps(call, ensure_ascii=False, default=str)
                assert "RelevantRootCauses" not in wire
                assert "ExpectedFailureCategory" not in wire
        finally:
            await service.services.close(timeout=5)
            await client.aclose()

    asyncio.run(run())
