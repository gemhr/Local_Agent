"""一个节点验证真实 PG 导出、权限和离线 v1/v2 重放。"""
import base64
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from core.ci_simulation.models import CIExecutionRow, NightlyCIBatchRow
from core.incidents.contracts import QualityExport, QualityEvaluate, CIError
from core.incidents import quality
from tests.test_nightly_ci_business import setup, finish


@pytest.mark.asyncio
async def test_frozen_export_and_rule_replay(clean_database):
    service, operator, a, b, batch_id = await setup(clean_database, 6)
    final = await finish(service, operator, batch_id)
    facts = final["frozen_summary"]["items"]
    def find(env, software="product-2.0", outcome="FAIL"):
        return next(f for f in facts if f["environment_id"] == env and f["software_version"] == software and f["outcome"] == outcome)
    first = find("env-parser-a")
    export = await quality.export_facts(service, a, QualityExport(execution_ids=[first["execution_id"], find("env-parser-a", outcome="PASS")["execution_id"]]))
    assert len(export["observations"]["records"]) == 2
    assert export["exported_by_user_id"] == str(a.user_id)
    assert "current_state" not in str(export) and "scenario_key" not in str(export)
    for principal in (operator, b, replace(a, tenant_id="other")):
        with pytest.raises(CIError, match="CI_NOT_FOUND"):
            await quality.export_facts(service, principal, QualityExport(execution_ids=[first["execution_id"]]))
    with pytest.raises(CIError, match="CI_HUMAN_REQUIRED"):
        await quality.export_facts(service, replace(a, principal_kind="SERVICE"), QualityExport(execution_ids=[first["execution_id"]]))
    left = next(f for f in export["observations"]["records"] if first["execution_id"] in base64.b64decode(f["canonical_bytes_b64"]).decode())
    async def frozen(principal, item):
        return (await quality.export_facts(service, principal, QualityExport(execution_ids=[item["execution_id"]])))["observations"]["records"][0]
    cases = [(b, find("env-parser-b"), True, True), (b, find("env-parser-b", "product-2.1"), False, True),
        (a, find("env-config"), False, False), (b, find("env-input"), False, False),
        (a, find("env-buffer", "product-2.1"), False, False), (b, find("env-evidence"), False, False)]
    for principal, fact, v1, v2 in cases:
        observations = dict(protocol="ci-business-observations.v1", kind="PAIR", records=[left, await frozen(principal, fact)], repair=None)
        for version, expected in zip(quality.RULES, (v1, v2)):
            body = dict(protocol="ci-business-quality-request.v1", project_id=str(uuid4()), run_id=str(uuid4()), attempt_id=str(uuid4()), request_id=str(uuid4()),
                case_id="pair", frozen_input_digest=quality.sha256_digest(observations), rule_version=version, observations=observations)
            result = quality.evaluate(QualityEvaluate.model_validate(body))
            assert result["pair_candidate"]["candidate"] is expected
            assert result["record_consistency"]["violations"] == []
            assert result["definition_digest"] == quality.definition_digest(version)
            with pytest.raises(ValidationError):
                QualityEvaluate.model_validate(dict(body, GroundTruth={"canary": "DO_NOT_SEND"}))
    repair_export = await quality.export_facts(service, a, QualityExport(incident_ids=[first["incident_id"]], include_repair=True))
    repair_body = QualityEvaluate.model_validate(dict(body, observations=repair_export["observations"]))
    result = quality.evaluate(repair_body)
    assert result["repair_consistency"]["eligible"] is False and result["repair_consistency"]["violations"] == []
    async with clean_database.session() as session:
        batch = await session.get(NightlyCIBatchRow, batch_id)
        assert batch.sealed_digest == final["sealed_digest"]
        for record in (await session.execute(select(CIExecutionRow))).scalars():
            assert record.state_revision == 1
