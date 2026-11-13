"""已批准 CoveragePlan 到准备资产、人工触发及唯一 CI 事实的事务接点。"""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from core.feature_impact.models import TaskRow, ReportRow, PlanRow
from core.feature_impact.knowledge import digest
from core.feature_impact.service import FeatureImpactService, BusinessError
from core.ci_simulation.models import ScenarioInstanceRow, CIExecutionRow
from core.ci_simulation.daas_assets import build
from core.ci_simulation.service import CIError


async def approved(session,principal,plan_id,test_id,*,lock=False,trusted_owner=None):
    plan=await session.get(PlanRow,plan_id)
    if plan is None: raise BusinessError("FEATURE_NOT_FOUND",404)
    if trusted_owner is None:
        task,report=await FeatureImpactService(None).owned_report(session,principal,plan.report_id,lock=lock)
    else:
        report=await session.get(ReportRow,plan.report_id)
        task=await session.get(TaskRow,report.task_id) if report else None
        if (principal.principal_kind!="SERVICE" or lock or task is None or (task.tenant_id,str(task.owner_user_id))!=trusted_owner):
            raise BusinessError("COVERAGE_SOURCE_DENIED",403)
    test=next((t for t in plan.payload["tests"] if t["test_id"]==test_id),None)
    if (test is None or test.get("test_kind")!="DAAS_CASE"
            or not any(d["finding_id"]==test["finding_id"] and d["decision"]=="ACCEPT" for d in plan.payload["decisions"])):
        raise BusinessError("TEST_NOT_APPROVED")
    if (task.snapshot.get("analysis_contract")!="interface-impact.v1" or report.rejection
            or digest({k:v for k,v in task.snapshot.items() if k!="input_digest"})!=task.snapshot["input_digest"]
            or digest(report.payload)!=report.envelope["report_digest"]
            or digest({k:v for k,v in plan.payload.items() if k!="plan_digest"})!=plan.payload["plan_digest"]
            or plan.payload["report_digest"]!=report.envelope["report_digest"]
            or plan.payload["reviewer"]!=str(task.owner_user_id)):
        raise BusinessError("COVERAGE_SOURCE_INVALID")
    finding=next(f for f in report.payload["findings"] if f["finding_id"]==test["finding_id"])
    original=next((t for t in finding["recommended_tests"] if t["test_id"]==test_id),None)
    if original!={k:v for k,v in test.items() if k not in {"finding_id","source"}}:
        raise BusinessError("COVERAGE_SOURCE_INVALID")
    source=dict(task_id=task.task_id,task_input_digest=task.snapshot["input_digest"],corpus_digest=task.snapshot["corpus_digest"],
        report_id=report.report_id,report_version=report.report_version,report_digest=report.envelope["report_digest"],
        candidate_kind=report.envelope["candidate_kind"],candidate_version=report.envelope["candidate_version"],
        plan_id=plan.plan_id,plan_version=plan.payload["plan_version"],plan_digest=plan.payload["plan_digest"],finding_id=test["finding_id"],test_id=test_id,test_digest=digest(test),
        approval_ref=dict(plan_id=plan.plan_id,review_request_id=plan.request_id,reviewer=plan.payload["reviewer"]))
    return task,report,plan,test,source


def public_asset(row,executions=()):
    state=row.current_state
    return dict(instance_id=row.instance_id,asset_digest=state["case_asset"]["asset_digest"],state_revision=row.state_revision,
        case_asset=state["case_asset"],workflow_source=state["workflow_source"],distribution=state["distribution"],
        execution_status="EXECUTED" if executions else "NOT_EXECUTED",executions=list(executions),
        execution_intent=dict(case_id=state["test_case"]["case_id"],expected_state_revision=row.state_revision),
        model_execution="NOT_EXECUTED",incremental_agent_value="NOT_VALIDATED")


async def prepare(service,principal,plan_id,body):
    async with service.database.transaction() as session:
        task,report,plan,test,source=await approved(session,principal,plan_id,body["test_id"],lock=True)
        if body["plan_digest"]!=plan.payload["plan_digest"]: raise BusinessError("PLAN_VERSION_CONFLICT")
        intent=digest(dict(plan_id=plan_id,**body))
        old=await session.scalar(select(ScenarioInstanceRow).where(ScenarioInstanceRow.tenant_id==principal.tenant_id,
            ScenarioInstanceRow.owner_user_id==principal.user_id,ScenarioInstanceRow.request_id==body["request_id"]))
        if old:
            if old.current_state.get("preparation_intent_digest")!=intent: raise BusinessError("REQUEST_CONFLICT")
            return public_asset(old,await references(session,old.instance_id))
        state=build(task.snapshot,test,body["executor_ref"])
        source["asset_digest"]=state["case_asset"]["asset_digest"]
        distribution=dict(mode="LOCAL_SIMULATED",target_ref=body["executor_ref"],distribution_id=str(uuid4()),
            request_id=body["request_id"],actor_kind="HUMAN",actor_user_id=str(principal.user_id),recorded_at=datetime.now(timezone.utc).isoformat(),asset_digest=source["asset_digest"])
        distribution["distribution_digest"]=digest(distribution)
        state.update(workflow_source=source,distribution=distribution,preparation_intent_digest=intent)
        identity=str(uuid4())
        await session.execute(insert(ScenarioInstanceRow).values(instance_id=identity,tenant_id=principal.tenant_id,owner_user_id=principal.user_id,
            request_id=body["request_id"],scenario_key="coverage-daas-case",template_version="1",state_revision=1,current_state=state,created_at=datetime.now(timezone.utc))
            .on_conflict_do_nothing(index_elements=["tenant_id","owner_user_id","request_id"]))
        row=await session.scalar(select(ScenarioInstanceRow).where(ScenarioInstanceRow.tenant_id==principal.tenant_id,
            ScenarioInstanceRow.owner_user_id==principal.user_id,ScenarioInstanceRow.request_id==body["request_id"]))
        if row.current_state.get("preparation_intent_digest")!=intent: raise BusinessError("REQUEST_CONFLICT")
        return public_asset(row)


async def references(session,instance_id):
    from core.incidents.models import IncidentRow
    rows=(await session.execute(select(CIExecutionRow,IncidentRow).outerjoin(IncidentRow,CIExecutionRow.execution_id==IncidentRow.execution_id)
        .where(CIExecutionRow.instance_id==instance_id).order_by(CIExecutionRow.started_at))).all()
    return [dict(execution_kind="CI",execution_id=e.execution_id,incident_id=i.incident_id if i else None,outcome=e.outcome,
        test_id=e.execution_snapshot["workflow_source"]["test_id"],trigger=e.execution_snapshot["trigger"]["request_id"]) for e,i in rows]


async def assets(service,principal,plan_id):
    async with service.database.session() as session:
        plan=await session.get(PlanRow,plan_id)
        if plan is None: raise BusinessError("FEATURE_NOT_FOUND",404)
        await service.owned_report(session,principal,plan.report_id)
        rows=(await session.scalars(select(ScenarioInstanceRow).where(ScenarioInstanceRow.tenant_id==principal.tenant_id,
            ScenarioInstanceRow.owner_user_id==principal.user_id,ScenarioInstanceRow.current_state["workflow_source"]["plan_id"].astext==plan_id))).all()
        return dict(items=[public_asset(r,await references(session,r.instance_id)) for r in rows])


async def lock_task(session,principal,row):
    """必须在 Scenario/Incident 行锁之前调用；JSON 指针只用于定位权威行。"""
    try:
        return await approved(session,principal,row.current_state["workflow_source"]["plan_id"],row.current_state["workflow_source"]["test_id"],lock=True)
    except BusinessError as exc: raise CIError(exc.code,exc.status) from None


async def authorize(session,principal,row,binding,old,request_id,case_id,revision,asset_digest,verification):
    task,report,plan,test,source=binding
    expected=build(task.snapshot,test,row.current_state["distribution"]["target_ref"])
    source["asset_digest"]=expected["case_asset"]["asset_digest"]
    state=row.current_state;distribution=state["distribution"]
    if (principal.principal_kind!="HUMAN" or source!=state["workflow_source"] or state["case_asset"]!=expected["case_asset"]
            or distribution["mode"]!="LOCAL_SIMULATED" or distribution["asset_digest"]!=source["asset_digest"]
            or distribution["actor_kind"]!="HUMAN" or distribution["actor_user_id"]!=str(principal.user_id)
            or distribution["distribution_digest"]!=digest({k:v for k,v in distribution.items() if k!="distribution_digest"})
            or asset_digest!=source["asset_digest"] or revision is None or case_id is None):
        raise CIError("COVERAGE_ASSET_TRIGGER_INVALID",409)
    intent=dict(request_id=request_id,case_id=case_id,state_revision=revision,asset_digest=asset_digest,
        executor_ref=distribution["target_ref"],actor_kind="HUMAN",actor_user_id=str(principal.user_id),tenant_id=principal.tenant_id,
        verification_ref=verification)
    if old:
        if old.execution_snapshot.get("trigger",{}).get("intent_digest")!=digest(intent): raise CIError("CI_REQUEST_CONFLICT",409)
        return None
    if verification:
        from core.incidents.models import IncidentRow,IncidentActionRow
        incident=await session.get(IncidentRow,verification["incident_id"])
        origin=await session.get(CIExecutionRow,incident.execution_id) if incident else None
        action=await session.scalar(select(IncidentActionRow).where(IncidentActionRow.incident_id==verification["incident_id"],
            IncidentActionRow.status=="APPLIED",IncidentActionRow.applied_state_revision==revision))
        unchanged=row.state_revision==revision==1 and digest(state["baseband"])==expected["recipe_digest"]
        if (origin is None or origin.instance_id!=row.instance_id or revision!=row.state_revision
                or (not unchanged and (action is None or action.confirmed_by_user_id!=principal.user_id))):
            raise CIError("COVERAGE_DERIVATION_INVALID",409)
        intent["derivation"]=dict(origin_execution_id=origin.execution_id,action_id=action.action_id if action else None,
            state_revision=revision,actual_configuration_digest=digest(state["baseband"]))
    elif (row.state_revision!=1 or revision!=1 or digest(state["baseband"])!=expected["recipe_digest"]
            or state["recipe_digest"]!=expected["recipe_digest"]):
        raise CIError("COVERAGE_ASSET_REVISION_INVALID",409)
    # intent_digest 只含稳定的客户端意图及可信 actor，不含服务端时间/派生投影。
    stable={k:v for k,v in intent.items() if k!="derivation"}
    return dict(**intent,intent_digest=digest(stable),triggered_at=datetime.now(timezone.utc).isoformat())


def seal_execution(result,trigger,instance_id):
    """原件首次 INSERT 前加安全来源；不修改 input_digest 或新增第七条证据。"""
    snapshot=result["execution_snapshot"];snapshot["trigger"]=trigger
    snapshot["actual_recipe_digest"]=digest(snapshot["baseband"])
    source=snapshot["workflow_source"]
    safe={k:source[k] for k in ("task_id","report_id","plan_id","plan_version","plan_digest","test_id","test_digest","asset_digest")}
    safe["public_versions"]={k:snapshot["case_asset"][k] for k in ("feature_ref","interface_ref","tag_ref","message_ref","testcase_ref")}
    safe.update(instance_id=instance_id,distribution_id=snapshot["distribution"]["distribution_id"],trigger_id=trigger["request_id"],
        state_revision=trigger["state_revision"],actual_configuration_digest=snapshot["actual_recipe_digest"],derivation=trigger.get("derivation"))
    result["summary"]["workflow_source"]=safe
    for evidence in result["evidence_manifest"]:
        if evidence["kind"]=="CI_SUMMARY": evidence["digest"]=digest(evidence["content"])
    result["evidence_digest"]=digest(sorted(result["evidence_manifest"],key=lambda e:e["evidence_id"]))
