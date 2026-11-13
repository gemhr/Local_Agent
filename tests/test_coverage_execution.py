"""接口资产值域与审核→准备→人工执行的直接验证。"""
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4
import pytest
from sqlalchemy import select,func
from core.feature_impact.interface_impact import development_input
from core.feature_impact.reports import rule_report,validate
from core.feature_impact.knowledge import digest
from core.ci_simulation.daas_assets import build,validate_test,EXECUTOR
from core.feature_impact.service import FeatureImpactService,BusinessError
from core.feature_impact.models import TaskRow,ExecutionRow
from core.ci_simulation.models import CIExecutionRow,ScenarioInstanceRow
from core.incidents.models import IncidentRow
from core.incidents.service import IncidentService
from core.incidents.contracts import CIError
from tests.test_ci_incident_diagnosis import user


def test_interface_versions_and_legacy_dispatch():
    task=development_input();report,source=rule_report(task)
    finding=report["findings"][0];test=finding["recommended_tests"][0]
    assert finding["existing_coverage"]=="PARTIAL" and test["case_configuration"]["gain"]==2
    assert validate(report,task,kind="RULE",reads=source["read_sources"])==report
    assert build(task,test,EXECUTOR)["baseband"]["kdt"]["gain"]==2
    unchanged=deepcopy(task)
    unchanged["documents"][0]["facts"]["catalog"]["tags"][1]["allowed_values"]=[1]
    doc=unchanged["documents"][0]
    doc["content_digest"]=digest({k:doc[k] for k in ("title","content","facts")})
    unchanged["manifest"][0]["content_digest"]=doc["content_digest"]
    assert rule_report(unchanged)[0]["findings"][0]["recommended_tests"]==[]
    old=deepcopy(test)
    for key in ("feature_ref","interface_ref","tag_ref","message_ref","testcase_ref"):old[key]["version"]="1"
    with pytest.raises(ValueError):validate_test(task,old)
    old["case_configuration"]["gain"]=1
    assert validate_test(task,old)["case_configuration"]["gain"]==1
    mismatched=deepcopy(test);mismatched["tag_ref"]["version"]="1"
    with pytest.raises(ValueError):validate_test(task,mismatched)
    from core.feature_impact.knowledge import development_input as buffer_input
    legacy=buffer_input();legacy_report,reads=rule_report(legacy)
    assert legacy_report["schema_version"]=="feature-impact-report.v1"
    with pytest.raises(ValueError):validate(report,legacy,kind="RULE",reads=reads["read_sources"])
    assert "implementation" not in str(task) and "APPLIED" not in str(task)


@pytest.mark.asyncio
async def test_approved_asset_trigger_atomicity(clean_database,monkeypatch):
    p=replace(await user(clean_database),roles=frozenset({"USER","OPERATOR"}))
    other=await user(clean_database);feature=FeatureImpactService(clean_database);ci=IncidentService(clean_database)
    task=await feature.create(p,str(uuid4()),public_task="sampling-interface")
    report=await feature.submit(p,task["task_id"],str(uuid4()),rule=True)
    assert report["status"]=="SEALED" and report["candidate_version"]=="interface-coverage.v1"
    body=dict(request_id=str(uuid4()),report_version=report["report_version"],report_digest=report["report_digest"],
        decisions=[dict(finding_id=f["finding_id"],decision="ACCEPT",reason="人工核验新增值域覆盖") for f in report["report"]["findings"]])
    plan=await feature.review(p,report["report_id"],body);test=plan["tests"][0]
    prepare=dict(request_id=str(uuid4()),plan_digest=plan["plan_digest"],test_id=test["test_id"],executor_ref=EXECUTOR)
    asset=await feature.prepare_assets(p,plan["plan_id"],prepare)
    assert asset==await feature.prepare_assets(p,plan["plan_id"],prepare)
    assert asset["execution_status"]=="NOT_EXECUTED"
    for changed in (dict(test_id="not-approved"),dict(plan_digest="0"*64),dict(executor_ref="other-target")):
        with pytest.raises(BusinessError):await feature.prepare_assets(p,plan["plan_id"],prepare|changed)
    with pytest.raises(BusinessError):await feature.execute(p,plan["plan_id"],dict(request_id=str(uuid4()),plan_digest=plan["plan_digest"],test_id=test["test_id"]))
    async def counts():
        async with clean_database.session() as s:
            return [await s.scalar(select(func.count()).select_from(t)) for t in (CIExecutionRow,IncidentRow,ExecutionRow)]
    assert await counts()==[0,0,0]
    async with clean_database.session() as s:assert (await s.get(TaskRow,task["task_id"])).exposed_at is None
    trigger=str(uuid4());options=dict(case_id="integer-primary",expected_state_revision=1,asset_digest=asset["asset_digest"])
    with pytest.raises(CIError):await ci.execute(p,asset["instance_id"],trigger,case_id="integer-primary",expected_state_revision=1)
    with pytest.raises(CIError):await ci.execute(other,asset["instance_id"],trigger,**options)
    def failure():raise RuntimeError("rollback after execution flush")
    with monkeypatch.context() as m:
        m.setattr("core.incidents.service.uuid4",failure)
        with pytest.raises(RuntimeError):await ci.execute(p,asset["instance_id"],trigger,**options)
    assert await counts()==[0,0,0]
    async with clean_database.session() as s:assert (await s.get(TaskRow,task["task_id"])).exposed_at is None
    execution,incident,created=await ci.execute(p,asset["instance_id"],trigger,**options)
    retry,same,created_again=await ci.execute(p,asset["instance_id"],trigger,**options)
    assert created and not created_again and execution.execution_id==retry.execution_id and incident.incident_id==same.incident_id
    with pytest.raises(CIError):await ci.execute(p,asset["instance_id"],trigger,**(options|dict(case_id="integer-control")))
    with pytest.raises(CIError):await ci.execute(p,asset["instance_id"],trigger,**(options|dict(asset_digest="0"*64)))
    with pytest.raises(BusinessError):await feature.submit(p,task["task_id"],str(uuid4()),rule=True)
    assert await counts()==[1,1,0]
    receipt=next(e["content"] for e in execution.evidence_manifest if e["kind"]=="EXECUTION_RECEIPT")
    assert (receipt["actual"],receipt["expected"],execution.outcome)==(8,15,"FAIL")
    summary=next(e for e in execution.evidence_manifest if e["kind"]=="CI_SUMMARY")
    assert summary["digest"]==digest(summary["content"]) and execution.evidence_digest==digest(sorted(execution.evidence_manifest,key=lambda e:e["evidence_id"]))
    assert summary["content"]["workflow_source"]["plan_id"]==plan["plan_id"]
    assert "actor_user_id" not in summary["content"]["workflow_source"]
    assert (await feature.report(p,report["report_id"]))["executions"][0]["execution_id"]==execution.execution_id
    detail=await ci.detail(p,incident.incident_id)
    assert detail["diagnosis_source"]=="RULE" and detail["rule_diagnosis"]["candidate_findings"][0]["mechanism"]=="CONFIG_APPLIED_AFTER_SAMPLE"
    assert (await feature.plan(p,plan["plan_id"]))==plan
    # 未修改批准配置的原 HUMAN 复测仍可用；它是追加验证，不冒充原始 trigger。
    from core.incidents.verification import create_verification
    from core.incidents.contracts import VerificationIntent
    verification=await create_verification(ci,p,incident.incident_id,VerificationIntent(request_id=uuid4(),mode="SINGLE",case_ids=["integer-primary"],expected_state_revision=1))
    async with clean_database.session() as s:
        retest=await s.get(CIExecutionRow,verification["items"][0]["execution_id"])
        assert retest.execution_snapshot["trigger"]["verification_ref"]["verification_id"]==verification["verification_id"]
        assert retest.execution_snapshot["trigger"]["derivation"]["actual_configuration_digest"]==execution.summary["input_digest"]
