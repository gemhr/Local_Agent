"""只验证新合同与直接兼容路径；不把规则当独立评价参考。"""
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4
import pytest
from core.feature_impact.public_tasks import load_public_task
from core.feature_impact.reports import rule_report, validate
from core.feature_impact.agent import definition, approved_experiment
from core.feature_impact.service import FeatureImpactService, BusinessError
from core.feature_impact.knowledge import digest
from tests.test_ci_incident_diagnosis import user


def test_conditions_order_reuse_unknown_and_version_dispatch():
    task=load_public_task("interface-message-analysis")
    report,source=rule_report(task)
    hot,cold=report["findings"]
    assert hot["recommendation"]=="ADD" and hot["recommended_tests"][0]["test_kind"]=="PLANNING_ONLY"
    assert set(hot["condition_codes"])=={"FEATURE_ENABLED","HOT_RECONFIG","FIRST_MANAGEMENT_MESSAGE"}
    assert cold["recommendation"]=="REUSE" and cold["existing_case_refs"]==["case:message:cold"] and not cold["recommended_tests"]
    altered=deepcopy(task)
    case=next(d for d in altered["documents"] if d["source_ref"]=="case:message:cold")
    case["facts"]["steps"].reverse()
    case["content_digest"]=digest({k:case[k] for k in ("title","content","facts")})
    altered["manifest"]=[{k:d[k] for k in ("source_ref","version","chunk","content_digest")} for d in altered["documents"]]
    assert rule_report(altered)[0]["findings"][1]["recommendation"]=="ADD"
    unknown,_=rule_report(load_public_task("interface-recovery-analysis"))
    f=unknown["findings"][0]
    assert f["recommendation"]=="CLARIFY" and f["existing_coverage"]=="UNKNOWN" and len(f["clarification_keys"])==3 and not f["recommended_tests"]
    invalid=deepcopy(report);invalid["findings"][0]["supporting_evidence"][0]["content_digest"]="0"*64
    with pytest.raises(ValueError): validate(invalid,task,kind="RULE",reads=source["read_sources"])
    assert definition("1.1.0").agent_version=="1.1.0" and definition("1.2.0").agent_version=="1.2.0"
    assert approved_experiment(task) and not approved_experiment(load_public_task("sampling-interface"))
    old,_=rule_report(load_public_task("sampling-interface"));assert old["schema_version"]=="feature-impact-report.v2"
    unsupported=deepcopy(report);unsupported["findings"][0]["recommended_tests"]=[old["findings"][0]["recommended_tests"][0]]
    with pytest.raises(ValueError,match="DAAS_CATALOG_NOT_AVAILABLE"):
        validate(unsupported,task,kind="RULE",reads=source["read_sources"])


@pytest.mark.asyncio
async def test_planning_only_rejected_and_sealed_source(clean_database):
    p=replace(await user(clean_database),roles=frozenset({"OPERATOR"}))
    other=await user(clean_database);service=FeatureImpactService(clean_database)
    task=await service.create(p,str(uuid4()),public_task="interface-message-analysis-dev")
    with pytest.raises(BusinessError):await service.task(other,task["task_id"])
    report=await service.submit(p,task["task_id"],str(uuid4()),rule=True)
    assert report["status"]=="SEALED" and report["schema_version"]=="feature-impact-report.v3"
    plan=await service.review(p,report["report_id"],dict(request_id=str(uuid4()),report_version=report["report_version"],report_digest=report["report_digest"],decisions=[dict(finding_id=f["finding_id"],decision="ACCEPT",reason="采纳规划条件，需后续实现") for f in report["report"]["findings"]]))
    body=dict(request_id=str(uuid4()),plan_digest=plan["plan_digest"],test_id=plan["tests"][0]["test_id"])
    with pytest.raises(BusinessError,match="PLANNING_ONLY_NOT_EXECUTABLE"):
        await service.execute(p,plan["plan_id"],body)
    with pytest.raises(BusinessError,match="PLANNING_ONLY_NOT_EXECUTABLE"):
        await service.prepare_assets(p,plan["plan_id"],body|dict(executor_ref="local-integer-executor@1"))
    from core.feature_impact.planning_quality import evaluate
    principal=replace(p,principal_kind="SERVICE",scopes=frozenset({"localagent:feature-impact:evaluate"}))
    value=dict(schema_version="feature-impact-input.v2",snapshot={k:v for k,v in task.items() if k not in {"task_id","agent_execution","model_budget","exposed"}})
    request=dict(protocol="feature-impact-request.v2",**{k:str(uuid4()) for k in ("project_id","run_id","attempt_id","request_id")},case_id="development",input=value,frozen_input_digest=digest(value),candidate_kind="RULE",candidate_version="interface-conditions.v1",selector=dict(task_id=task["task_id"],report_id=report["report_id"],run_id=None))
    output=await evaluate(service,None,principal,request)
    assert output["report"]==report["report"] and output["source_record"]["report_id"]==report["report_id"]
    request["candidate_version"]="wrong"
    with pytest.raises(BusinessError):await evaluate(service,None,principal,request)
