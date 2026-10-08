"""WP10B 版本与真实 Runtime repair 边界回归；推理质量由正式模型评测验证。"""

import asyncio
import json

from core.stage13.triage_candidate_v3 import VERSION, candidate_v3_definitions
from core.stage13.triage_candidate_vnext import candidate_definitions, VERSION as OLD
from core.stage13.triage_subject import definition_digest, definitions
from tests.test_stage13_execution_policy import request
from tests.test_stage13_wp04_triage import assembly, output, payload


def test_v2_is_unchanged_and_v3_has_new_identity_and_same_capabilities():
    previous = candidate_definitions(definitions(), OLD)
    current = candidate_v3_definitions(definitions())
    assert (
        definition_digest(previous[1])
        == "6da5430304382d6c7e233d40b1061b1b930567a87111ef924505e4c5d575f234"
    )
    assert current[0] == previous[0]
    assert current[1].agent_version == VERSION
    assert definition_digest(current[1]) != definition_digest(previous[1])
    assert current[1].allowed_tools == previous[1].allowed_tools
    assert current[1].model_profile_id == previous[1].model_profile_id


def test_v3_runtime_registration_and_repair_preserve_authorized_business_output(
    clean_database, monkeypatch
):
    monkeypatch.setenv("LOCAL_AGENT_STAGE13_CANDIDATE_VERSION", VERSION)

    async def run():
        p = payload()
        valid = output(p)
        service, calls, client = await assembly(
            clean_database, ["invalid", json.dumps(valid)]
        )
        try:
            manifest = service.manifests["ci_triage_candidate"]
            assert manifest["subject_version"] == VERSION
            assert manifest["agent_definition_digest"] == definition_digest(
                candidate_v3_definitions(definitions())[1]
            )
            body = request(p, manifest)
            response = await service.evaluation_execute(**body)
            assert response["business_output_validation"]["status"] == "VALID"
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
